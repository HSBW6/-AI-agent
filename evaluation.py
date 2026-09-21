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


def _block_defines(block, names):
    """代码块里是否**定义**了 names 中的函数/类（只认 def/class 定义行）。"""
    for name in names:
        if re.search(r"^\s*(?:async\s+)?def\s+%s\s*\(" % re.escape(name), block, re.M):
            return True
        if re.search(r"^\s*class\s+%s\s*[\(:]" % re.escape(name), block, re.M):
            return True
    return False


def pick_verification_block(blocks, suite, use_last_block=True):
    """从多个代码块里挑出真正定义了解题函数的那一块。

    规则（从强到弱）：
      1) 块内定义了套件的 function_names 或 class_names → 命中多块时取最后一块
         （同一题常被反复重写，定稿在最后）
      2) 一块都没命中 → 回退旧语义：
         use_last_block=True 取最后一块，False 取第一块
    """
    if not blocks:
        return None
    suite = suite or {}
    names = list(suite.get("function_names", []) or []) \
        + list(suite.get("class_names", []) or [])
    matched = [b for b in blocks if _block_defines(b, names)]
    if matched:
        return matched[-1]
    return blocks[-1] if use_last_block else blocks[0]


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
    """性能边界输入：前 12000 个偶数的数组 + 末尾两个大奇数（共 12002 个元素）。

    线性哈希解本机实测 0.05~0.06s 通过；O(n^2) 暴力解实测 2.21~2.29s（连跑 3 次），
    稳定超过本用例 time_limit=2 秒——筛掉暴力解靠的是用例级限时（结果正确但超时
    同样判失败），全局 timeout=15 只兜死循环。12000 的限时余量约 10%，换明显更快
    的机器需重新校准规模；20000 规模实测单例 6.09s，拖慢整轮验证，故不取。
    答案被刻意放在数组最后两位：暴力解必须扫完全表才命中。
    """
    nums = list(range(2, 24002, 2))       # 12000 个偶数
    nums += [1_000_003, 1_000_005]        # 两个大奇数在末尾
    return nums, 2_000_008                # 1_000_003 + 1_000_005


# 默认题目用例套件（run_code_verification / chat.run_discussion 的 verify_suite 共用）
# 用例字典字段：name / args / checker 必填；可选 time_limit（秒）——断言全通过但
# 单例耗时超过该值同样判失败（用于筛掉结果对但复杂度不达标的解法），缺省不启用。
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
            {"name": "性能边界·12000 长数组尾部命中", "args": _long_two_sum_nums(),
             "checker": "two_sum_indices", "time_limit": 2},
        ],
    },
}

# 找不到套件时的兜底标签：**仅**用于"显式传了未注册套件名"这条开发者错误路径
# （下面 run_code_verification 里 suite is None 的 error 分支）。自动模式没匹配到套件
# 走 status="skipped"，不复用它——那类场景题目是已知的，说"未知题目"属误导。
_UNKNOWN_LABEL = "未知题目"


def get_suite(suite_name):
    """按名取套件；找不到返回 None（由调用方决定如何展示）"""
    return TEST_SUITES.get(suite_name)


# 自动挑套件的关键词表：只放高置信的题目专名，宁可跳过也不猜错。
# 键 = 套件 key，值 = 命中该题的关键词（一律小写，匹配前题面也转小写）
_AUTO_MATCH_HINTS = {
    "two_sum": ("两数之和", "two sum", "two_sum", "twosum", "2sum"),
}

# 题面"标题区"的判定：先剥掉"题目：/问题：/Task:"这类前缀标签，再截到第一个句读
_TITLE_LABEL_RE = re.compile(r"^\s*(?:题目|问题|task|problem)\s*[:：]\s*", re.IGNORECASE)
_TITLE_CUT_CHARS = "。！？；\n\r"


