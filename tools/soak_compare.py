"""两次浸泡跑（旧台账 vs 新一轮）的逐项对比。

为什么单独写一个：`event_audit.py` 只回答「这一批健不健康」，不回答「改完以后
比改之前好多少」。返工类指标可以复用它的判据，机制触发类指标要看原始回包
（`process[].out`），所以这里两路都读。

用法：
    python tools/soak_compare.py \
        --base "evals/results/soak*.json" \
        --new "evals/results/fix*.json"

判据与口径全部来自 `tools/event_audit.py`（一次调用只归一类、只认工具自己写的行）；
机制触发只统计回包里出现的权威字样，不从脚本文本猜。
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_audit as ea  # noqa: E402

# 机制触发字样 → 含义。都取自回包（工具/闸门自己打印的），不解析脚本源码。
MARKERS: list[tuple[str, str, str]] = [
    ("参数不对", "param_hint", "工具回包「参数不对」——签名/参数名发错"),
    ("已是最新内容", "idem_block", "幂等层拦下逐字节相同的重写"),
    ("缺 base_version", "occ_block", "OCC 拦下无版本号的整写"),
    ("本次放行", "fail_open", "连拦两次后放行"),
    ("已按同名参数处理", "alias_fix", "纯改名别名自动改对"),
    ("[脚本异常]", "script_exc", "PTC 脚本抛异常"),
    ("0 次工具调用", "noop", "脚本执行了但没调到工具"),
    ("未找到待替换文本", "edit_miss", "edit_file 没定位到 old_text"),
    ("存在歧义", "edit_ambig", "edit_file 命中多处"),
    ("内容未变", "unchanged", "回执告知内容与上次相同"),
]


def _load(paths: list[Path]) -> list[dict]:
    cases: list[dict] = []
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for c in d.get("cases") or []:
            c = dict(c)
            c["_file"] = p.stem
            cases.append(c)
    return cases


def _markers(cases: list[dict]) -> dict[str, int]:
    cnt: dict[str, int] = {k: 0 for _t, k, _d in MARKERS}
    for c in cases:
        blob = "\n".join(str(e.get("out") or "") for e in c.get("process") or [])
        for text, key, _d in MARKERS:
            if text in blob:
                cnt[key] += 1
        if any(e.get("name") == "find_tools" for e in c.get("process") or []):
            cnt.setdefault("find_tools", 0)
            cnt["find_tools"] += 1
    return cnt


def _row(label: str, cases: list[dict]) -> dict:
    # 直接复用 event_audit 的单次判据，避免两套口径
    stats = [ea.audit_run(c, seed_names=set()) for c in cases]
    n = len(stats) or 1
    dur = [s["duration"] for s in stats]
    turns = [s["turns"] for s in stats]
    calls = [s["calls"] for s in stats]
    return {
        "label": label,
        "n": len(stats),
        "pass": sum(1 for s in stats if s["verdict"] == "pass") / n,
        "clean": sum(1 for s in stats if s["clean"]) / n,
        "clean_err": sum(1 for s in stats if s["error_n"] == 0) / n,
        "err": sum(s["error_n"] for s in stats) / n,
        "red": sum(s["redundant_n"] for s in stats) / n,
        "turns": statistics.median(turns) if turns else 0,
        "calls": statistics.median(calls) if calls else 0,
        "dur": statistics.median(dur) if dur else 0,
        "dur_all": sum(dur),
        "markers": _markers(cases),
        "by_case": {cid: [s for s in stats if s["id"] == cid]
                    for cid in dict.fromkeys(s["id"] for s in stats)},
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def report(base: dict, new: dict, base_paths: list[str], new_paths: list[str]) -> str:
    L: list[str] = []
    L.append("# 两批浸泡跑对比\n")
    L.append(f"- 旧：`{base_paths}`，{base['n']} 次运行")
    L.append(f"- 新：`{new_paths}`，{new['n']} 次运行")
    L.append("- 口径与 `tools/event_audit.py` 完全一致（一次调用只归一类；"
             "写盘只认「系统核对」块）\n")

    L.append("## 总量\n")
    L.append("| 指标 | 旧 | 新 | 变化 |")
    L.append("| --- | ---: | ---: | ---: |")
    rows = [
        ("判定通过率", _pct(base["pass"]), _pct(new["pass"]),
         f"{(new['pass'] - base['pass']) * 100:+.1f}pt"),
        ("零返工运行占比", _pct(base["clean"]), _pct(new["clean"]),
         f"{(new['clean'] - base['clean']) * 100:+.1f}pt"),
        ("零错误运行占比", _pct(base["clean_err"]), _pct(new["clean_err"]),
         f"{(new['clean_err'] - base['clean_err']) * 100:+.1f}pt"),
        ("错误类返工 / 次", f"{base['err']:.2f}", f"{new['err']:.2f}",
         f"{new['err'] - base['err']:+.2f}"),
        ("冗余类返工 / 次", f"{base['red']:.2f}", f"{new['red']:.2f}",
         f"{new['red'] - base['red']:+.2f}"),
        ("回合 中位", f"{base['turns']:.0f}", f"{new['turns']:.0f}",
         f"{new['turns'] - base['turns']:+.0f}"),
        ("调用 中位", f"{base['calls']:.0f}", f"{new['calls']:.0f}",
         f"{new['calls'] - base['calls']:+.0f}"),
        ("单次耗时 中位 (s)", f"{base['dur']:.0f}", f"{new['dur']:.0f}",
         f"{(new['dur'] - base['dur']) / base['dur'] * 100:+.0f}%"
         if base["dur"] else "-"),
        ("总墙钟 (min)", f"{base['dur_all'] / 60:.0f}",
         f"{new['dur_all'] / 60:.0f}", "-"),
    ]
    for r in rows:
        L.append("| {} | {} | {} | {} |".format(*r))
    L.append("")

    L.append("## 分任务\n")
    L.append("| 任务 | 运行(旧/新) | 回合 中位 | 调用 中位 | 耗时 中位 (s) | 零返工 | 错误类/次 | 冗余类/次 |")
    L.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for cid in base["by_case"]:
        b = base["by_case"][cid]
        n = new["by_case"].get(cid) or []
        def med(xs, f):
            return statistics.median([f(x) for x in xs]) if xs else float("nan")
        L.append("| {} | {}/{} | {:.0f} → {:.0f} | {:.0f} → {:.0f} | {:.0f} → {:.0f} "
                 "| {}/{} → {}/{} | {:.2f} → {:.2f} | {:.2f} → {:.2f} |".format(
                     cid, len(b), len(n),
                     med(b, lambda x: x["turns"]), med(n, lambda x: x["turns"]),
                     med(b, lambda x: x["calls"]), med(n, lambda x: x["calls"]),
                     med(b, lambda x: x["duration"]), med(n, lambda x: x["duration"]),
                     sum(1 for x in b if x["clean"]), len(b),
                     sum(1 for x in n if x["clean"]), len(n),
                     sum(x["error_n"] for x in b) / max(1, len(b)),
                     sum(x["error_n"] for x in n) / max(1, len(n)),
                     sum(x["redundant_n"] for x in b) / max(1, len(b)),
                     sum(x["redundant_n"] for x in n) / max(1, len(n))))
    L.append("")

    L.append("## 机制触发（回包里出现即计一次运行）\n")
    L.append("| 机制 | 旧 | 新 | 含义 |")
    L.append("| --- | ---: | ---: | --- |")
    keys = [k for _t, k, _d in MARKERS] + ["find_tools"]
    desc = {k: d for _t, k, d in MARKERS}
    desc["find_tools"] = "调 find_tools 打听工具名/参数"
    for k in keys:
        L.append(f"| `{k}` | {base['markers'].get(k, 0)} | "
                 f"{new['markers'].get(k, 0)} | {desc[k]} |")
    L.append("")

    bad = [s for cid in new["by_case"] for s in new["by_case"][cid] if not s["clean"]]
    L.append(f"## 新一轮里仍有返工的运行（{len(bad)} 次）\n")
    if bad:
        L.append("| 轮次 | 任务 | 回合 | 调用 | 判定 | 返工 |")
        L.append("| --- | --- | ---: | ---: | --- | --- |")
        for s in bad:
            detail = "；".join(
                (f"s{w['step']} " if w["step"] not in ("-", None, "") else "")
                + w["kind"] for w in s["waste"])
            L.append(f"| {s.get('round', '-')} | {s['id']} | {s['turns']} | {s['calls']} "
                     f"| {s['verdict']}/{s['layer']} | {detail} |")
    else:
        L.append("- 无")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="两批浸泡跑对比")
    ap.add_argument("--base", default=str(RESULTS / "soak*.json"))
    ap.add_argument("--new", default=str(RESULTS / "fix*.json"))
    ap.add_argument("--md", default="", help="把报告写进这个文件")
    args = ap.parse_args()

    b_paths = sorted(Path(p) for p in glob.glob(args.base))
    n_paths = sorted(Path(p) for p in glob.glob(args.new))
    if not b_paths or not n_paths:
        print(f"结果文件不足：base={len(b_paths)} new={len(n_paths)}", file=sys.stderr)
        return 2
    base = _row("旧", _load(b_paths))
    new = _row("新", _load(n_paths))
    text = report(base, new, args.base, args.new)
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
        print(f"写入 {args.md}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
