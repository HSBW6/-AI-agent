"""评测闭环（Evaluation Loop）：把总结文本里的 ```python 代码块真正跑起来验证。

管线：解析总结中的 python 代码块
      → 在受限命名空间 exec（无 import/文件/网络等危险能力）
      → 放进独立子进程执行（超时强杀，防死循环）
      → 按"题目用例套件"逐条断言
      → 返回结构化结果（passed / tests / error）

被 MultiAgentChat 的 chat.py / gui.py 调用：总结输出后自动验证，
CLI 打印"代码验证 ✓/✗"，GUI 在最终总结区渲染同样结论——
让"AI 讨论产出的代码被机器验证"可量化（对应交接文档 §10.3 第 1 条优化方向）。

⚠️ 安全边界说明（务必读）：
  本模块是"防意外破坏"级别的受限执行，不是对抗恶意攻击的强安全沙箱：
   - 受限 builtins（不含 open/__import__/eval/exec/compile/input/breakpoint 等）；
   - 独立子进程 + timeout 强杀（死循环不会拖垮主程序）；
   - 但 Python 本身无法做到绝对隔离，请勿在不可信高危环境把本模块当安全边界。

如何扩展新题目（对接后续 LeetCode 讨论题）：
  1. 在 TEST_SUITES 里注册一个新 key，例如：
       "reverse_string": {
           "label": "反转字符串（LeetCode 344）",
           "function_names": ["reverse_string", "reverseString"],
           "class_names": ["Solution"],
           "checkers": {"expect_result": "assert result == 'olleh', f'期望 olleh，实际 {result!r}'"},
           "cases": [
               {"name": "基本反转", "args": ["hello"], "checker": "expect_result"},
               ...
           ],
       }
  2. 参数结构：args 会被展开为 fn(*args)；checker 是断言代码片段，
     可引用局部变量 result（fn 的返回值）与 args（原参数列表）。
  3. 校验模式（checker）可复用；同类题目的新边界直接往 cases 里加即可。
"""
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 1. 代码块提取
# ---------------------------------------------------------------------------

