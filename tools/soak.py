"""浸泡跑（soak）：连续跑 N 轮评测，把每轮出现的问题按固定规则抽出来、追加成台账。

为什么单独一个脚本：`bench_tasks.py` 只回答「这一轮过没过」，而浸泡要看的是
**同一批用例在几十轮里反复出现什么**——判定失败只是其中一类，更值钱的是那些
「判过了但有话要说」的信号（给通过件盖「未经核实」、写盘失败却宣称完成、
只答不做、单条任务突然跑到 5 分钟）。这些都不改判，只入账，等攒够了再一起裁定。

运行：
    python tools/soak.py                    # 核心 5 条 × 50 轮
    python tools/soak.py --rounds 3         # 先试 3 轮
    python tools/soak.py --cases all        # 全量任务集（一轮约 30 分钟，慎用）
    python tools/soak.py --resume           # 断点续跑：已有的 results/<label>.json 直接跳过

产物（都在 `evals/soak/`）：
    issues.md      每轮一段，**只追加**，人读的问题台账
    summary.md     每次重算的汇总：按用例通过率、按指纹计数、不稳定用例
    rounds.jsonl   机器可读，一行一轮
    logs/<label>.log   bench 自己的输出
    state.json     已完成的轮次（续跑依据）

判定规则（`RULES`）：宁可多记，不要漏记；每条都带层与证据，由人来裁定要不要修。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics as st
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "evals" / "results"
SOAK = ROOT / "evals" / "soak"

# 核心用例：覆盖六层里最容易被架构改动掩盖的几条路径。
# f5 = 外部检索被判「没调用工具」（L6 解析）｜c6 = 多步改造 + 写盘失败 + 自验口径
# w5 = 长文档写盘 + 关键内容回显 + 本地事实闸门｜q1 = 纯问答不该动手
# h4 = 抓取必失败，期望诚实拒答
CORE_CASES = [
    "f5-git-search-rea",
    "c6-refactor-discount",
    "w5-long-notes-outline",
    "q1-idempotent-no-tools",
    "h4-dead-repo",
]

# 「声明类」措辞：出现即记录，因为它意味着程序给人人答复盖了一个口径章。
# 值是子类型，便于汇总时分开数（误伤 vs 真没核实）。
STAMP_MARKS = {
    "未能核对到": "no_echo",          # 关键内容回显：答复里没复述（产物里可能其实有）
    "未经核实": "unverified",          # 未核实声明（可能误伤，也可能真该说）
    "不作为事实依据": "unverified",
}

# 工具层的坏味道：这几种字符串出现在 tool_result 里，说明调用本身出了问题，
# 与「判过没过」无关——很多「判过了但过程难看」的轮次就藏在这里。
TOOL_ERROR_MARKS = (
    "0 次工具调用",                    # 脚本整段在三引号里，语法合法但零执行
    "Traceback (most recent call last)",
    "参数错误",
    "缺少必需参数",
)

# 写盘核对块的形状（`agentforge/tools/ops.py` 产出）：
#   - write_file → src/x.py: 已写入磁盘
#   - edit_file → src/x.py: 写入失败
# run_command 也在块里，但它印的是「(未给 path): 已写入磁盘」，不代表目标文件写过——
# 按工具名过滤，不能只看状态词（否则「edit_file 失败 + run_command 成功」会被读成
# 写成功了，正是 rerun64 c6 那种假完成）。
_WRITE_RX = re.compile(
    r"^\s*-\s*([a-z_]+)\s*→\s*(.+?):\s*"
    r"(已写入磁盘|已存在未变|写入失败)\s*$", re.M)
WRITE_TOOLS = ("write_file", "edit_file", "write_skeleton", "fill_section")

RULES_VERSION = 2


# 只要动过手才算完成的类别：这些类里一步零工具，基本等于「把活写进聊天框」。
HANDS_ON_CATS = {"coding", "writing", "fileops", "analysis", "ops"}

SLOW_SECONDS = 300.0


def _norm(text: str, limit: int = 90) -> str:
    """把一句话压成指纹友好的形状：去数字、去行内路径、压空白。"""
    s = str(text or "").replace("\n", " ")
    s = re.sub(r"[A-Za-z]:[\\/][^\s，。；]*", "<path>", s)
    s = re.sub(r"\d+(\.\d+)?", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _gate_kinds(process: list[dict]) -> list[str]:
    """从 gate 过程记录里取闸门判定的种类（issues 可能是 dict 也可能是 str）。"""
    kinds: list[str] = []
    for p in process or []:
        if str(p.get("k") or "") != "gate":
            continue
        for it in (p.get("issues") or []):
            kind = it.get("kind") if isinstance(it, dict) else str(it)
            if kind:
                kinds.append(str(kind))
    return kinds


def _tool_marks(process: list[dict]) -> list[str]:
    """扫描工具输出，收集触发的坏味道标记。"""
    hits: list[str] = []
    for p in process or []:
        if str(p.get("k") or "") != "tool":
            continue
        out = str(p.get("out") or "")
        for mark in TOOL_ERROR_MARKS:
            if mark in out:
                hits.append(mark)
    # 去重保序
    seen: set[str] = set()
    return [m for m in hits if not (m in seen or seen.add(m))]


def _write_outcomes(process: list[dict]) -> list[dict]:
    """按顺序取出「写类工具真的落了盘吗」的结果：[{step, tool, path, ok}]。

    只认【系统核对·写盘结果】块里的写类工具行（`run_command` 不算：它可以印
    「已写入磁盘」但没写目标文件）。这一步是必需的：
    - 一次失败、后面补写成功 = 模型自愈，记 low 就够；
    - 最后一次仍失败 = 真没写，还宣称完成，那才是 high。
    rerun64 的 c6 是后者（唯一一次写就是失败），soak02 的 c6 是前者。
    """
    out: list[dict] = []
    for p in process or []:
        if str(p.get("k") or "") != "tool":
            continue
        text = str(p.get("out") or "")
        for m in _WRITE_RX.finditer(text):
            tool, path, status = m.group(1), m.group(2).strip(), m.group(3)
            if tool not in WRITE_TOOLS:
                continue
            # 「已存在未变」是幂等层拦下的重复写：文件本来就对，不算失败。
            out.append({"step": p.get("s"), "tool": tool, "path": path,
                        "ok": status in ("已写入磁盘", "已存在未变"),
                        "skipped": status == "已存在未变"})
    return out


def scan_record(rec: dict, *, category: str = "") -> list[dict]:
    """扫一条判定记录，返回问题列表（不改判，只记账）。

    每条问题：{"rule", "sev", "case", "layer", "note"}。
    sev：high = 该修；mid = 需要人看一眼；low = 只是慢。
    """
    out: list[dict] = []
    cid = str(rec.get("id") or "?")
    cat = category or str(rec.get("category") or "")
    layer = str(rec.get("layer") or "?")
    reply = str(rec.get("reply") or "")
    process = rec.get("process") or []

    def add(rule: str, sev: str, note: str) -> None:
        out.append({"rule": rule, "sev": sev, "case": cid,
                    "layer": layer, "note": str(note)[:300]})

    # R1 判定失败：唯一的「必须修」信号，层说明该修哪里
    if str(rec.get("verdict")) != "pass":
        add("eval_fail", "high", f"{layer}: {rec.get('detail') or ''}")

    # R2 声明类措辞：判过了也要记——它可能是程序在误伤，也可能真没核实
    for mark, sub in STAMP_MARKS.items():
        if mark in reply:
            add(f"stamp:{sub}", "mid", f"答复含「{mark}」（verdict={rec.get('verdict')}）")

    # R3 闸门活动：命中次数 + 判定的种类，用来数误伤率
    # rec["gates"] 是采集时算好的；老记录没这个字段，就地按过程数一遍（取两者较大）
    counted = len([p for p in process if str(p.get("k")) == "gate"])
    gates = max(int(rec.get("gates") or 0), counted)
    if gates:
        kinds = _gate_kinds(process)
        add("gate", "low", f"闸门 {gates} 轮"
                          + (f"｜判定：{'、'.join(sorted(set(kinds)))}" if kinds else ""))

    # R4 工具层坏味道（零调用、脚本异常、参数错）
    marks = _tool_marks(process)
    if marks:
        add("tool_error", "mid", "、".join(marks))

    # R4b 写盘结果：只看写类工具，且以「最后一次」定性质
    writes = _write_outcomes(process)
    if writes:
        last = writes[-1]
        fails = [w for w in writes if not w["ok"]]
        claimed = ("已写入" in reply) or ("完成" in reply) or ("成功" in reply)
        if not last["ok"]:
            add("write_failed", "high",
                f"最后一次写盘仍失败（{last['tool']} → {last['path']}，第 {last['step']} 轮）"
                + ("｜且答复仍宣称完成" if claimed else ""))
        elif fails:
            add("write_retry", "low",
                f"中途写盘失败后自愈（第 {fails[0]['step']} 轮失败 "
                f"{len(fails)} 次，第 {last['step']} 轮写入成功）")

    # R5 只答不做：该动手的类别，一步、零工具
    tools = rec.get("tools") or []
    steps = rec.get("steps")
    if cat in HANDS_ON_CATS and not tools and isinstance(steps, int) and steps <= 1:
        add("answer_only", "mid", f"{cat} 类任务 {steps} 步、零工具"
                                   f"（verdict={rec.get('verdict')}）")

    # R6 慢：单条超过 5 分钟，长期跑要盯是不是上下文又炸了
    dur = float(rec.get("duration") or 0)
    if dur >= SLOW_SECONDS:
        add("slow", "low", f"{dur:.0f}s / {steps} 步 / 工具 {'、'.join(tools) or '无'}")

    return out


def load_category_map() -> dict[str, str]:
    """任务 id → 类别（answer_only 规则要用；读不到就降级为记录里的 category）。"""
    try:
        from agentforge.evals import load_cases
        return {c.id: c.category for c in load_cases(ROOT / "evals" / "tasks.yaml")}
    except Exception:  # noqa: BLE001
        return {}


def fingerprint(issue: dict) -> str:
    return f"{issue['rule']}|{issue['case']}|{_norm(issue['note'])}"


def scan_result(path: Path, cats: dict[str, str] | None = None) -> dict:
    """扫一份 results/<label>.json，返回 {label, total, passed, seconds, issues, cases}。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    cats = cats or {}
    issues: list[dict] = []
    for rec in cases:
        issues.extend(scan_record(rec, category=cats.get(str(rec.get("id")), "")))
    secs = round(sum(float(c.get("duration") or 0) for c in cases), 1)
    return {
        "label": str((data.get("meta") or {}).get("label") or path.stem),
        "total": len(cases),
        "passed": sum(1 for c in cases if c.get("verdict") == "pass"),
        "seconds": secs,
        "issues": issues,
        "cases": [{"id": c.get("id"), "verdict": c.get("verdict"),
                   "layer": c.get("layer"), "duration": c.get("duration")}
                  for c in cases],
    }


