"""A/B 对比：比较两次评测跑批的结果，回答"改了之后到底变好没有"。

运行：python tools/bench_compare.py baseline new [--verbose]

输出三层：
1. 逐任务对比：pass/fail 翻转一目了然（这就是换配置的证据）
2. 汇总：各自通过数、翻转计数（修复数/退化数）、平均耗时
3. 失败层分布对比：退化集中在哪一层，就说明问题出在哪个环节
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent.parent / "evals" / "results"


def load(label: str) -> dict:
    fp = RESULTS_DIR / f"{label}.json"
    if not fp.exists():
        sys.exit(f"找不到结果文件：{fp}\n先跑：python tools/bench_tasks.py --label {label}")
    return json.loads(fp.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="两次评测跑批对比")
    ap.add_argument("a", help="基线 label")
    ap.add_argument("b", help="对比 label")
    ap.add_argument("--verbose", action="store_true", help="列出全部任务（默认只列有变化的）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ra, rb = load(args.a), load(args.b)
    ma = {c["id"]: c for c in ra["cases"]}
    mb = {c["id"]: c for c in rb["cases"]}
    ids = [c["id"] for c in ra["cases"]] + [i for i in mb if i not in ma]

    print(f"{'任务':<20} {'判定':<12} {'层(归因)':<24} 耗时对比")
    print("-" * 78)
    fixed, broke, only_a, only_b = [], [], [], []
    for cid in ids:
        ca, cb = ma.get(cid), mb.get(cid)
        va = ca["verdict"] if ca else "-"
        vb = cb["verdict"] if cb else "-"
        la = ca.get("layer", "-") if ca else "-"
        lb = cb.get("layer", "-") if cb else "-"
        if va == vb == "pass" and not args.verbose:
            continue
        tag = ""
        if va == "fail" and vb == "pass":
            tag = "  ← 修复"
            fixed.append(cid)
        elif va == "pass" and vb == "fail":
            tag = "  ← 退化"
            broke.append(cid)
        elif va == "pass" and not cb:
            tag = "  ← B 缺跑"
            only_a.append(cid)
        elif vb == "pass" and not ca:
            tag = "  ← A 缺跑"
            only_b.append(cid)
        dur = f"{ca.get('duration','?')}s → {cb.get('duration','?')}s" if ca and cb else "-"
        print(f"{cid:<20} {va + ' → ' + vb:<12} {la + ' → ' + lb:<24} {dur}{tag}")

    pa = sum(1 for c in ma.values() if c["verdict"] == "pass")
    pb = sum(1 for c in mb.values() if c["verdict"] == "pass")
    print("-" * 78)
    print(f"通过：{args.a} {pa}/{len(ma)}   {args.b} {pb}/{len(mb)}")
    print(f"翻转：修复 {len(fixed)} 个 {fixed or ''}；退化 {len(broke)} 个 {broke or ''}")
    if only_a or only_b:
        print(f"缺席：A 缺 {only_a or '无'}；B 缺 {only_b or '无'}（缺席不计入胜负）")

    fa = Counter(c.get("layer") for c in ma.values() if c["verdict"] == "fail")
    fb = Counter(c.get("layer") for c in mb.values() if c["verdict"] == "fail")
    if fa or fb:
        print(f"失败层分布：{args.a} {dict(fa)}  →  {args.b} {dict(fb)}")
        print("（crash/empty = 管线或拆解问题；exact/norm/numeric/fuzzy = 执行质量；"
              "check = 说得好但产物是错的）")

    da = [c.get("duration", 0) for c in ma.values()]
    db = [c.get("duration", 0) for c in mb.values()]
    if da and db:
        print(f"平均耗时：{args.a} {round(sum(da)/len(da),1)}s  →  {args.b} {round(sum(db)/len(db),1)}s")

    if pa == pb and not fixed and not broke:
        print("\n结论：两版打平——按「脚手架只减不增」原则，保留更简单的那个。")
    elif pb > pa and not broke:
        print(f"\n结论：{args.b} 严格更优（只修不破）。")
    elif pb > pa:
        print(f"\n结论：{args.b} 净收益为正（修复 {len(fixed)} / 退化 {len(broke)}），逐条看过退化再定。")
    else:
        print(f"\n结论：{args.a} 更优或互有胜负——退化集中在哪一层，就回哪一层找原因。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
