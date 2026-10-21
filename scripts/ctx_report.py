# -*- coding: utf-8 -*-
"""上下文构成体检：这一跑里，模型看到的东西到底是什么、占多少。

为什么需要它：rerun62 的日志里 `compact` 事件有 30 条，`truncate 3872→3878`
这种「越压越长」的条目夹在其中，`60640→6087` 之后又弹回 `63703`——
光看这些数字判断不了「是谁在占地方」，得把上下文按来源拆开看。

用法：
    python scripts/ctx_report.py                     # 最近一次运行
    python scripts/ctx_report.py --journal .agentforge/journal/run-1021-002150-592-506
    python scripts/ctx_report.py --list 5            # 最近 5 次各出一张表
    python scripts/ctx_report.py --json              # 机器可读（写进复盘报告用）

数据来源是会话日志（`.agentforge/journal/<run>/events.jsonl`）：
  · `system` 事件 —— 每轮实际贴给模型的系统提示全文（模型可见即已记录）
  · `message` 事件 —— 每一条进历史的 user / assistant / tool 消息
  · `compact` 事件 —— 每次压缩的前后字符数与策略
另有一节直接量工具 schema 的体积（core 下发 vs 全量），它不进 messages，
但在 API 请求里真实占预算。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

JOURNAL_ROOT = ROOT / ".agentforge" / "journal"

# 系统提示里的分节标题（顺序即出现顺序）。命中即开新节，用来算「谁占得多」。
SECTIONS = [
    ("## 工作环境", "基础环境（路径/权限/模型）"),
    ("## 工作方式", "工作方式与纪律"),
    ("## 输出要求", "输出格式要求"),
    ("## 安全约束", "安全约束"),
    ("## 写文件：系统按记录核对", "写盘规则"),
    ("## 产出与中间文件", "产出目录规则"),
    ("【项目速览", "项目速览（warmup 预热）"),
    ("## PTC 常驻", "PTC 常驻说明"),
    ("## 可编入脚本的工具", "工具索引（只有名字）"),
    ("## 长期记忆", "长期记忆（偏好）"),
]


def _rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _msg_of(rec: dict) -> dict:
    """message 事件里的 msg 是 Python repr（不是 JSON），用 literal_eval 还原。"""
    raw = (rec or {}).get("msg")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            got = ast.literal_eval(raw)
            return got if isinstance(got, dict) else {"content": raw}
        except (ValueError, SyntaxError):
            return {"content": raw}
    return {}


def _size(m: dict) -> int:
    c = m.get("content")
    n = len(c) if isinstance(c, str) else 0
    if m.get("tool_calls"):
        n += len(str(m["tool_calls"]))
    return n


def _system_sections(text: str) -> list[tuple[str, int]]:
    """按标题把系统提示切成小节，返回 [(名字, 字符数)]，保持文档顺序。"""
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        for prefix, name in SECTIONS:
            if line.startswith(prefix):
                marks.append((i, name))
                break
    if not marks:
        return [("（未识别分节）", len(text))]
    out: list[tuple[str, int]] = []
    if marks[0][0] > 0:                              # 标题之前的前言
        out.append(("开头总述", len("\n".join(lines[:marks[0][0]]))))
    for k, (idx, name) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        out.append((name, len("\n".join(lines[idx:end]))))
    return out


def _tool_budget() -> dict:
    """工具 schema 的体积：PTC 常驻下只下发 core 那几个，量出来才知道省了多少。"""
    try:
        from agentforge.config import Config
        from agentforge.tools import build_registry
        cfg = Config.load(str(ROOT / "config.yaml"))
        reg = build_registry(None, cfg)
        allsch = reg.schemas()
        core = reg.selector.core_schemas() if reg.selector else allsch
        return {
            "resident": bool(getattr(cfg, "ptc_resident", False)),
            "all_count": len(allsch),
            "all_chars": len(json.dumps(allsch, ensure_ascii=False)),
            "core_count": len(core),
            "core_chars": len(json.dumps(core, ensure_ascii=False)),
            "core_names": [s["function"]["name"] for s in core],
        }
    except Exception as e:  # noqa: BLE001 —— 体检脚本不该因为配置缺失就整个失败
        return {"error": str(e)}


def _tool_name(m: dict) -> str:
    """tool 消息的工具名。journal 只存 role/content，所以先从抬头猜：
    `[ops_batch] 3 次工具调用（read_file×2、web_search×1）` 这类格式很稳定。"""
    name = str(m.get("name") or m.get("tool_name") or "")
    if name:
        return name
    body = str(m.get("content") or "")
    m1 = re.match(r"\s*\[([a-z_][\w-]*)\]", body)
    if not m1:
        return "（未知工具）"
    base = m1.group(1)
    inner = re.findall(r"（([^）]*)）", body[:200])
    if inner:
        used = re.findall(r"([a-z_][\w-]*)×\d+", inner[0])
        if used:
            return f"{base}({'/'.join(dict.fromkeys(used))})"
    return base


def report(run_dir: Path) -> dict:
    path = run_dir / "events.jsonl"
    rows = _rows(path)
    systems = [r for r in rows if r.get("type") == "system"]
    msgs = [_msg_of(r) for r in rows if r.get("type") == "message"]
    compacts = [r for r in rows if r.get("type") == "compact"]
    start = next((r for r in rows if r.get("type") == "run_start"), {})

    system_text = str(systems[-1].get("content") or "") if systems else ""
    sections = _system_sections(system_text) if system_text else []

    by_role: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    for m in msgs:
        role = str(m.get("role") or "?")
        n = _size(m)
        by_role[role] = by_role.get(role, 0) + n
        if role == "tool":
            name = _tool_name(m)
            by_tool[name] = by_tool.get(name, 0) + n

    biggest = sorted(((_size(m), str(m.get("role") or "?"), m) for m in msgs),
                     key=lambda t: -t[0])[:8]

    return {
        "run": run_dir.name,
        "prompt_head": str(start.get("user_text") or "")[:80],
        "model": start.get("model_name") or start.get("model") or "",
        "system_chars": len(system_text),
        "system_sections": sections,
        "message_count": len(msgs),
        "by_role": by_role,
        "by_tool": by_tool,
        "biggest": [(n, role, str(m.get("content") or "")[:70].replace("\n", " "))
                    for n, role, m in biggest],
        "compacts": [{"strategy": c.get("strategy"),
                      "before": c.get("before"), "after": c.get("after"),
                      "note": c.get("note")} for c in compacts],
        "tools": _tool_budget(),
    }


def render(rep: dict) -> None:
    print("=" * 78)
    print(f"运行 {rep['run']}   模型 {rep['model']}")
    print(f"任务：{rep['prompt_head']}…")
    print("=" * 78)

    total = rep["system_chars"] + sum(rep["by_role"].values())
    print(f"\n【总览】系统提示 {rep['system_chars']} 字 · 历史 {rep['message_count']} 条 "
          f"{sum(rep['by_role'].values())} 字 · 合计 {total} 字")

    t = rep["tools"]
    if "error" not in t:
        print(f"\n【工具 schema】PTC 常驻={t['resident']} · "
              f"下发 {t['core_count']} 个 / {t['core_chars']} 字 "
              f"（全量 {t['all_count']} 个 / {t['all_chars']} 字，只列名字的索引在系统提示里）")
        print(f"  本轮下发：{', '.join(t['core_names'])}")
    else:
        print(f"\n【工具 schema】测量失败：{t['error']}")

    if rep["system_sections"]:
        print("\n【系统提示分节】")
        base = max(1, rep["system_chars"])
        for name, n in sorted(rep["system_sections"], key=lambda kv: -kv[1]):
            print(f"  {n:6d} 字  {n / base * 100:5.1f}%   {name}")

    print("\n【历史构成】按角色")
    for role, n in sorted(rep["by_role"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d} 字  {role}")
    if rep["by_tool"]:
        print("\n【历史构成】按工具（tool 消息）")
        for name, n in sorted(rep["by_tool"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:6d} 字  {name}")
    if rep["biggest"]:
        print("\n【单条最大的消息】")
        for n, role, head in rep["biggest"]:
            print(f"  {n:6d} 字  {role:9s} {head}")

    cs = rep["compacts"]
    print(f"\n【压缩记录】{len(cs)} 次")
    if cs:
        bad = [c for c in cs if (c["after"] or 0) >= (c["before"] or 0)]
        by_strategy: dict[str, int] = {}
        for c in cs:
            by_strategy[str(c["strategy"])] = by_strategy.get(str(c["strategy"]), 0) + 1
        print("  策略分布：" + "，".join(f"{k}×{v}" for k, v in by_strategy.items()))
        if bad:
            print(f"  ⚠ 其中 {len(bad)} 次「压了没变小」（含前缀叠加/压不动还硬压）：")
            for c in bad[:5]:
                print(f"    {c['before']} → {c['after']}  {c['note']}")
        for c in cs[:6]:
            print(f"  {c['strategy']:9s} {c['before']} → {c['after']}  {c['note']}")
        if len(cs) > 6:
            print(f"  …其余 {len(cs) - 6} 条略（--json 可看全）")


def main() -> None:
    ap = argparse.ArgumentParser(description="上下文构成体检")
    ap.add_argument("--journal", help="指定 journal 目录（默认取最近一次）")
    ap.add_argument("--list", type=int, default=0, help="最近 N 次各出一张表")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if args.journal:
        dirs = [Path(args.journal)]
        if not dirs[0].is_absolute():
            dirs[0] = ROOT / dirs[0]
    else:
        dirs = sorted((d for d in JOURNAL_ROOT.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
        dirs = dirs[: max(1, args.list)]
    if not dirs:
        print("没有找到会话日志（.agentforge/journal/*）")
        return

    reps = [report(d) for d in dirs]
    if args.json:
        print(json.dumps(reps, ensure_ascii=False, indent=2))
        return
    for rep in reps:
        render(rep)
        print()


if __name__ == "__main__":
    main()