# 匹配 ```python / ```py（大小写不敏感，容忍 ```python:xxx 或尾随空格）围栏
_FENCE_RE = re.compile(r"```(?:python|py)\b[^\n]*\n(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_code_blocks(text):
    """从总结文本中解析出所有 python 代码块（按出现顺序）。

    只识别带 python/py 语言标记的围栏块（总结者人设约定输出 ```python），
    避免把 ```text / ```bash 等说明块误当成可执行代码。
    无围栏的裸代码不提取——约定优先，宁缺毋滥。

    返回：字符串列表（代码块原文，已去掉围栏与首尾空行）。
    """
    if not text:
        return []
    blocks = []
    for m in _FENCE_RE.finditer(text):
        code = m.group(1).strip("\n")
        code = "\n".join(line.rstrip() for line in code.splitlines()).strip()
        if code:
            blocks.append(code)
    return blocks


# ---------------------------------------------------------------------------
# 2. 受限命名空间：只提供"写算法"所需的安全内建
# ---------------------------------------------------------------------------

SANDBOX_BUILTINS = (
    # 数据/运算
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "chr",
    "classmethod", "complex", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "hash", "hex", "int", "isinstance", "issubclass",
    "iter", "len", "list", "map", "max", "min", "next", "object", "oct", "ord",
    "pow", "print", "property", "range", "repr", "reversed", "round", "set",
    "slice", "sorted", "staticmethod", "str", "sum", "tuple", "type", "zip",
    # 类/语法支持（class 语句需要）
    "__build_class__",
    # 常见异常（让用户代码能 raise ValueError 等）
    "ArithmeticError", "AssertionError", "AttributeError", "BaseException",
    "EOFError", "Exception", "IndexError", "KeyError", "LookupError",
    "MemoryError", "NameError", "NotImplementedError", "OSError",
    "OverflowError", "RuntimeError", "StopIteration", "SyntaxError",
    "SystemError", "TypeError", "UnboundLocalError", "ValueError",
    "ZeroDivisionError",
)
# 明确不提供：__import__ / open / input / eval / exec / compile / breakpoint /
# getattr / setattr / delattr / globals / locals / vars / dir / exit / quit 等


def build_sandbox_globals():
    """构造受限 globals：__builtins__ 只含白名单（交给子进程侧执行组装）。

    这里返回的是"清单"，真正受限 dict 在子进程里构建（runner 见下），
    父进程侧只负责传递名单，避免本地泄露真实 builtins。
    """
    return {"__builtins__": {name: None for name in SANDBOX_BUILTINS},
            "__name__": "__sandbox__"}


# ---------------------------------------------------------------------------
# 3. 题目用例套件（单一来源，新增题目在此扩展）
# ---------------------------------------------------------------------------

# 两数之和的语义校验模板：仅要求返回的两个下标合法且和等于 target。
# 相比"和期望列表相等"，能容忍返回顺序任意，同时抓到两个常见错误：
#   - 同一元素用两次（返回 [i, i]）——用 i != j 拦截；
#   - 返回下标越界 / 元素和不对——用长度与求和拦截。
_CHECKER_TWO_SUM_INDICES = """
assert isinstance(result, (list, tuple)), f"应返回两个下标的列表/元组，实际: {result!r}"
assert len(result) == 2, f"应返回两个下标，实际: {result!r}"
i, j = int(result[0]), int(result[1])
assert i != j, f"两个下标不能指向同一元素: {result!r}（典型错误：同一元素用了两次）"
assert 0 <= i < len(args[0]) and 0 <= j < len(args[0]), f"下标越界: {result!r}"
assert args[0][i] + args[0][j] == args[1], (
    f"nums[{i}] + nums[{j}] = {args[0][i]} + {args[0][j]} != target={args[1]}"
)
"""


def _long_two_sum_nums():
    """性能边界输入：前 4000 个偶数的数组 + 末尾两个大奇数。

    线性哈希解法毫秒级通过；O(n^2) 暴力法在此用例上明显变慢，
    配合子进程 timeout 能筛掉明显低效实现。
    """
    nums = list(range(2, 8002, 2))       # 4000 个偶数
    nums += [1_000_003, 1_000_005]       # 两个大奇数在末尾
    return nums, 2_000_008               # 1_000_003 + 1_000_005


# 默认题目用例套件（run_code_verification / chat.run_discussion 的 verify_suite 共用）
DEFAULT_SUITE = "two_sum"

TEST_SUITES = {
    DEFAULT_SUITE: {
        "label": "两数之和（LeetCode 1）",
        # 总结者代码常见命名都覆盖；class Solution 模板也支持（见 runner）
        "function_names": ["two_sum", "twoSum", "twosum", "TwoSum"],
        "class_names": ["Solution"],
        "checkers": {"two_sum_indices": _CHECKER_TWO_SUM_INDICES},
        "cases": [
            {"name": "基本命中", "args": [[2, 7, 11, 15], 9],
             "checker": "two_sum_indices"},
            {"name": "答案在数组中间", "args": [[3, 2, 4], 6],
             "checker": "two_sum_indices"},
            {"name": "边界·两个相同元素（不可用同一元素两次）", "args": [[3, 3], 6],
             "checker": "two_sum_indices"},
            {"name": "边界·零值与重复元素", "args": [[0, 4, 3, 0], 0],
             "checker": "two_sum_indices"},
            {"name": "边界·负数参与命中", "args": [[-3, 4, 3, 90], 0],
             "checker": "two_sum_indices"},
            {"name": "性能边界·4000+ 长数组尾部命中", "args": _long_two_sum_nums(),
             "checker": "two_sum_indices"},
        ],
    },
}

# 找不到套件时的兜底
_UNKNOWN_LABEL = "未知题目"


def get_suite(suite_name):
    """按名取套件；找不到返回 None（由调用方决定如何展示）"""
    return TEST_SUITES.get(suite_name)


# ---------------------------------------------------------------------------
# 4. 子进程 runner：受限执行用户代码 + 逐用例断言
# ---------------------------------------------------------------------------

# runner 以特权身份运行（自身可 import json/sys），只有"用户代码"被 exec 进受限
# 命名空间。payload（含用户代码与用例）通过 stdin 传入，避免超长命令行参数；
# 结果通过 stdout 上的哨兵包裹 JSON 回传。
_RUNNER_SCRIPT = r"""
import json, sys, builtins as _b

def _main():
    out = {"ok": False, "tests": [], "error": None, "function": None}
    # stdout 固定 UTF-8（Windows 控制台默认 cp936 会把中文 JSON 写成 GBK，
    # 父进程统一按 UTF-8 解码；子进程被捕获时无控制台，必须显式 reconfig）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception as e:
        out["error"] = f"payload 解析失败: {e}"
        sys.stdout.write("__EVAL_RESULT__" + json.dumps(out, ensure_ascii=False) + "__END__")
        return

    # 受限命名空间：只放白名单内建
    safe = {}
    for _n in data.get("allowed_builtins", []):
        try:
            safe[_n] = getattr(_b, _n)
        except AttributeError:
            pass
    ns = {"__builtins__": safe, "__name__": "__sandbox__"}

    # 1) exec 用户代码（编译错误/顶层 NameError 在此被捕获）
    try:
        exec(compile(data["code"], "<generated>", "exec"), ns, ns)
    except Exception as e:
        out["error"] = f"代码无法执行: {type(e).__name__}: {e}"
        sys.stdout.write("__EVAL_RESULT__" + json.dumps(out, ensure_ascii=False) + "__END__")
        return

    # 2) 定位解题函数：函数级别名优先，其次 class Solution 模板（实例方法）
    fn = None
    found = None
    for _a in data.get("function_names", []):
        _cand = ns.get(_a)
        if callable(_cand):
            fn, found = _cand, _a
            break
    if fn is None:
        for _cn in data.get("class_names", []):
            _cls = ns.get(_cn)
            if _cls is None:
                continue
            for _a in data.get("function_names", []):
                _cand = getattr(_cls, _a, None)
                if not callable(_cand):
                    continue
                try:                       # LeetCode 模板通常是实例方法
                    fn = getattr(_cls(), _a)
                    found = f"{_cn}.{_a}"
                    break
                except Exception:
                    fn = _cand               # 静态/类方法等，原样调用
                    found = f"{_cn}.{_a}"
                    break
            if fn is not None:
                break
    if fn is None:
        out["error"] = ("未找到解题函数（期望函数名："
                        + "/".join(data.get("function_names", [])) + "）")
        sys.stdout.write("__EVAL_RESULT__" + json.dumps(out, ensure_ascii=False) + "__END__")
        return
    out["function"] = found

    # 3) 逐用例断言（每个用例独立捕获异常，互不影响）
    checkers = data.get("checkers", {})
    all_ok = True
    for _case in data.get("cases", []):
        _tr = {"name": _case["name"], "passed": False, "detail": ""}
        try:
            _result = fn(*_case["args"])
            _tpl = checkers.get(_case["checker"], "")
            if not _tpl:
                raise RuntimeError(f"缺少校验模板: {_case['checker']}")
            _loc = {"result": _result, "args": _case["args"]}
            exec(compile(_tpl, "<checker>", "exec"), _loc, _loc)
            _tr["passed"] = True
        except AssertionError as _e:
            _tr["detail"] = f"断言失败: {_e}" if str(_e) else "断言失败"
        except Exception as _e:
            _tr["detail"] = f"{type(_e).__name__}: {_e}"
        all_ok = all_ok and _tr["passed"]
        out["tests"].append(_tr)
    out["ok"] = all_ok
    sys.stdout.write("__EVAL_RESULT__" + json.dumps(out, ensure_ascii=False) + "__END__")

_main()
"""

def _terminate_process_tree(proc):
    """超时后杀整棵进程树（子进程 + 它 spawn 的孙子进程）。

    只杀直接子进程可能留孤儿：若被评测代码逃逸沙箱并创建了孙进程，
    TerminateProcess 不会连坐。Windows 用 taskkill /T /F 递归杀整树，
    POSIX 用 start_new_session + os.killpg 杀整个进程组。
    """
    if os.name == "nt":
        kill_kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kill_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            # /T = 连带子进程树, /F = 强制杀; pythonw 启动时 CREATE_NO_WINDOW 防闪黑框
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10, **kill_kwargs,
            )
        except Exception:
            try:
                proc.kill()          # taskkill 失败时退回直接杀
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass



