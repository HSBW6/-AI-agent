#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集成自检：验证「数据集 → 套件转换 → 评测引擎 → 沙箱 → 断言」整条链路全通。

做法：把每道题的 `reference`（人工核对过的参考实现）当作"模型输出的代码"，
包成 ```python 代码块喂给真实评测引擎 `run_code_verification`，要求全部 PASS。

为什么需要它：
  `validate_dataset.py` 是直接 exec 参考实现比对 expect，**绕过了评测引擎**；
  而 runner 跑实验时走的是引擎（TEST_SUITES + 沙箱 + checker 断言）。
  两边格式不同（数据集用 expect 值，引擎要 checker 模板），只测一边会漏掉
  "单元验证通过、集成跑不通"。本脚本专门堵这个缝。

不调用任何 API、不花钱。改过数据集、to_suite 转换或评测引擎后都该跑一遍。

退出码：全部通过 = 0；否则 = 1。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from runner import load_problems, register_problem  # noqa: E402
from evaluation import run_code_verification  # noqa: E402

CODE_FENCE = "```python\n{}\n```"


def main():
    problems = load_problems()
    print("=" * 68)
    print("集成自检：%d 道题的 reference 过真实评测引擎" % len(problems))
    print("=" * 68)

    passed, failures = 0, []
    for i, p in enumerate(problems, 1):
        register_problem(p)
        text = CODE_FENCE.format(p["reference"])
        try:
            r = run_code_verification(text, suite_name=p["suite_key"])
        except Exception as exc:                      # 引擎自身异常也要暴露
            failures.append((p["suite_key"], "engine_exception",
                             "%s: %s" % (type(exc).__name__, exc)))
            print("[%2d/%d] %-32s ENGINE-ERROR" % (i, len(problems), p["suite_key"]))
            continue
        if r.passed:
            passed += 1
            print("[%2d/%d] %-32s PASS  用例 %d/%d"
                  % (i, len(problems), p["suite_key"],
                     sum(1 for t in r.tests if t.passed), len(r.tests)))
        else:
            failures.append((p["suite_key"], r.status, str(r.error)[:120]))
            print("[%2d/%d] %-32s FAIL  status=%s"
                  % (i, len(problems), p["suite_key"], r.status))

    print("\n" + "=" * 68)
    if not failures:
        print("结果：%d/%d 通过 —— 评测链路完好 ✅" % (passed, len(problems)))
        return 0
    print("结果：%d/%d 通过 —— 有 %d 题未通过 ❌" % (passed, len(problems), len(failures)))
    for key, status, err in failures:
        print("  ✗ %-32s status=%-12s %s" % (key, status, err))
    return 1


if __name__ == "__main__":
    sys.exit(main())
