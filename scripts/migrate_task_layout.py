"""把历史任务目录整理成统一布局（一次性迁移，可重复执行）。

规则（与 agentforge/layout.py 一致）：

    history/<任务>/record/        过程记录（harness 写）
        evidence.jsonl / citations.jsonl / versions/
        work/                     模型写的中间/过渡文件
    history/<任务>/output/        最终产出（按声明路径镜像）

旧任务里这些位置是乱的：中间文件直接摊在任务目录根上，`_records/` 是证据目录，
有的任务还带一个 `evals/`（当年的相对路径产物）和一个 `tmp/`。
这个脚本按「目录作用」把它们归位，不动任何内容。

用法：
    python scripts/migrate_task_layout.py [--apply]
不带 --apply 只打印计划。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 旧证据目录名
LEGACY_RECORD = "_records"
# 这些顶层名字一看就是模型写的中间产物（旧任务里摊在根上的那批也是）
INTERMEDIATE_HINTS = {"tmp", "temp", "drafts", "scratch", "cache", "work"}
# 这些是任务产出（当年相对路径带前缀写进产出目录的）
OUTPUT_HINTS = {"evals", "docs", "reports"}


def plan(task: Path) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    record = task / "record"
    output = task / "output"
    for child in sorted(task.iterdir()):
        name = child.name
        if name in {"record", "output"}:
            continue
        if child.is_dir():
            if name == LEGACY_RECORD:
                moves.append((child, record))
            elif name in INTERMEDIATE_HINTS:
                moves.append((child, record / "work" / name))
            elif name in OUTPUT_HINTS:
                moves.append((child, output / name))
            else:
                # 认不出的目录：当中间产物收进 work/，宁可多留不误删
                moves.append((child, record / "work" / name))
            continue
        # 散落在任务根上的文件：旧版「相对路径一律落任务目录」的遗留。
        # 报告类（md/txt/json/csv）多半就是当年要交的东西 → output/；
        # 其余（日志、临时脚本、备份）当中间产物 → record/work/。
        if child.suffix.lower() in {".md", ".txt", ".json", ".csv", ".html"} \
                and not any(k in name.lower() for k in ("tmp", "temp", "draft", "log", "bak")):
            moves.append((child, output / name))
        else:
            moves.append((child, record / "work" / name))
    return moves


def main() -> None:
    apply = "--apply" in sys.argv
    total = 0
    for task in sorted((ROOT / "history").iterdir()):
        if not task.is_dir():
            continue
        moves = plan(task)
        if not moves:
            continue
        print(f"\n{task.name}")
        for src, dst in moves:
            rel_src = src.relative_to(ROOT).as_posix()
            rel_dst = dst.relative_to(ROOT).as_posix()
            print(f"  {rel_src}  →  {rel_dst}")
            if apply:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    # 目标已存在（例如 record/ 已经迁过）：并进去，不覆盖
                    for item in src.iterdir():
                        target = dst / item.name
                        if target.exists():
                            print(f"    ! 跳过已存在：{target.relative_to(ROOT)}")
                            continue
                        item.rename(target)
                    if src.is_dir():
                        try:
                            src.rmdir()
                        except OSError:
                            pass
                else:
                    src.rename(dst)
            total += 1
    print(f"\n{'已执行' if apply else '计划'} {total} 项")


if __name__ == "__main__":
    main()