# ---------------------------------------------------------------------------
# 5. 结果类型与对外 API
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    """单个用例的验证结果"""
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerificationResult:
    """一次完整代码验证的结果"""
    passed: bool            # 代码提取成功且所有用例通过才算 True
    status: str             # pass / fail / no_code / error / timeout
    tests: list = field(default_factory=list)   # list[TestResult]
    error: str = None       # 失败/异常说明（无错误时为 None）
    code: str = None        # 被实际验证的代码块（未提取到则为 None）
    function: str = None    # 命中的函数名（如 two_sum / Solution.twoSum）
    suite_label: str = ""


def run_code_verification(summary_text, suite_name=DEFAULT_SUITE, timeout=15,
                          use_last_block=True):
    """把总结文本中的 python 代码块在受限环境执行并跑题目用例断言。

    参数：
      summary_text    总结者输出全文（从中解析 ```python 代码块）
      suite_name      题目用例套件 key（见 TEST_SUITES，扩展新题在此加）
      timeout         子进程执行总超时（秒），防止死循环拖垮调用方
      use_last_block  总结含多个代码块时，是否只用最后一个（默认 True，
                      通常最后一块才是最终代码；其余块按"思路草稿"忽略）
    返回：
      VerificationResult
    """
    suite = get_suite(suite_name)
    label = suite["label"] if suite else _UNKNOWN_LABEL

    blocks = extract_code_blocks(summary_text)
    if not blocks:
        return VerificationResult(
            passed=False, status="no_code", code=None,
            error="总结中未找到 ```python 代码块（人设约定：代码必须用 python 围栏包裹）",
            suite_label=label,
        )
    code = blocks[-1] if use_last_block else blocks[0]

    if suite is None:
        return VerificationResult(
            passed=False, status="error", code=code,
            error=f"用例套件不存在: {suite_name}（请在 evaluation.TEST_SUITES 注册）",
            suite_label=label,
        )

    payload = {
        "code": code,
        "allowed_builtins": list(SANDBOX_BUILTINS),
        "function_names": suite.get("function_names", []),
        "class_names": suite.get("class_names", []),
        "checkers": suite.get("checkers", {}),
        "cases": suite.get("cases", []),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"   # 双保险：runner 自身 reconfigure 为主
        popen_kwargs = {}
        if os.name == "nt":
            # Windows：让子进程自成一个进程组，超时后 taskkill /T 才能整树击杀
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # POSIX：新会话 → 整个进程组可被 os.killpg 一锅端
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _RUNNER_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, **popen_kwargs,
        )
        try:
            out_bytes, err_bytes = proc.communicate(
                input=payload_json.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)          # 先杀整棵进程树
            try:
                out_bytes, err_bytes = proc.communicate(timeout=5)   # 回收管道
            except Exception:
                out_bytes, err_bytes = b"", b""
            return VerificationResult(
                passed=False, status="timeout", code=code,
                error=f"执行超时（>{timeout}s），疑似死循环或复杂度失控，已终止进程树",
                suite_label=label,
            )
    except FileNotFoundError as e:
        return VerificationResult(
            passed=False, status="error", code=code,
            error=f"无法启动沙箱进程: {e}",
            suite_label=label,
        )


    out_text = out_bytes.decode("utf-8", errors="replace")
    stderr = err_bytes.decode("utf-8", errors="replace").strip()
    marker = "__EVAL_RESULT__"
    start = out_text.rfind(marker)
    if start < 0:
        return VerificationResult(
            passed=False, status="error", code=code,
            error=f"沙箱子进程无有效输出（exit={proc.returncode}）"
                  + (f"；stderr: {stderr[:500]}" if stderr else ""),
            suite_label=label,
        )
    end = out_text.find("__END__", start + len(marker))
    if end < 0:
        end = len(out_text)
    try:
        data = json.loads(out_text[start + len(marker):end])
    except Exception as e:
        return VerificationResult(
            passed=False, status="error", code=code,
            error=f"沙箱结果解析失败: {e}",
            suite_label=label,
        )

    tests = [TestResult(name=t["name"], passed=bool(t["passed"]),
                        detail=t.get("detail", ""))
             for t in data.get("tests", [])]
    status = "pass" if data.get("ok") else "fail"
    if data.get("error"):
        status = "error"
    return VerificationResult(
        passed=bool(data.get("ok")) and not data.get("error"),
        status=status,
        tests=tests,
        error=data.get("error"),
        code=code,
        function=data.get("function"),
        suite_label=label,
    )


