#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四配置对照实验的执行层。

设计原则：**不改产品代码**（chat.py / agent.py / evaluation.py / personas.py），
只复用它们的构件（Agent / run_discussion / run_code_verification）。

四种配置：
  single                单模型直答：总结者角色直接输出完整代码（无讨论）
  single_self_correct   单模型自纠：验证失败 → 把错误信息回喂 → 重答（≤max_retries）
  homogeneous           双模型讨论，两个角色都用同一厂商（deepseek）
  heterogeneous         双模型讨论，DeepSeek + 智谱（产品现状）

公平性的关键（本实验唯一的变量应当是"有没有讨论"）：
  四种配置**都由「总结者」角色产出最终代码**，人设相同、max_tokens 相同
  （config.SUMMARIZER_MAX_TOKENS），题面构造函数也相同。区别只有：
    - single / single_self_correct：总结者直接答题
    - homogeneous / heterogeneous：先由两个角色讨论，总结者再据讨论产出代码

   注意：不能拿「DeepSeek」角色跑 single —— 它的人设写着"每次回复不超过
   180 字（讲题破例 250 字）"，根本容不下完整代码，对比会失真。

失败原因分类的意义：
  数据集里有若干"约定陷阱"题（has_cycle 用 (nums,pos) 表示链表、
  rotate_array 要求返回新数组、two_sum_ii 用 1-based 下标等）。模型失败
  可能只是没按题面接口写，而非算法不会。把 interface_* 与
  assertion_failed 分开统计，结论才干净。
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from agent import Agent  # noqa: E402
from chat import run_discussion  # noqa: E402
from evaluation import run_code_verification  # noqa: E402

# ---------------------------------------------------------------- 常量

MODES = ("single", "single_self_correct", "homogeneous", "heterogeneous")

# 产出最终代码的角色：与产品里的总结者是同一人设（中立主持人，人设要求
# "给出完整可运行代码，用 ```python 包裹"），保证四配置对齐。
SOLVER_ROLE = "总结者"

# 讨论参与者角色（产品现状就是这两个人设）
DEBATE_ROLES = ("DeepSeek", "智谱")

DEFAULT_DATASETS = (
    ROOT / "experiments" / "dataset_raw.json",
    ROOT / "experiments" / "dataset_raw_batch2.json",
)

# 失败原因分类（前缀 interface_ 的属于"没按题面接口写"，不算算法能力问题）
ERROR_CLASSES = (
    "passed",
    "no_code",                   # 没输出代码块
    "syntax_error",              # 语法错
    "interface_function_missing",  # 找不到题面要求的函数名
    "interface_arg_mismatch",    # 参数个数/类型不匹配
    "runtime_error",             # 其它运行时异常
    "assertion_failed",          # 断言失败（真正的算法错）
    "timeout",                   # 超时
    "skipped",                   # 未注册套件，跳过
    "unknown",
)

SOLVER_OUTPUT_RULES = (
    "\n\n【输出要求】\n"
    "1. 只输出一个 ```python 代码块，里面是完整、可直接运行的实现；"
    "不要给多个候选版本，不要只给思路。\n"
    "2. 实现的函数名必须是 {fn}，签名必须是 {sig}。\n"
    "3. 允许定义辅助函数，但不要在顶层 import 任何模块，"
    "不要读写文件、不要联网。\n"
    "4. 这段代码会被放进沙箱真实跑测试用例，请确保边界情况正确。\n"
)


# ---------------------------------------------------------------- 工具