def topic_title(topic):
    """取题面的"标题区"（小写后返回）：第一个非空行 → 剥掉"题目："类前缀 →
    截到第一个句读符号（。！？；）。

    返回空串表示拿不到标题区（调用方按"拿不准"处理）。只看标题区是为了区分
    "题面就是在讲这道题"（专名出现在标题里）与"正文顺口提一句"（如
    "这题比两数之和难很多"，专名埋在正文里）——后者不该被认成该题。
    """
    for raw_line in (topic or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = _TITLE_LABEL_RE.sub("", line)
        for idx, ch in enumerate(line):
            if ch in _TITLE_CUT_CHARS:
                line = line[:idx]
                break
        return line.strip().lower()
    return ""


def pick_suite(topic):
    """按题面自动挑用例套件；拿不准返回 None（调用方应跳过验证，不猜）。

    ⚠️ 这是启发式，不是精确识别。判定条件（先看标题区，正文一律不看）：
      标题区中命中关键词，且满足其一——
        ① 标题区**以关键词打头**（"两数之和（LeetCode 1）"、"Two Sum - LeetCode 1"）；
        ② 标题区同时命中 **≥2 个不同关键词**。
      因此像 "LeetCode 1: Two Sum" 这种"题号在前"的写法会漏判 —— 宁可漏成
      skipped，也不猜错成假红/假绿。
    只认高置信专名，不放 target / 数组 这类泛词——避免给不相关题目
    硬套 two_sum 用例，把好代码判成"假红"。
    """
    title = topic_title(topic)
    if not title:
        return None
    for suite_name, hints in _AUTO_MATCH_HINTS.items():
        if suite_name not in TEST_SUITES:
            continue
        hits = [h for h in hints if h in title]
        if not hits:
            continue
        if len(hits) >= 2 or any(title.startswith(h) for h in hits):
            return suite_name
    return None


def validate_suite(suite_name):
    """父进程预校验用例套件，返回问题清单（空列表 = 合法）。

    ⚠️ 必须在起子进程之前调用。套件坏掉时若照常跑到 runner，例如 cases 为空，
    子进程里的 `for _case in cases` 会空转、all_ok 保持 True → 父进程拿到
    ok=True 直接渲染成 [代码验证 ✓]（**假 pass**：什么都没验，却报通过）。
    把"套件级故障"拦在父进程，统一报 error，与"代码坏"（fail）区分开。

    校验项：
      1. 套件存在；
      2. cases 是非空 list（空套件 = 无用例可验，必然假 pass）；
      3. 每个用例的 args 是 list/tuple（runner 用 fn(*args) 展开发参）；若是字符串、
         数字这类非序列，会被按字符/类型错误展开，验的就不是原意了；
      4. 每个用例的 checker 名在套件 checkers 中存在且模板为非空字符串
         （缺模板时 runner 抛 RuntimeError，会把"套件坏"误报成"代码坏"）。
    """
    suite = get_suite(suite_name)
    if suite is None:
        return [f"用例套件不存在: {suite_name}（请在 evaluation.TEST_SUITES 注册）"]

    problems = []
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        problems.append("cases 为空或不是列表：无用例可验证（继续跑会假 pass）")
        return problems          # 没有用例，逐用例校验无意义

    checkers = suite.get("checkers") or {}
    for idx, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            problems.append(f"第 {idx} 个用例不是 dict: {case!r}")
            continue
        name = case.get("name") or f"#{idx}"
        if not isinstance(case.get("args"), (list, tuple)):
            problems.append(
                f"用例「{name}」的 args 必须是 list/tuple（会被 fn(*args) 展开），"
                f"实际 {type(case.get('args')).__name__}")
        checker_name = case.get("checker")
        template = checkers.get(checker_name)
        if not isinstance(template, str) or not template.strip():
            problems.append(f"用例「{name}」引用的 checker 无效: {checker_name!r}")
    return problems



# ---------------------------------------------------------------------------
# 4. 子进程 runner：受限执行用户代码 + 逐用例断言
# ---------------------------------------------------------------------------

# runner 以特权身份运行（自身可 import json/sys/time），只有"用户代码"被 exec 进受限
# 命名空间。payload（含用户代码与用例）通过 stdin 传入，避免超长命令行参数；
# 结果通过 stdout 上的哨兵包裹 JSON 回传。
_RUNNER_SCRIPT = r"""
import json, sys, time, builtins as _b


class _CaseTimeLimit(Exception):
    # 用例级限时被突破（结果对但太慢）：单独成类，便于与"断言失败"区分并报出实际耗时
    pass


def _main():
    out = {"ok": False, "tests": [], "error": None, "function": None}
    # stdout 固定 UTF-8（Windows 控制台默认 cp936 会把中文 JSON 写成 GBK，
    # 父进程统一按 UTF-8 解码；子进程被捕获时无控制台，必须显式 reconfig）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
    _cases = data.get("cases", [])
    if not _cases:
        # 双保险：父进程 validate_suite 已拦空套件，这里再兜一层，
        # 防止空 for 循环让 all_ok 保持 True 而报出"假 pass"
        out["error"] = "用例套件无效：cases 为空，无用例可验证"
        sys.stdout.write("__EVAL_RESULT__" + json.dumps(out, ensure_ascii=False) + "__END__")
        return
    all_ok = True
    for _case in _cases:
        _tr = {"name": _case["name"], "passed": False, "detail": ""}
        _limit = _case.get("time_limit")
        _t0 = time.perf_counter() if _limit is not None else None
        try:
            _result = fn(*_case["args"])
            _tpl = checkers.get(_case["checker"], "")
            if not _tpl:
                raise RuntimeError(f"缺少校验模板: {_case['checker']}")
            _loc = {"result": _result, "args": _case["args"]}
            exec(compile(_tpl, "<checker>", "exec"), _loc, _loc)
            if _limit is not None:
                _elapsed = time.perf_counter() - _t0
                if _elapsed > _limit:
                    raise _CaseTimeLimit(
                        f"超出用例限时: 限时 {_limit:g}s，实际耗时 {_elapsed:.2f}s"
                        "（结果正确但性能不达标）"
                    )
            _tr["passed"] = True
        except AssertionError as _e:
            _tr["detail"] = f"断言失败: {_e}" if str(_e) else "断言失败"
        except _CaseTimeLimit as _e:
            _tr["detail"] = str(_e)
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
    status: str             # pass / fail / no_code / error / timeout / skipped
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
      timeout         子进程执行总超时（秒），只兜死循环；单例性能门槛由套件里
                      各用例自己的可选 time_limit 字段控制（断言全过但超时同样
                      判该用例失败）
       use_last_block  多块都没命中解题函数时的兜底：True 取最后一块，False 取
                      第一块（默认 True）。命中 function_names/class_names 的块
                      始终优先，不受此开关影响
    返回：
      VerificationResult
      status: str             # pass / fail / no_code / error / timeout / skipped
    """

    if suite_name is None:
        # 自动模式没匹配到套件：中性跳过，不算失败。
        # 注意：这里**不能**复用 _UNKNOWN_LABEL（"未知题目"）——题目本身是已知的，
        # 只是没给它注册用例套件；说成"未知题目"属臆断误导。文案改成完整句式说清
        # 因果，suite_label 留空，由 CLI/GUI 渲染层自行决定要不要贴标签前缀。
        return VerificationResult(
            passed=False, status="skipped", code=None,
            error="未注册该题目的用例套件，已跳过代码验证（可继续讨论，不影响结论）",
            suite_label="",
        )
    suite = get_suite(suite_name)
    label = suite["label"] if suite else _UNKNOWN_LABEL

    blocks = extract_code_blocks(summary_text)
    if not blocks:
        return VerificationResult(
            passed=False, status="no_code", code=None,
            error="总结中未找到 ```python 代码块（人设约定：代码必须用 python 围栏包裹）",
            suite_label=label,
        )
    code = pick_verification_block(blocks, suite, use_last_block)


    if suite is None:
        return VerificationResult(
            passed=False, status="error", code=code,
            error=f"用例套件不存在: {suite_name}（请在 evaluation.TEST_SUITES 注册）",
            suite_label=label,
        )

    # 父进程预校验（批次 1.2）：套件本身有毛病就直接报 error，
    # 绝不放行到子进程——否则空套件会让 runner 空转出"假 pass"。
    problems = validate_suite(suite_name)
    if problems:
        return VerificationResult(
            passed=False, status="error", code=code,
            error="用例套件无效：" + "；".join(problems),
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
        # 最小环境变量：子进程只要能起来就够，不需要（也不该拿到）API Key 等敏感变量。
        # 注：-I 隐含 -E，PYTHON* 环境变量一律被忽略，故此处不设 PYTHONIOENCODING；
        #     编码已由 runner 自己 reconfigure 保证。Windows 必须保留 SystemRoot，否则子进程可能起不来。
        _env_allow = ("SystemRoot", "windir", "PATH", "TEMP", "TMP", "PATHEXT")
        env = {k: os.environ[k] for k in _env_allow if k in os.environ}

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

def ensure_utf8_stdio():
    """把父进程 stdout / stderr 切到 UTF-8，避免 GBK 控制台下输出 ✓/✗ 崩溃。

    背景（坑 8 / 待办 4.1）：format_verification 会打印 ✓ / ✗（U+2713 / U+2717），
    GBK(cp936) 编码不了。交互式控制台由 Python 走 WinAPI 不会崩，但
    `python chat.py > log.txt`、管道重定向、以及部分 IDE 捕获 stdout 时，
    sys.stdout.encoding 会回退到 cp936，直接抛 UnicodeEncodeError。

    runner 子进程内部已自行 reconfigure（见 _RUNNER_SCRIPT），本函数专供父进程
    入口（chat.py __main__ 与 evaluation.py demo）在打印前调用。容错优先：流不支持
    reconfigure（非 TextIOWrapper）或已关闭时静默跳过，绝不因设置编码本身把程序弄崩。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def format_verification(result):
    """把 VerificationResult 渲染成 CLI 的多行文本（chat.py 打印用）。

    返回带换行的字符串；通过/失败用 ✓/✗ 直观区分。
    """
    if result.status == "skipped":
        # 用 ASCII '-' 保持中性，别让人误以为判失败。
        # 自动模式没匹配到套件时 suite_label 为空，此时不贴标签前缀，更不许回退成
        # _UNKNOWN_LABEL（"未知题目"）——题目是已知的，只是没注册套件，属误导。
        detail = result.error or "已跳过代码验证"
        prefix = f"{result.suite_label}：" if result.suite_label else ""
        return f"[代码验证 -] {prefix}{detail}"

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
    # 注意：demo 会 print 带 ✓ 的验证结果，Windows 默认 GBK 直接 UnicodeEncodeError
    ensure_utf8_stdio()
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
