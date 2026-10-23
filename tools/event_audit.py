"""事件流体检：量「每轮做了多少步、其中多少步是返工」。

评测只回 pass/fail，回答不了「这轮是不是在正确步骤上」。本工具读
`evals/results/<label>.json` 里的过程记录（rec["process"]），把一次运行拆成
「模型回合（turn）」和「工具调用（call）」，再按可核对的证据给每次调用定性。

判据必须可核对（handoff §7：闸门只能证伪，不能误伤、不能补信号）：
每条规则都只认工具自己写进 out 的那一行，不猜、不数语气。

    noop_script out 里是「[ops_batch] 0 次工具调用」——脚本跑了但一次工具都没调到
    param_err   out 里有「[参数] X 接受：」——签名发错，这一步必然要重来
    edit_miss   out 里有「未找到待替换文本」或「找到 N 处匹配，存在歧义」
    write_fail  【系统核对·写盘结果】块里出现「写入失败」——harness 实测，不是模型自述
    script_exc  out 里有「[脚本异常]」——PTC 脚本自己写崩
    discover    调用 find_tools——不知道工具名，先花一步找
    rewrite     同一个路径被整文件覆盖多次（write_file 整写，前一次必然作废）

一次调用只归一类（优先级从上到下）：soak31 的 c6 某步既报参数错又报写入失败，
那是同一个错误的两句描述，记两次会把返工量吹高一倍。

用法：
    python tools/event_audit.py                    # 默认扫 evals/results/soak*.json
    python tools/event_audit.py --glob "evals/results/fix*.json"
    python tools/event_audit.py --md docs/event-health.md
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import tooltrace  # noqa: E402

RESULTS = ROOT / "evals" / "results"
TASKS = ROOT / "evals" / "tasks.yaml"

READ_TOOLS = {"read_file", "list_dir", "grep", "glob", "view_file"}
WRITE_TOOLS = {"write_file", "edit_file", "patch", "apply_patch", "append_file"}

RE_PARAM = re.compile(r"\[参数\]\s*([A-Za-z_][\w\.]*)\s*接受：")
RE_WRITE_FAIL = re.compile(r"-\s*([A-Za-z_][\w\.]*)\s*→\s*(\S+?):\s*写入失败")
RE_WRITE_OK = re.compile(r"-\s*([A-Za-z_][\w\.]*)\s*→\s*(\S+?):\s*已写入磁盘")
RE_NOOP = re.compile(r"\[ops_batch\]\s*0\s*次工具调用")


def _inner_names(ev: dict) -> list[str]:
    """这一条事件实际碰了哪些工具。inner 缺了就从脚本里解析（同一个口径：tooltrace）。"""
    names = list(ev.get("inner") or [])
    if not names:
        args = ev.get("args")
        script = args.get("script") if isinstance(args, dict) else ""
        if script:
            names = tooltrace.names_from_script(str(script))
        elif isinstance(args, dict):
            names = [ev.get("name") or ""]
    return [n for n in names if n]


def _classify(ev: dict) -> tuple[str, str] | None:
    """给一次工具调用定性。返回 (种类, 可核对的证据片段) 或 None。"""
    out = str(ev.get("out") or "")
    if RE_NOOP.search(out):
        # 脚本语法没问题、就是没调到工具：这一步什么信息都没拿到，比报错更彻底
        return "noop_script", "脚本执行了但 0 次工具调用"
    if "[脚本异常]" in out:
        line = next((l.strip() for l in out.splitlines() if "[脚本异常]" in l), "")
        return "script_exc", line[:110]
    m = RE_PARAM.search(out)
    if m:
        line = next((l.strip() for l in out.splitlines() if "[参数]" in l), "")
        return "param_err", line[:110]
    if "未找到待替换文本" in out:
        return "edit_miss", "未找到待替换文本"
    if "存在歧义" in out:
        line = next((l.strip() for l in out.splitlines() if "存在歧义" in l), "")
        return "edit_miss", line[:110]
    # 写盘失败只在【系统核对】块里认：模型自己的「已写入」不算数，工具的错误散文也不算
    if "【系统核对·写盘结果】" in out and "写入失败" in out:
        tail = out.split("【系统核对·写盘结果】", 1)[1]
        m2 = RE_WRITE_FAIL.search(tail)
        if m2:
            return "write_fail", f"{m2.group(1)} → {m2.group(2)}"
    if ev.get("name") == "find_tools":
        q = (ev.get("args") or {}).get("query") if isinstance(ev.get("args"), dict) else ""
        return "discover", f"find_tools(query={q!r})"
    return None


def _written_paths(ev: dict) -> list[str]:
    """这次调用**真的整篇写**了哪些路径。

    只认【系统核对·写盘结果】里的「已写入磁盘」——脚本里写了 write_file 不代表
    执行到了（soak27 的 w5 就有一次脚本被判空跑，如果按脚本文本数就会把它记成
    重复写）。权威信号只有一个。

    只认 `write_file`：`fill_section` / `write_skeleton` 是**增量**填充，同一路径
    写 6 次是它应有的用法（soak04 就是这么干的），算进「重复整写」就是误伤。
    """
    out = str(ev.get("out") or "")
    if "【系统核对·写盘结果】" not in out:
        return []
    tail = out.split("【系统核对·写盘结果】", 1)[1]
    return [p for t, p in RE_WRITE_OK.findall(tail) if t == "write_file"]


def audit_run(case: dict, *, seed_names: set[str]) -> dict:
    evs = list(case.get("process") or [])
    tools = [e for e in evs if e.get("k") == "tool"]
    # 回合数优先用 turns（bench 数出来的 step 事件数）。旧台账只有 steps，
    # 那是「最后一条泳道的步号」，多泳道并行时会漏算（见 bench_tasks 注释）。
    turns = int(case.get("turns") or case.get("steps")
                or (max([int(e.get("s") or 0) for e in evs]) if evs else 0))

    waste: list[dict] = []
    for e in tools:
        hit = _classify(e)
        if hit:
            waste.append({"kind": hit[0], "step": e.get("s"), "tier": "error",
                          "note": hit[1]})

    # 同一路径被反复整文件覆盖：write_file 是整写，前一次必然作废。
    # 按「成功写盘的次数 - 1」算，不看步号也不看顺序（多泳道下这两样都不稳）。
    per_path: dict[str, int] = {}
    for e in tools:
        for p in _written_paths(e):
            per_path[p] = per_path.get(p, 0) + 1
    for p, n in per_path.items():
        if n > 1:
            waste.append({"kind": "rewrite", "step": "-", "tier": "redundant",
                          "extra": n - 1,
                          "note": f"{p} 整写 {n} 次，前 {n - 1} 次被覆盖"})

    # 未读先写（仅提示）：题面提到 seed 文件、却一次都没读就动手
    first_write = None
    read_before = False
    for e in tools:
        names = set(_inner_names(e))
        if names & READ_TOOLS:
            read_before = True
        if names & WRITE_TOOLS and first_write is None:
            first_write = e.get("s")
            blind = not read_before and bool(seed_names)
            break

    calls = len(tools)
    kinds = [w["kind"] for w in waste]
    err_n = sum(int(w.get("extra", 1)) for w in waste if w["tier"] == "error")
    red_n = sum(int(w.get("extra", 1)) for w in waste if w["tier"] == "redundant")
    return {
        "id": case.get("id"),
        "verdict": case.get("verdict"),
        "layer": case.get("layer"),
        "duration": float(case.get("duration") or 0),
        "turns": turns,
        "calls": calls,
        "calls_per_turn": round(calls / turns, 2) if turns else 0.0,
        "waste": waste,
        "waste_n": err_n + red_n,
        "error_n": err_n,
        "redundant_n": red_n,
        "kinds": kinds,
        "clean": not waste,
        "blind_write": bool(first_write and blind),
        "events": len(evs),
    }


def _seeds() -> dict[str, set[str]]:
    """任务 id → seed 里的文件名。读不到 yaml 就退回空表（未读先写那条就只报 0）。"""
    try:
        import yaml  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    try:
        raw = yaml.safe_load(TASKS.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    out: dict[str, set[str]] = {}
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        seed = t.get("seed") or {}
        if isinstance(seed, dict) and seed:
            out[str(t.get("id"))] = {str(k) for k in seed}
    return out


def _pct(a: int, b: int) -> str:
    return f"{a * 100.0 / b:.1f}%" if b else "-"


def collect(paths: list[Path]) -> list[dict]:
    seeds = _seeds()
    runs: list[dict] = []
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[跳过] {p.name}: {e}", file=sys.stderr)
            continue
        for c in data.get("cases") or []:
            if not c.get("process") and c.get("verdict") == "skip":
                continue
            rec = audit_run(c, seed_names=seeds.get(str(c.get("id")), set()))
            rec["round"] = p.stem
            runs.append(rec)
    return runs


def _md(runs: list[dict], paths: list[Path]) -> str:
    L: list[str] = []
    ids: list[str] = []
    for r in runs:
        if r["id"] not in ids:
            ids.append(r["id"])
    n = len(runs)
    clean = sum(1 for r in runs if r["clean"])
    calls = sum(r["calls"] for r in runs)
    turns = sum(r["turns"] for r in runs)

    L.append(f"# 事件流体检（{len(paths)} 份结果 / {n} 次运行）\n")
    L.append("口径：`turns` = 模型回合数，`calls` = 工具调用事件数。一次调用只归一类返工"
             "（优先级：空脚本 > 脚本异常 > 签名错 > 定位失败 > 写盘失败）。"
             "两类分开看：**错误类**＝这一动作必须重做；**冗余类**＝不算错，"
             "但同一个文件被整写多次、前面的版本直接作废。"
             "判据见 `tools/event_audit.py` 顶部注释。\n")
    L.append("## 总览\n")
    err_total = sum(r["error_n"] for r in runs)
    red_total = sum(r["redundant_n"] for r in runs)
    clean_err = sum(1 for r in runs if r["error_n"] == 0)
    L.append(f"- 运行 {n} 次，**零返工 {clean} 次（{_pct(clean, n)}）**；"
             f"无错误类 {clean_err} 次（{_pct(clean_err, n)}）")
    L.append(f"- 回合合计 {turns}，工具调用合计 {calls}，平均每回合 {calls / turns:.2f} 次调用")
    L.append(f"- 错误类返工 {err_total} 次（占调用 {_pct(err_total, calls)}）；"
             f"冗余类 {red_total} 次（占调用 {_pct(red_total, calls)}）")
    ok = sum(1 for r in runs if r["verdict"] == "pass")
    L.append(f"- 判定通过 {ok} 次（{_pct(ok, n)}）；零返工且通过 "
             f"{sum(1 for r in runs if r['clean'] and r['verdict'] == 'pass')} 次"
             f"（{_pct(sum(1 for r in runs if r['clean'] and r['verdict'] == 'pass'), n)}）\n")

    L.append("## 分任务\n")
    L.append("| 任务 | 运行 | 回合 中位/最大 | 调用 中位/最大 | 零错误 | 零返工 | 错误类 | 冗余类 |")
    L.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for cid in ids:
        rs = [r for r in runs if r["id"] == cid]
        t = [r["turns"] for r in rs]
        c = [r["calls"] for r in rs]
        ce = sum(1 for r in rs if r["error_n"] == 0)
        cl = sum(1 for r in rs if r["clean"])
        L.append(f"| {cid} | {len(rs)} | {statistics.median(t):.0f} / {max(t)} "
                 f"| {statistics.median(c):.0f} / {max(c)} | {ce}/{len(rs)} "
                 f"| {cl}/{len(rs)} | {sum(r['error_n'] for r in rs)} "
                 f"| {sum(r['redundant_n'] for r in rs)} |")
    L.append("")

    L.append("## 返工分类\n")
    L.append("| 种类 | 层级 | 次数 | 含义 |")
    L.append("| --- | --- | ---: | --- |")
    meaning = {
        "noop_script": ("错误", "脚本执行了但 0 次工具调用，这一步什么也没拿到"),
        "script_exc": ("错误", "PTC 脚本自己抛异常"),
        "param_err": ("错误", "工具签名发错，这一步必须重来"),
        "edit_miss": ("错误", "edit_file 没定位到文本 / 多处歧义"),
        "write_fail": ("错误", "系统核对到写盘失败（且上面几类都解释不了）"),
        "discover": ("错误", "调 find_tools 找工具名，等于先花一步打听该用什么"),
        "rewrite": ("冗余", "同一个文件被整写多次，前面的版本直接作废"),
    }
    for k, (tier, text) in meaning.items():
        c = sum(1 for r in runs for x in r["kinds"] if x == k)
        L.append(f"| `{k}` | {tier} | {c} | {text} |")
    L.append("")

    bad = [r for r in runs if not r["clean"]]
    if bad:
        L.append("## 有返工的运行（逐条）\n")
        L.append("| 轮次 | 任务 | 回合 | 调用 | 判定 | 返工 |")
        L.append("| --- | --- | ---: | ---: | --- | --- |")
        for r in bad:
            detail = "；".join(
                (f"s{w['step']} " if w["step"] not in ("-", None, "") else "")
                + w["kind"] for w in r["waste"])
            L.append(f"| {r.get('round', '-')} | {r['id']} | {r['turns']} | {r['calls']} "
                     f"| {r['verdict']}/{r['layer']} | {detail} |")
        L.append("")

    blind = [r for r in runs if r["blind_write"]]
    L.append(f"## 未读先写（提示项，{len(blind)} 次）\n")
    if blind:
        for r in blind:
            L.append(f"- {r['round']} · {r['id']}")
    else:
        L.append("- 无")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="事件流体检：每轮步数与返工量")
    ap.add_argument("--glob", default=str(RESULTS / "soak*.json"),
                    help="结果文件通配（默认 evals/results/soak*.json）")
    ap.add_argument("--md", default="", help="同时写一份 markdown 到这个路径")
    ap.add_argument("--json", default="", help="机器可读汇总写到这个路径")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    paths = sorted(Path(p) for p in glob.glob(args.glob))
    if not paths:
        print(f"没有匹配到结果文件：{args.glob}", file=sys.stderr)
        return 1
    runs = collect(paths)
    if not runs:
        print("结果文件里没有可分析的过程记录。", file=sys.stderr)
        return 1

    md = _md(runs, paths)
    print(md)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
        print(f"\n→ 已写入 {args.md}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