def _g(obj, key, default=None):
    """同时支持 VerificationResult 对象与 hooks 捕获到的 dict。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def load_problems(paths=None, difficulties=None, limit=None):
    """合并多批数据集；按 suite_key 去重；可筛难度与限量。"""
    problems, seen = [], set()
    for p in (paths or DEFAULT_DATASETS):
        p = Path(p)
        if not p.exists():
            print("[警告] 数据集不存在，跳过：%s" % p, file=sys.stderr)
            continue
        for item in json.loads(p.read_text(encoding="utf-8")):
            key = item.get("suite_key")
            if not key or key in seen:
                continue
            if difficulties and item.get("difficulty") not in difficulties:
                continue
            seen.add(key)
            item["_source"] = p.name
            problems.append(item)
    if limit:
        problems = problems[:limit]
    return problems


def build_solver_prompt(problem):
    """题面 + 明确的输出契约（同一个函数供四种配置使用，保证题面一致）。"""
    rule = SOLVER_OUTPUT_RULES.format(
        fn=problem["function_name"], sig=problem["signature"]
    )
    return problem["prompt"] + rule


def classify_failure(result):
    """把验证结果归类为可统计的失败类型。"""
    status = _g(result, "status")
    if status == "pass":
        return "passed"
    if status == "skipped":
        return "skipped"
    if status == "no_code":
        return "no_code"
    if status == "timeout":
        return "timeout"
    if status == "fail":
        return "assertion_failed"
    # status == "error"：按 error 文本与命中函数细分
    err = (_g(result, "error") or "")
    low = err.lower()
    if "syntaxerror" in low or "语法错误" in err:
        return "syntax_error"
    if _g(result, "function") in (None, "") and (
        "函数" in err or "function" in low or "未找到" in err or "找不到" in err
    ):
        return "interface_function_missing"
    if "typeerror" in low and ("argument" in low or "positional" in low or "参数" in err):
        return "interface_arg_mismatch"
    if "typeerror" in low:
        return "interface_arg_mismatch"
    if err:
        return "runtime_error"
    return "unknown"


def build_feedback(result, attempt_no):
    """把沙箱失败信息整理成给模型的修正指令 —— 这就是"验证反馈回流"。"""
    lines = ["\n\n【第 %d 次提交未通过测试，请修正后重新给出完整代码】" % attempt_no]
    status = _g(result, "status")
    if status == "no_code":
        lines.append("你没有输出 ```python 代码块。请只输出一个代码块，内含完整实现。")
    elif status == "timeout":
        lines.append("你的代码执行超时（15 秒）。请检查是否存在死循环，或复杂度是否过高。")
    elif status == "fail":
        failed = [t for t in (_g(result, "tests") or []) if not _g(t, "passed")]
        if not failed:
            lines.append("有用例未通过，但没有拿到具体用例信息。")
        for t in failed[:3]:
            lines.append("失败用例「%s」：%s" % (_g(t, "name"), _g(t, "detail") or "断言未通过"))
    elif status == "error":
        lines.append("代码执行报错：%s" % (_g(result, "error") or "未知错误"))
    else:
        lines.append("上次提交未通过（status=%s）：%s" % (status, _g(result, "error")))
    lines.append("请给出修正后的完整代码（仍然只用一个 ```python 代码块）。")
    return "\n".join(lines)


@dataclass
class RunResult:
    """一次实验运行的结果（一条记录 = 一道题 × 一种配置）。"""
    mode: str
    suite_key: str
    difficulty: str
    passed: bool
    status: str
    error_class: str
    attempts: int
    elapsed_sec: float
    code_len: int = 0
    detail: str = ""
    code: str = field(default="", repr=False)


# ---------------------------------------------------------------- 四种配置

def run_single(problem, provider="deepseek", temperature=None, max_tokens=None):
    """单模型直答：总结者一次性输出代码。返回 (reply, VerificationResult)。"""
    agent = Agent(SOLVER_ROLE, provider=provider, temperature=temperature)
    reply = agent.say(
        build_solver_prompt(problem),
        extra_instruction="这是一道独立的算法题（没有讨论记录），请直接给出最终实现。",
        max_tokens=max_tokens or config.SUMMARIZER_MAX_TOKENS,
    )
    return reply, run_code_verification(reply, suite_name=problem["suite_key"])


def run_single_self_correct(problem, provider="deepseek", temperature=None,
                            max_retries=2, max_tokens=None):
    """单模型自纠：答 → 验证 → 把错误回喂 → 重答。返回 (最后一次reply, 最后验证, 次数)。"""
    agent = Agent(SOLVER_ROLE, provider=provider, temperature=temperature)
    base = build_solver_prompt(problem)
    text = base
    reply, result, attempts = "", None, 0
    while attempts <= max_retries:
        attempts += 1
        reply = agent.say(
            text,
            extra_instruction="这是一道独立的算法题（没有讨论记录），请直接给出最终实现。",
            max_tokens=max_tokens or config.SUMMARIZER_MAX_TOKENS,
        )
        result = run_code_verification(reply, suite_name=problem["suite_key"])
        if _g(result, "passed") or _g(result, "status") in ("skipped",):
            break
        text = base + build_feedback(result, attempts)
    return reply, result, attempts


def run_debate(problem, providers=("deepseek", "zhipu"), rounds=3,
               summarizer_provider="deepseek", temperature=None):
    """双模型讨论后由总结者产出代码。

    providers=("deepseek","deepseek") → 同质；("deepseek","zhipu") → 异构。
    验证结果通过 hooks 的 on_verification 捕获（不改产品代码）。
    """
    captured = {}

    def _on_verification(**kwargs):
        captured.update(kwargs)

    participants = [
        (DEBATE_ROLES[i], providers[i]) for i in range(len(providers))
    ]
    summary = run_discussion(
        topic=build_solver_prompt(problem),
        participant_names=participants,
        max_rounds=rounds,
        summarizer_provider=summarizer_provider,
        hooks={"on_verification": _on_verification},
        verbose=False,
        verify_code=True,
        verify_suite=problem["suite_key"],
    )
    return summary, captured


def _camel_variants(fn):
    """生成 camelCase 变体，减少"模型写了 validParentheses 但引擎只认
    valid_parentheses"这类接口假失败。"""
    parts = fn.split("_")
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    return [camel] if camel != fn else []