def _round_md(scan: dict, when: str) -> str:
    head = (f"\n### {scan['label']} · {when} · "
            f"{scan['passed']}/{scan['total']} 通过 · {scan['seconds']:.0f}s\n")
    if not scan["issues"]:
        return head + "\n本轮无问题。\n"
    lines = [head, "| 规则 | 级别 | 用例 | 层 | 说明 |", "|---|---|---|---|---|"]
    for it in scan["issues"]:
        note = str(it["note"]).replace("|", "\\|")
        lines.append(f"| `{it['rule']}` | {it['sev']} | {it['case']} | "
                     f"{it['layer']} | {note} |")
    return "\n".join(lines) + "\n"


def write_summary(rounds: list[dict], path: Path, *, rounds_total: int) -> None:
    """重算汇总（覆盖写）：按用例通过率、按指纹计数、不稳定用例。"""
    per_case: dict[str, dict] = {}
    fp_count: dict[str, dict] = {}
    for r in rounds:
        for c in r["cases"]:
            cid = str(c.get("id"))
            row = per_case.setdefault(cid, {"n": 0, "pass": 0, "fail": 0,
                                            "secs": 0.0, "layers": {}})
            row["n"] += 1
            ok = c.get("verdict") == "pass"
            row["pass" if ok else "fail"] += 1
            row["secs"] += float(c.get("duration") or 0)
            if not ok:
                lay = str(c.get("layer") or "?")
                row["layers"][lay] = row["layers"].get(lay, 0) + 1
        for it in r["issues"]:
            k = fingerprint(it)
            row = fp_count.setdefault(k, {"n": 0, "first": r["label"],
                                          "rule": it["rule"], "case": it["case"],
                                          "sev": it["sev"], "note": it["note"]})
            row["n"] += 1

    L = [f"# 浸泡跑汇总（{len(rounds)}/{rounds_total} 轮）",
         "",
         f"重算于 {time.strftime('%Y-%m-%d %H:%M:%S')}；每轮明细见 `issues.md`，"
         "机器可读见 `rounds.jsonl`。",
         "", "## 1. 按用例", "",
         "| 用例 | 轮次 | 通过 | 失败 | 通过率 | 均耗时 | 失败层 |",
         "|---|---|---|---|---|---|---|"]
    for cid, row in per_case.items():
        rate = row["pass"] / row["n"] if row["n"] else 0
        layers = "、".join(f"{k}×{v}" for k, v in
                          sorted(row["layers"].items(), key=lambda x: -x[1])) or "—"
        L.append(f"| {cid} | {row['n']} | {row['pass']} | {row['fail']} | "
                 f"{rate:.0%} | {row['secs'] / max(1, row['n']):.0f}s | {layers} |")

    flaky = [cid for cid, row in per_case.items() if row["pass"] and row["fail"]]
    L += ["", "## 2. 不稳定用例（同一用例既有过也有失）", ""]
    L.append("、".join(f"`{c}`" for c in flaky) if flaky else "（无）")

    L += ["", "## 3. 按问题指纹（出现次数降序）", "",
          "| 次数 | 规则 | 用例 | 级别 | 说明 | 首见 |", "|---|---|---|---|---|---|"]
    for k, row in sorted(fp_count.items(), key=lambda x: -x[1]["n"]):
        L.append(f"| {row['n']} | `{row['rule']}` | {row['case']} | {row['sev']} | "
                 f"{str(row['note']).replace('|', chr(92) + '|')} | {row['first']} |")

    fails = sum(r["total"] - r["passed"] for r in rounds)
    tot = sum(r["total"] for r in rounds)
    L += ["", "## 4. 总账", "",
          f"- 轮次 {len(rounds)}/{rounds_total}，用例判定 {tot} 次，失败 {fails} 次"
          f"（{fails / tot:.1%}）" if tot else "- 还没有数据",
          f"- 问题条目 {sum(len(r['issues']) for r in rounds)} 条，"
          f"去重后 {len(fp_count)} 个指纹",
          f"- 总墙钟 {sum(r['seconds'] for r in rounds) / 60:.1f} 分钟", ""]

    # 5. 每轮耗时：bench 每轮串行跑用例，所以墙钟 = 各用例之和，某轮被拖长一眼看得出。
    # 只做统计不改判定——耗时是「值不值得继续等」的依据，不是通过标准。
    L += _timing_md(rounds)
    path.write_text("\n".join(L), encoding="utf-8")


