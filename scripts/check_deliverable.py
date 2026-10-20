# -*- coding: utf-8 -*-
"""交付物证据核查器：把差距清单里引用的 path:line 逐条回源核对。

评测口径（不看流程健康，只看结论对不对）：
1. 文件存在性 —— 引用的路径是否真的在磁盘上；
2. 行号可达性 —— 文件总行数是否覆盖引用行号（行号超出即编造）；
3. 目录引用密度 —— 每条差距行是否都带证据（有 path:line 才算）；
4. 覆盖度 —— 任务要求的 10 个对比点是否都出现；
5. 借鉴清单 —— 末尾是否有 ≤5 条的性价比清单。

用法：python debug/check_deliverable.py evals/xxx.md [--verbose]
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

# path:line（兼容 path:12 / path.py:12-18 / path.py#L12）
RX_REF = re.compile(
    r"([A-Za-z0-9_\-./]+\.(?:py|md|ts|tsx|js|json|yaml|yml|toml|txt))"
    r"(?::(\d+)(?:\s*[-–]\s*(\d+))?|#L(\d+))?"
)

REQUIRED_TOPICS = [
    ("agent loop 结构", (r"loop", r"主循环", r"agent\s*loop", r"循环结构")),
    ("工具调用方式", (r"工具调用", r"原生\s*(?:tool|function)", r"tool_call", r"文本兜底")),
    ("上下文压缩", (r"上下文压缩", r"压缩策略", r"compact", r"摘要")),
    ("TODO/任务清单", (r"todo", r"任务清单", r"任务系统", r"task\s*system")),
    ("子代理与上下文隔离", (r"子代理", r"subagent", r"sub-agent", r"上下文隔离")),
    ("权限与沙箱", (r"权限", r"sandbox", r"沙箱", r"permission")),
    ("评测体系", (r"评测", r"eval", r"benchmark", r"SWE-Bench")),
    ("会话恢复", (r"会话恢复", r"续跑", r"中断", r"resume", r"恢复")),
    ("记忆系统", (r"记忆", r"memory")),
    ("skill 系统", (r"skill", r"技能")),
]


def load(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def check(path_arg: str, verbose: bool = False) -> int:
    target = (ROOT / path_arg) if not Path(path_arg).is_absolute() else Path(path_arg)
    if not target.exists():
        print(f"[FAIL] 交付物不存在：{target}")
        return 1
    text = load(target)
    lines = [l for l in text.splitlines() if l.strip()]
    print(f"交付物：{target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
    print(f"大小：{len(text)} 字符 / {len(lines)} 非空行")

    # ---- 表格行 ----
    table_rows = [l for l in lines if l.count("|") >= 4 and not set(l.strip()) <= set("|-: ")]
    data_rows = [l for l in table_rows if not re.match(r"^\s*\|[\s\-:|]+\|?\s*$", l)]
    headerish = [l for l in data_rows if re.search(r"对比点|对方做法|AgentForge 现状", l)]
    print(f"\n表格行：{len(data_rows)}（含表头 {len(headerish)}）")

    # ---- 逐条引用回源 ----
    refs = []
    for m in RX_REF.finditer(text):
        p = m.group(1)
        ln = m.group(2) or m.group(4)
        end = m.group(3)
        refs.append((p, int(ln) if ln else None, int(end) if end else None))
    # 去重保序
    seen, uniq = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            uniq.append(r)

    missing_file, bad_line, ok_ref, hmm = [], [], 0, []
    for p, ln, end in uniq:
        cand = ROOT / p
        if not cand.exists():
            # 允许 external/ 前缀省略的写法
            alt = ROOT / "external" / p
            if alt.exists():
                cand = alt
        if not cand.exists() and "/" not in p:
            # 裸文件名（证据列常写 `loop.py` 而不是 `agentforge/loop.py`）：
            # 在项目里找一次唯一的同名文件，找到就算定位成功。
            hits = [c for c in ROOT.rglob(p)
                    if ".venv" not in c.parts and "node_modules" not in c.parts][:3]
            if hits:
                cand = hits[0]
        if not cand.exists():
            missing_file.append(p)
            continue
        if ln is None:
            hmm.append(p)
            continue
        try:
            total = len(load(cand).splitlines())
        except Exception:
            total = 0
        top = max(ln, end or 0)
        if top > total:
            bad_line.append((p, top, total))
        else:
            ok_ref += 1

    print(f"\n引用总数：{len(uniq)}  带行号且可定位：{ok_ref}")
    print(f"文件不存在：{len(missing_file)}")
    for p in missing_file[:15]:
        print(f"  [X] {p}")
    print(f"行号超出文件长度（疑似编造）：{len(bad_line)}")
    for p, top, total in bad_line[:15]:
        print(f"  [X] {p}:{top} > 实际 {total} 行")
    if hmm:
        print(f"仅路径无行号：{len(hmm)}（{'、'.join(hmm[:6])}）")

    # ---- 覆盖度 ----
    low = text.lower()
    print("\n十项对比点覆盖：")
    miss = []
    for name, pats in REQUIRED_TOPICS:
        hit = any(re.search(p, low) for p in pats)
        print(f"  {'OK ' if hit else 'MISS'} {name}")
        if not hit:
            miss.append(name)

    # ---- 借鉴清单 ----
    tail = text[-2500:]
    has_list = bool(re.search(r"性价比|借鉴清单|优先", tail))
    items = len(re.findall(r"^\s*(?:\d+[.、)]|[-*])\s+\S", tail, re.M))
    print(f"\n借鉴清单节：{'有' if has_list else '无'}；条目行数≈{items}")

    verdict = []
    if missing_file:
        verdict.append(f"{len(missing_file)} 个引用文件不存在")
    if bad_line:
        verdict.append(f"{len(bad_line)} 处行号越界")
    if miss:
        verdict.append(f"缺 {len(miss)} 项要求维度")
    if not has_list:
        verdict.append("缺借鉴清单")
    print("\n" + "=" * 60)
    print("结论：" + ("；".join(verdict) if verdict else "证据与覆盖度均通过"))
    return 0 if not verdict else 2


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("用法：python debug/check_deliverable.py <交付物路径>")
        sys.exit(1)
    sys.exit(check(args[0], "--verbose" in sys.argv))