def to_suite(problem):
    """把数据集条目转成 evaluation.TEST_SUITES 需要的套件结构。

    两处格式差异必须转换：
      1. 数据集用 `expect`（期望值），评测引擎用 `checker`（断言模板字符串）；
         故为每个用例生成一个专属模板。模板里只嵌入期望值的 %r 字面量，
         失败消息则用 repr(result) 在运行时拼接——这样无论期望值是字符串
         还是含引号的文本，都不会造成模板自身的引号冲突。
      2. 引擎按 function_names 列表匹配函数，故补上 camelCase 变体。
    """
    checkers, cases = {}, []
    for i, c in enumerate(problem["cases"]):
        cname = "case_%d" % i
        checkers[cname] = (
            "assert result == %r, '结果不符，实际 ' + repr(result)" % (c["expect"],)
        )
        cases.append({"name": c["name"], "args": c["args"], "checker": cname})
    fn = problem["function_name"]
    return {
        "label": problem.get("label", problem["suite_key"]),
        "function_names": [fn] + _camel_variants(fn),
        "class_names": ["Solution"],
        "checkers": checkers,
        "cases": cases,
    }


def register_problem(problem):
    """运行时把题目套件注入 evaluation.TEST_SUITES（幂等，不改产品文件）。"""
    import evaluation
    evaluation.TEST_SUITES[problem["suite_key"]] = to_suite(problem)


def register_problems(problems):
    for p in problems:
        register_problem(p)
    return len(problems)


