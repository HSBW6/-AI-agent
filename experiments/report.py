#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把实验结果 JSONL 汇总成统计表（Markdown），直接用于结论与 README。

用法：
    python experiments/report.py                          # 汇总 results/ 下所有 jsonl
    python experiments/report.py experiments/results/groupA.jsonl
    python experiments/report.py --single single --baseline single

输出四张表：
  1. 总览：每个配置的通过率 / 平均耗时 / 平均尝试次数
  2. 分层：按难度（easy/medium/hard）拆开看通过率
  3. 失败分类分布：区分"接口问题"与"算法问题"
  4. 配对对比：同一道题在 baseline 与目标配置下的胜负变化
     （讨论救回来的题 / 被讨论弄坏的题 / 无变化）—— 这是最有洞察力的一张表
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "experiments" / "results"

# 接口类失败（没按题面要求的函数名/签名写）与算法类失败要分开解读
INTERFACE_CLASSES = {"interface_function_missing", "interface_arg_mismatch"}
FORMAT_CLASSES = {"no_code", "syntax_error"}


def load_rows(paths):
    rows = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            print("[警告] 结果文件不存在：%s" % p, file=sys.stderr)
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception as exc:
                print("[警告] 跳过损坏记录：%s" % exc, file=sys.stderr)
    # 去重：同一 (mode, suite_key) 只保留最后一条。
    # 免费档限流中断后重跑会往同一个 JSONL 追加记录，不去重会导致重复计数。
    dedup = {}
    for r in rows:
        dedup[(r.get("mode"), r.get("suite_key"))] = r
    dropped = len(rows) - len(dedup)
    if dropped:
        print("（已去重 %d 条重复记录，保留每题每配置的最新一次）" % dropped)
    return list(dedup.values())


def pct(a, b):
    return "%.1f%%" % (100.0 * a / b) if b else "n/a"


def by_mode(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[r.get("mode", "?")].append(r)
    return groups


def table_overview(groups):
    print("\n## 1. 总览\n")
    print("| 配置 | 题数 | 通过 | 通过率 | 平均耗时 | 平均尝试 | 说明 |")
    print("|---|---|---|---|---|---|---|")
    for mode, rs in groups.items():
        n = len(rs)
        ok = sum(1 for r in rs if r.get("passed"))
        avg_t = sum(r.get("elapsed_sec") or 0 for r in rs) / n if n else 0
        avg_a = sum(r.get("attempts") or 1 for r in rs) / n if n else 0
        print("| `%s` | %d | %d | **%s** | %.1fs | %.2f | |" % (mode, n, ok, pct(ok, n), avg_t, avg_a))


def table_by_difficulty(groups):
    print("\n## 2. 按难度分层\n")
    diffs = ["easy", "medium", "hard"]
    header = "| 配置 | " + " | ".join(diffs) + " |"
    print(header)
    print("|---" * (len(diffs) + 1) + "|")
    for mode, rs in groups.items():
        cells = []
        for d in diffs:
            sub = [r for r in rs if r.get("difficulty") == d]
            ok = sum(1 for r in sub if r.get("passed"))
            cells.append("%d/%d (%s)" % (ok, len(sub), pct(ok, len(sub))))
        print("| `%s` | %s |" % (mode, " | ".join(cells)))


def table_failures(groups):
    print("\n## 3. 失败分类分布\n")
    print("| 配置 | " + " | ".join(["通过"] + sorted({r.get("error_class") for rs in groups.values() for r in rs if r.get("error_class") != "passed"})) + " |")
    classes = sorted({r.get("error_class") for rs in groups.values() for r in rs if r.get("error_class") != "passed"})
    print("|---" * (len(classes) + 2) + "|")
    for mode, rs in groups.items():
        c = Counter(r.get("error_class") for r in rs)
        cells = [str(c.get("passed", 0))] + [str(c.get(k, 0)) for k in classes]
        print("| `%s` | %s |" % (mode, " | ".join(cells)))
    if classes:
        print("\n分类含义：`interface_*` = 未按题面接口写（**不算算法能力问题**）；"
              "`no_code`/`syntax_error` = 输出格式问题；`assertion_failed` = 算法真的错了。")


def table_paired(groups, baseline, target):
    if baseline not in groups or target not in groups:
        return
    base = {r["suite_key"]: r for r in groups[baseline]}
    targ = {r["suite_key"]: r for r in groups[target]}
    common = sorted(set(base) & set(targ))
    if not common:
        return
    fixed = [k for k in common if not base[k].get("passed") and targ[k].get("passed")]
    broken = [k for k in common if base[k].get("passed") and not targ[k].get("passed")]
    both_ok = [k for k in common if base[k].get("passed") and targ[k].get("passed")]
    both_bad = [k for k in common if not base[k].get("passed") and not targ[k].get("passed")]

    print("\n## 4. 配对对比：`%s` vs `%s`（同一批题，%d 道）\n" % (baseline, target, len(common)))
    print("| 变化 | 题数 | 题号 |")
    print("|---|---|---|")
    print("| ✅ 讨论**救回来** | %d | %s |" % (len(fixed), ", ".join(fixed) or "—"))
    print("| ❌ 讨论**弄坏了** | %d | %s |" % (len(broken), ", ".join(broken) or "—"))
    print("| 两者都对 | %d | %s |" % (len(both_ok), ", ".join(both_ok) or "—"))
    print("| 两者都错 | %d | %s |" % (len(both_bad), ", ".join(both_bad) or "—"))

    if fixed or broken:
        net = len(fixed) - len(broken)
        print("\n**净收益：%+d 题**（救回 %d，弄坏 %d）" % (net, len(fixed), len(broken)))
        # 配对符号检验（小样本下比率的直觉补充）
        n_disc = len(fixed) + len(broken)
        if n_disc:
            print("（在 %d 道出现差异的题里，讨论方向为正的比例 = %.0f%%）"
                  % (n_disc, 100.0 * len(fixed) / n_disc))


def main(argv=None):
    ap = argparse.ArgumentParser(description="实验结果汇总")
    ap.add_argument("paths", nargs="*", help="结果 JSONL；缺省为 results/ 下全部")
    ap.add_argument("--baseline", default="single", help="配对对比的基准配置")
    ap.add_argument("--target", default="homogeneous", help="配对对比的目标配置")
    args = ap.parse_args(argv)

    paths = args.paths or sorted(RESULTS_DIR.glob("*.jsonl"))
    if not paths:
        print("没有找到任何结果文件（%s/*.jsonl）" % RESULTS_DIR)
        return 1

    rows = load_rows(paths)
    if not rows:
        print("结果文件为空。")
        return 1

    print("# 对照实验统计报告")
    print("\n数据来源：%s" % "、".join(Path(p).name for p in paths))
    print("总记录数：%d" % len(rows))
    groups = by_mode(rows)
    print("配置：%s" % "、".join("`%s`(%d)" % (m, len(rs)) for m, rs in groups.items()))

    table_overview(groups)
    table_by_difficulty(groups)
    table_failures(groups)
    table_paired(groups, args.baseline, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