# ---------------------------------------------------------------------------
# 6. CLI 展示辅助
# ---------------------------------------------------------------------------

def format_verification(result):
    """把 VerificationResult 渲染成 CLI 的多行文本（chat.py 打印用）。

    返回带换行的字符串；通过/失败用 ✓/✗ 直观区分。
    """
    label = result.suite_label or _UNKNOWN_LABEL
    lines = []
    if result.status == "pass":
        n = len(result.tests)
        lines.append(f"[代码验证 ✓] {label}：{n}/{n} 用例通过"
                     + (f"（命中函数 {result.function}）" if result.function else ""))
        return "\n".join(lines)

    lines.append(f"[代码验证 ✗] {label}：")
    if result.status == "fail":
        passed_n = sum(1 for t in result.tests if t.passed)
        total = len(result.tests)
        lines[-1] += f"通过 {passed_n}/{total}，失败 {total - passed_n} 个用例"
        for t in result.tests:
            if not t.passed:
                lines.append(f"  - 「{t.name}」：{t.detail or '断言失败'}")
    else:
        lines[-1] += result.error or "验证失败"
        lines[-1] += f"（命中函数 {result.function}）" if result.function else ""
    return "\n".join(lines)


if __name__ == "__main__":
    # 快速自测：跑默认两数之和套件（供手工冒烟，不进入 unittest）
    import textwrap
    demo = textwrap.dedent("""
        总结要点……（略）

        ```python
        def two_sum(nums, target):
            seen = {}
            for i, num in enumerate(nums):
                if target - num in seen:
                    return [seen[target - num], i]
                seen[num] = i
        ```
    """)
    print(format_verification(run_code_verification(demo)))