def run_one(mode, problem, **kwargs):
    """统一入口：按 mode 分派，返回 RunResult。"""
    register_problem(problem)          # 必须先注册，否则引擎报"套件不存在"
    started = time.time()
    reply, verification, attempts = "", None, 1

    if mode == "single":
        reply, verification = run_single(
            problem,
            provider=kwargs.get("provider", "deepseek"),
            temperature=kwargs.get("temperature"),
            max_tokens=kwargs.get("max_tokens"),
        )
    elif mode == "single_self_correct":
        reply, verification, attempts = run_single_self_correct(
            problem,
            provider=kwargs.get("provider", "deepseek"),
            temperature=kwargs.get("temperature"),
            max_retries=kwargs.get("max_retries", 2),
            max_tokens=kwargs.get("max_tokens"),
        )
    elif mode == "homogeneous":
        # 两个参与者都用 --provider 指定的同一厂商（默认 deepseek）。
        # 传 --provider zhipu 即「智谱 × 2」，可用免费档跑零成本主实验。
        same = kwargs.get("provider", "deepseek")
        reply, verification = run_debate(
            problem, providers=(same, same),
            rounds=kwargs.get("rounds", 3),
            summarizer_provider=same,
            temperature=kwargs.get("temperature"),
        )
    elif mode == "heterogeneous":
        reply, verification = run_debate(
            problem, providers=("deepseek", "zhipu"),
            rounds=kwargs.get("rounds", 3),
            summarizer_provider=kwargs.get("provider", "deepseek"),
            temperature=kwargs.get("temperature"),
        )
    else:
        raise ValueError("未知 mode: %r，可选 %s" % (mode, MODES))

    status = _g(verification, "status") or "unknown"
    err_class = classify_failure(verification)
    return RunResult(
        mode=mode,
        suite_key=problem["suite_key"],
        difficulty=problem.get("difficulty", "?"),
        passed=bool(_g(verification, "passed")),
        status=status,
        error_class=err_class,
        attempts=attempts,
        elapsed_sec=round(time.time() - started, 2),
        code_len=len(_g(verification, "code") or ""),
        detail=(_g(verification, "error") or "")[:300],
        code=(_g(verification, "code") or ""),
    )


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="四配置对照实验执行器")
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("--limit", type=int, default=3, help="跑前 N 道题（冒烟用，默认 3）")
    ap.add_argument("--difficulty", default=None,
                    help="只跑指定难度，逗号分隔，如 easy,hard")
    ap.add_argument("--rounds", type=int, default=3, help="讨论轮数（debate 模式）")
    ap.add_argument("--max-retries", type=int, default=2, help="自纠重试上限")
    ap.add_argument("--provider", default="deepseek", help="single/自纠与总结者用的厂商")
    ap.add_argument("--out", default=None, help="结果落盘路径（JSONL）")
    ap.add_argument("--dry-run", action="store_true", help="只打印题面与配置，不调 API")
    args = ap.parse_args(argv)

    difficulties = args.difficulty.split(",") if args.difficulty else None
    problems = load_problems(difficulties=difficulties, limit=args.limit)
    print("加载题目：%d 道（mode=%s）" % (len(problems), args.mode))

    if args.dry_run:
        for p in problems:
            print("\n" + "=" * 70)
            print("%s | %s | %s" % (p["suite_key"], p["difficulty"], p["label"]))
            print("-" * 70)
            print(build_solver_prompt(p))
        return 0

    kwargs = dict(rounds=args.rounds, max_retries=args.max_retries,
                  provider=args.provider)
    results = []
    for i, p in enumerate(problems, 1):
        print("\n[%d/%d] %s (%s)" % (i, len(problems), p["suite_key"], p["difficulty"]))
        try:
            r = run_one(args.mode, p, **kwargs)
        except Exception as exc:
            print("  [异常] %s: %s" % (type(exc).__name__, exc))
            r = RunResult(mode=args.mode, suite_key=p["suite_key"],
                          difficulty=p.get("difficulty", "?"), passed=False,
                          status="exception", error_class="unknown",
                          attempts=1, elapsed_sec=0.0,
                          detail="%s: %s" % (type(exc).__name__, exc))
        results.append(r)
        mark = "PASS" if r.passed else "FAIL"
        print("  %s | status=%s | err=%s | attempts=%d | %.1fs"
              % (mark, r.status, r.error_class, r.attempts, r.elapsed_sec))

    passed = sum(1 for r in results if r.passed)
    print("\n" + "=" * 70)
    print("结果：%d/%d 通过" % (passed, len(results)))
    from collections import Counter
    for cls, n in Counter(r.error_class for r in results).most_common():
        print("  %-28s %d" % (cls, n))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
        print("\n已追加写入：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
