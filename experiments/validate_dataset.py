#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 experiments/dataset_raw.json（算法题数据集）。

用途：作为数据集交付的验收闸门。全 PASS 才算交付完成。

检查项：
  1. JSON 可解析、顶层是数组、恰好 10 个元素
  2. 每题字段齐全、类型正确、取值合法（难度枚举、prompt 长度等）
  3. suite_key / function_name 全局唯一、不重复
  4. 难度分布 = easy 3 / medium 4 / hard 3
  5. reference 不含 import / input / print / open / eval / exec / __import__
  6. reference 能在**受限命名空间**里 exec 并取出 function_name 对应的函数
  7. 每个用例 fn(*args) 的实际返回值 == expect（精确相等）
  8. 每题 3~5 个用例，且至少一个用例名含「边界」
  9. expect 类型限定为 int / str / bool / list（避免 float 精度与 None 歧义）

退出码：全部通过 = 0；否则 = 1。
"""

import builtins
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 默认校验 Marvis 的交付物；也可传入路径校验其它数据集（如自测样本）
DATASET_PATH = (
    Path(sys.argv[1]).resolve()
    if len(sys.argv) > 1
    else ROOT / "experiments" / "dataset_raw.json"
)
# 期望规模可用命令行覆盖（多批数据集难度分布不同）：
#   validate_dataset.py <path> [题目总数] [easy] [medium] [hard]
TOTAL_EXPECTED = int(sys.argv[2]) if len(sys.argv) > 2 else 10
if len(sys.argv) >= 6:
    EXPECTED_DIFF = {
        "easy": int(sys.argv[3]),
        "medium": int(sys.argv[4]),
        "hard": int(sys.argv[5]),
    }
else:
    EXPECTED_DIFF = {"easy": 3, "medium": 4, "hard": 3}

REQUIRED_FIELDS = [
    "suite_key", "label", "difficulty", "function_name",
    "signature", "prompt", "reference", "cases",
]
BANNED_PATTERNS = [
    (r"^\s*import\s+\w", "含有 import（沙箱禁用）"),
    (r"^\s*from\s+\w+\s+import", "含有 from-import（沙箱禁用）"),
    (r"__import__", "使用了 __import__"),
    (r"\binput\s*\(", "使用了 input()"),
    (r"\bprint\s*\(", "使用了 print()"),
    (r"\bopen\s*\(", "使用了 open()"),
    (r"\beval\s*\(", "使用了 eval()"),
    (r"\bexec\s*\(", "使用了 exec()"),
]

# exec 环境回退白名单（与项目沙箱同源的意图：不给文件/网络/动态执行能力）
_FALLBACK_NAMES = [
    "abs", "all", "any", "bin", "bool", "chr", "dict", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "getattr", "hasattr", "hash",
    "hex", "int", "isinstance", "issubclass", "iter", "len", "list", "map",
    "max", "min", "next", "oct", "ord", "pow", "range", "repr", "reversed",
    "round", "set", "setattr", "slice", "sorted", "str", "sum", "tuple",
    "type", "zip", "Exception", "ValueError", "TypeError", "KeyError",
    "IndexError", "ZeroDivisionError", "RuntimeError", "StopIteration",
]


def load_sandbox_builtins():
    """优先复用项目真实沙箱白名单，保证与评测环境一致。"""
    sys.path.insert(0, str(ROOT))
    try:
        from evaluation import SANDBOX_BUILTINS  # type: ignore

        if isinstance(SANDBOX_BUILTINS, dict):
            return dict(SANDBOX_BUILTINS)
        return {n: getattr(builtins, n) for n in SANDBOX_BUILTINS if hasattr(builtins, n)}
    except Exception:
        return {n: getattr(builtins, n) for n in _FALLBACK_NAMES if hasattr(builtins, n)}


SANDBOX = load_sandbox_builtins()


def build_env():
    # __name__ 必须提供：被测代码若在函数内部定义 class，class 体的执行需要
    # __name__ 来设置 __module__，缺失会抛 NameError（Marvis 首轮验证实测到）。
    # 项目真实沙箱 runner 里同样提供它，这里必须对齐——否则数据集会出现
    # "验证器能过、真实评测报错"或反过来的环境不一致。
    return {"__builtins__": dict(SANDBOX), "__name__": "__sandbox__"}


class ItemResult:
    def __init__(self, index, item):
        self.index = index
        self.item = item if isinstance(item, dict) else {}
        self.errors = []
        self.case_total = 0
        self.case_passed = 0

    @property
    def name(self):
        return str(self.item.get("suite_key") or "(缺 suite_key)")

    @property
    def ok(self):
        return not self.errors


def check_shape(item):
    """字段级校验，返回错误列表。"""
    errs = []
    for field in REQUIRED_FIELDS:
        if field not in item:
            errs.append("缺字段 %s" % field)
    if errs:
        return errs
    for field in ("suite_key", "label", "function_name", "signature", "prompt", "reference"):
        if not isinstance(item[field], str) or not item[field].strip():
            errs.append("%s 必须是非空字符串" % field)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", item.get("suite_key", "")):
        errs.append("suite_key 必须是小写下划线英文：%r" % item.get("suite_key"))
    if not re.fullmatch(r"[a-z][a-z0-9_]*", item.get("function_name", "")):
        errs.append("function_name 必须是小写下划线英文：%r" % item.get("function_name"))
    if item.get("difficulty") not in EXPECTED_DIFF:
        errs.append("difficulty 必须是 easy/medium/hard，实际 %r" % item.get("difficulty"))
    prompt = item.get("prompt", "")
    if isinstance(prompt, str) and not (150 <= len(prompt) <= 700):
        errs.append("prompt 长度应在 150~700 字，实际 %d" % len(prompt))
    if item["function_name"] not in item["reference"]:
        errs.append("reference 里找不到函数名 %s" % item["function_name"])
    for pattern, why in BANNED_PATTERNS:
        if re.search(pattern, item["reference"], re.M):
            errs.append("reference %s" % why)
    return errs


def check_cases(item, res):
    """逐个用例实跑，比对 expect。"""
    cases = item.get("cases")
    if not isinstance(cases, list):
        res.errors.append("cases 必须是数组")
        return
    if not (3 <= len(cases) <= 5):
        res.errors.append("用例数必须在 3~5，实际 %d" % len(cases))
    if not any("边界" in str(c.get("name", "")) for c in cases if isinstance(c, dict)):
        res.errors.append("缺少 name 含「边界」的用例")

    env = build_env()
    try:
        ns = {}
        exec(compile(item["reference"], "<reference>", "exec"), env, ns)
    except Exception as exc:
        res.errors.append("reference 执行失败：%s: %s" % (type(exc).__name__, exc))
        return
    fn = ns.get(item["function_name"])
    if not callable(fn):
        res.errors.append("reference 里没定义可调用函数 %s" % item["function_name"])
        return

    res.case_total = len(cases)
    for i, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            res.errors.append("第 %d 个用例不是对象" % i)
            continue
        label = case.get("name") or ("用例%d" % i)
        if "args" not in case or not isinstance(case["args"], list):
            res.errors.append("[%s] args 必须是数组（只放函数实参）" % label)
            continue
        if "expect" not in case:
            res.errors.append("[%s] 缺 expect" % label)
            continue
        expect = case["expect"]
        if not isinstance(expect, (int, str, bool, list)):
            res.errors.append(
                "[%s] expect 只允许 int/str/bool/list（避免 float 精度与 None 歧义），实际 %s"
                % (label, type(expect).__name__)
            )
            continue
        try:
            actual = fn(*case["args"])
        except Exception as exc:
            res.errors.append("[%s] 调用抛异常：%s: %s" % (label, type(exc).__name__, exc))
            continue
        if actual != expect:
            res.errors.append(
                "[%s] 实测 %r != expect %r （args=%r）" % (label, actual, expect, case["args"])
            )
            continue
        res.case_passed += 1


def main():
    print("=" * 66)
    print("数据集验收：%s" % DATASET_PATH)
    print("=" * 66)

    if not DATASET_PATH.exists():
        print("\n[FAIL] 文件不存在。请先按任务书生成该文件。")
        return 1

    try:
        data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print("\n[FAIL] JSON 解析失败：%s" % exc)
        return 1

    if not isinstance(data, list):
        print("\n[FAIL] 顶层必须是 JSON 数组（元素为题目对象）。")
        return 1

    global_errors = []
    if len(data) != TOTAL_EXPECTED:
        global_errors.append("题目数应为 %d，实际 %d" % (TOTAL_EXPECTED, len(data)))

    results = []
    seen_keys, seen_fns = {}, {}
    for idx, item in enumerate(data, 1):
        res = ItemResult(idx, item)
        if not isinstance(item, dict):
            res.errors.append("元素不是对象")
        else:
            res.errors.extend(check_shape(item))
            key = item.get("suite_key")
            fn = item.get("function_name")
            if key in seen_keys:
                res.errors.append("suite_key 与第 %d 题重复" % seen_keys[key])
            elif key:
                seen_keys[key] = idx
            if fn in seen_fns:
                res.errors.append("function_name 与第 %d 题重复" % seen_fns[fn])
            elif fn:
                seen_fns[fn] = idx
            if not [e for e in res.errors if "缺字段" in e]:
                check_cases(item, res)
        results.append(res)

    diff_count = {}
    for res in results:
        d = res.item.get("difficulty")
        if d in EXPECTED_DIFF:
            diff_count[d] = diff_count.get(d, 0) + 1

    print()
    for res in results:
        head = "[%2d/%d] %-26s %-10s %s" % (
            res.index, len(data), res.name, res.item.get("difficulty", "?"),
            "PASS" if res.ok else "FAIL",
        )
        print(head)
        if res.item.get("label"):
            print("        %s" % res.item["label"])
        if res.case_total:
            print("        用例 %d/%d 通过" % (res.case_passed, res.case_total))
        for err in res.errors:
            print("        ✗ %s" % err)

    print("\n" + "=" * 66)
    passed = sum(1 for r in results if r.ok)
    print("难度分布：easy=%d medium=%d hard=%d（要求 %d/%d/%d）"
          % (diff_count.get("easy", 0), diff_count.get("medium", 0), diff_count.get("hard", 0),
             EXPECTED_DIFF["easy"], EXPECTED_DIFF["medium"], EXPECTED_DIFF["hard"]))
    for d, want in EXPECTED_DIFF.items():
        if diff_count.get(d, 0) != want:
            global_errors.append("难度 %s 应为 %d 题，实际 %d" % (d, want, diff_count.get(d, 0)))
    for err in global_errors:
        print("✗ %s" % err)

    total = len(results)
    if passed == total and not global_errors and total == TOTAL_EXPECTED:
        print("\n结果：%d/%d PASS —— 数据集验收通过 ✅" % (passed, total))
        return 0
    print("\n结果：%d/%d PASS —— 未通过，请按上面 ✗ 提示逐条修复 ❌" % (passed, total))
    return 1


if __name__ == "__main__":
    sys.exit(main())