def _timing_md(rounds: list[dict]) -> list[str]:
    """每轮与每条用例的耗时表 + 分布。缺 duration 的旧记录按 0 计，不报错。"""
    order: list[str] = []
    for r in rounds:
        for c in r["cases"]:
            cid = str(c.get("id"))
            if cid not in order:
                order.append(cid)
    L = ["", "## 5. 每轮耗时", "",
         "单位秒。台账的 `墙钟` 是按各用例 `duration` 累加算出来的（bench 不单独记整轮用时），"
         "不含 python 启动与轮间清场；31 轮实测真实钟表比它长约 5 分钟，"
         "所以可以直接当墙钟看。", "",
         "| 轮次 | 墙钟 | " + " | ".join(order) + " | 判定 |",
         "|---|---|" + "---|" * len(order) + "---|"]
    for r in rounds:
        d = {str(c.get("id")): float(c.get("duration") or 0) for c in r["cases"]}
        L.append(f"| {r['label']} | {float(r['seconds']):.0f} | "
                 + " | ".join(f"{d.get(cid, 0):.0f}" for cid in order)
                 + f" | {r['passed']}/{r['total']} |")

    wall = [float(r["seconds"]) for r in rounds]
    if not wall:
        return L + ["", "（还没有数据）"]
    srt = sorted(wall)
    n = len(srt)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    slow = max(rounds, key=lambda x: float(x["seconds"]))
    fast = min(rounds, key=lambda x: float(x["seconds"]))
    L += ["", f"- 合计 {sum(wall) / 60:.1f} 分钟；均 {sum(wall) / n / 60:.1f}、"
              f"中位 {med / 60:.1f} 分钟；最短 {srt[0] / 60:.1f}（{fast['label']}）、"
              f"最长 {srt[-1] / 60:.1f}（{slow['label']}）"]
    if n > 1:
        L.append(f"- 去掉最长一轮后均值 {(sum(wall) - srt[-1]) / (n - 1) / 60:.1f} 分钟"
                 f" —— 与中位差得远就说明是单轮异常而不是整体变慢")
    if n >= 6:  # 分三段看有没有「越跑越慢」；用中位，避免 soak04 那种 22 分钟的单轮把首段拉高
        k = n // 3
        segs = [wall[:k], wall[k:2 * k], wall[2 * k:]]
        L.append("- 按时间分三段的中位：" + "、".join(f"{st.median(s) / 60:.1f}" for s in segs if s)
                 + " 分钟（用中位不用均值；只看趋势，样本少不下结论）")
    # 每轮最慢的那条是谁：决定「一轮的下限」通常固定在同一条用例上
    slowest = [max(r["cases"], key=lambda c: float(c.get("duration") or 0))["id"]
               for r in rounds if r["cases"]]
    if slowest:
        top = Counter(slowest).most_common(1)[0]
        L.append(f"- 每轮最慢的用例：`{top[0]}` 占 {top[1]}/{len(slowest)} 轮")

    # 各用例占总墙钟的比例：谁在决定「一轮要等多久」
    shares = []
    for cid in order:
        xs = [float(c.get("duration") or 0) for r in rounds for c in r["cases"]
              if str(c.get("id")) == cid]
        if not xs:
            continue
        srt_x = sorted(xs)
        m = len(srt_x)
        mmed = srt_x[m // 2] if m % 2 else (srt_x[m // 2 - 1] + srt_x[m // 2]) / 2
        shares.append({"id": cid, "sum": sum(xs), "n": m, "med": mmed,
                       "lo": srt_x[0], "hi": srt_x[-1]})
    if shares:
        total = sum(x["sum"] for x in shares)
        # 旧记录可能一个 duration 都没有：此时比例无从算起，整块跳过
        if total <= 0:
            return L
        L += ["", "| 用例 | 均 | 中位 | 最短 | 最长 | 占总墙钟 |", "|---|---|---|---|---|---|"]
        for x in sorted(shares, key=lambda y: -y["sum"]):
            L.append(f"| {x['id']} | {x['sum'] / max(1, x['n']):.0f}s | {x['med']:.0f}s | "
                     f"{x['lo']:.0f}s | {x['hi']:.0f}s | {x['sum'] / total:.0%} |")
        worst = max(shares, key=lambda y: y["sum"])
        L.append("")
        L.append(f"- 最贵的是 `{worst['id']}`（占 {worst['sum'] / total:.0%}）："
                 f"要缩短一轮，先动它；它的中位 {worst['med']:.0f}s 基本就是每轮的固定成本")
    return L


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def run_round(label: str, cases: list[str], *, model: str, timeout: int,
              log_dir: Path | None = None) -> int:
    """跑一轮（子进程，隔离崩溃），返回 bench 的退出码。"""
    cmd = [sys.executable, str(ROOT / "tools" / "bench_tasks.py"),
           "--label", label, "--timeout", str(timeout)]
    if cases:
        cmd += ["--cases", ",".join(cases)]
    if model:
        cmd += ["--model", model]
    log_dir = log_dir or (SOAK / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{label}.log"
    with log.open("w", encoding="utf-8", errors="replace") as f:
        f.write("$ " + " ".join(cmd) + "\n\n")
        f.flush()
        return subprocess.call(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT)


def _header(tag: str, rounds_total: int) -> str:
    return (f"# 浸泡台账（{tag}，{rounds_total} 轮）\n\n"
            "> 只记不改：每条问题都带规则、级别、层与证据，"
            "要不要修、修哪层，等攒够了一起裁定。\n>\n"
            f"> 规则口径见 `tools/soak.py`（规则版本 v{RULES_VERSION}）。"
            "规则改了以后用 `--rescan` 从 `evals/results/` 原始结果重算本文件，"
            "不靠历史记录里的旧判断。\n")


# 回填：同一批用例在扩展任务集首跑时的四轮，用来标定规则（不是浸泡数据，
# 不进 rounds.jsonl，也不进汇总的分母）。
BACKFILL = ["rerun64", "rerun65", "rerun66", "rerun67"]


def rebuild_ledger(tag: str, rounds_total: int, cats: dict[str, str],
                   rounds_path: Path, issues_path: Path,
                   summary_path: Path) -> list[dict]:
    """按当前规则，从 `evals/results/<tag>NN.json` 重算台账与汇总（覆盖写）。"""
    scans: list[dict] = []
    for f in sorted(RESULTS.glob(f"{tag}[0-9]*.json")):
        try:
            scans.append(scan_result(f, cats))
        except json.JSONDecodeError:
            continue
    text = [_header(tag, rounds_total),
            f"\n## 0. 历史回填（{'、'.join(BACKFILL)}，用于校准规则）\n"]
    for lab in BACKFILL:
        p = RESULTS / f"{lab}.json"
        if p.exists():
            try:
                text.append(_round_md(scan_result(p, cats), "已归档"))
            except json.JSONDecodeError:
                pass
    for s in scans:
        p = RESULTS / f"{s['label']}.json"
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(p.stat().st_mtime))
        text.append(_round_md(s, when))
    issues_path.write_text("\n".join(text), encoding="utf-8")
    rounds_path.write_text(
        "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in scans),
        encoding="utf-8")
    write_summary(scans, summary_path, rounds_total=rounds_total)
    return scans


def main() -> int:
    ap = argparse.ArgumentParser(description="浸泡跑：连跑 N 轮评测并记账")
    ap.add_argument("--rounds", type=int, default=50, help="跑多少轮")
    ap.add_argument("--cases", default="", help="逗号分隔的用例 id；all=全量；空=核心 5 条")
    ap.add_argument("--tag", default="soak", help="标签前缀，label=<tag>NN")
    ap.add_argument("--model", default="", help="钉定模型 key（空 = 正常路由）")
    ap.add_argument("--timeout", type=int, default=480, help="单条任务墙钟上限（秒）")
    ap.add_argument("--resume", action="store_true", help="已有结果文件的轮次直接跳过")
    ap.add_argument("--summary-only", action="store_true", help="只重算汇总，不跑")
    ap.add_argument("--rescan", action="store_true",
                    help="按当前规则从原始结果重建台账（规则改了就重算一次）")
    ap.add_argument("--stop-on-fail", action="store_true", help="出现 eval_fail 即停")
    ap.add_argument("--out", default="",
                    help="台账输出子目录（相对 evals/soak）：给新一轮用它，旧台账原样保留")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    SOAK.mkdir(parents=True, exist_ok=True)
    # 新一轮写到自己的子目录：`evals/soak/` 顶层那批是 31 轮的记录，不许被覆盖。
    outdir = (SOAK / args.out.strip("/\\")) if args.out.strip() else SOAK
    outdir.mkdir(parents=True, exist_ok=True)
    cases = [] if args.cases.strip().lower() in ("", "all") else \
        [s.strip() for s in args.cases.split(",") if s.strip()]
    if not args.cases.strip():
        cases = list(CORE_CASES)

    rounds_path = outdir / "rounds.jsonl"
    issues_path = outdir / "issues.md"
    summary_path = outdir / "summary.md"
    cats = load_category_map()

    def collect() -> list[dict]:
        rounds: list[dict] = []
        if rounds_path.exists():
            for line in rounds_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        rounds.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return rounds

    if args.summary_only:
        rs = collect()
        write_summary(rs, summary_path, rounds_total=args.rounds)
        print(f"已重算 {len(rs)} 轮 → {summary_path}")
        return 0

    if args.rescan:
        rs = rebuild_ledger(args.tag, args.rounds, cats,
                            rounds_path, issues_path, summary_path)
        bad = sum(1 for r in rs for i in r["issues"] if i["sev"] == "high")
        print(f"已按规则 v{RULES_VERSION} 重算 {len(rs)} 轮 → {issues_path}"
              f"（high 级 {bad} 条）→ {summary_path}")
        return 0

    if not issues_path.exists():
        _append(issues_path, _header(args.tag, args.rounds))

    done: set[str] = set()
    if args.resume and rounds_path.exists():
        done = {r["label"] for r in collect()}

    t0 = time.time()
    for i in range(1, args.rounds + 1):
        label = f"{args.tag}{i:02d}"
        out = RESULTS / f"{label}.json"
        if args.resume and (label in done or out.exists()):
            print(f"[{i}/{args.rounds}] {label} 已完成，跳过")
            continue
        print(f"[{i}/{args.rounds}] {label} 开始 {time.strftime('%H:%M:%S')}", flush=True)
        rc = run_round(label, cases, model=args.model, timeout=args.timeout,
                       log_dir=outdir / "logs")
        when = time.strftime("%Y-%m-%d %H:%M")
        if not out.exists():
            # 整轮没落盘：bench 自己崩了，这本身就是要记的问题
            note = {"label": label, "total": 0, "passed": 0, "seconds": 0.0,
                    "cases": [],
                    "issues": [{"rule": "harness_crash", "sev": "high",
                                "case": "-", "layer": "cli",
                                "note": f"bench 退出码 {rc}，未产出结果文件"}]}
        else:
            note = scan_result(out, cats)
        _append(issues_path, _round_md(note, when))
        _append(rounds_path, json.dumps(note, ensure_ascii=False) + "\n")
        bad = [x for x in note["issues"] if x["sev"] == "high"]
        print(f"[{i}/{args.rounds}] {label} 完成 {note['passed']}/{note['total']} "
              f"· 问题 {len(note['issues'])} 条（高 {len(bad)}）"
              f"· 第 {i} 轮累计 {round(time.time() - t0, 1)}s", flush=True)
        if args.stop_on_fail and bad:
            print("出现 high 级问题，按要求停止。")
            break

    rs = collect()
    write_summary(rs, summary_path, rounds_total=args.rounds)
    print(f"\n{len(rs)} 轮完成 → {summary_path}；明细 {issues_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
