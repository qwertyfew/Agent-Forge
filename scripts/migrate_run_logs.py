# -*- coding: utf-8 -*-
"""把 `.run/` 里每一轮的运行日志归到对应任务的案卷里。

为什么：一次任务现在有 `history/<任务>/{record,output}` 两层案卷，但派发器把
事件流、原始输出、答复全文都写在了 `.run/` 根上（`rerun31.events.jsonl` 之类），
复盘时仍然要「按编号去 .run 翻日志、去 history 找目录」两头对。

这个脚本按任务台账（`.tasks.json`）里的【本轮编号：rerunN】把 run 编号映射到
任务创建时间（`layout.task_dir_for_run`），再落到同名历史目录：

    history/<任务>/record/run/
      events.jsonl    SSE 事件流（前端看到的全部事件，逐条）
      raw.log         原始流（含 delta 片段）
      reply.txt       最终答复全文
      tools.txt       工具调用序列
      prompt.txt      本轮任务书

找不到对应任务目录的（历史目录早被清理的 rerun1..N），统一归到
`history/_archive/run-logs/<run>/`，不再堆在 `.run/` 根上。

用法：
    python scripts/migrate_run_logs.py           # 只打印计划
    python scripts/migrate_run_logs.py --apply    # 真的搬
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import layout                                 # noqa: E402

RUN = ROOT / ".run"
HISTORY = ROOT / "history"
ARCHIVE = HISTORY / "_archive" / "run-logs"
_SUFFIX = (".events.jsonl", ".raw.log", ".reply.txt", ".tools.txt", ".prompt.txt")
_TARGET = {"events.jsonl": "events.jsonl", "raw.log": "raw.log",
           "reply.txt": "reply.txt", "tools.txt": "tools.txt",
           "prompt.txt": "prompt.txt"}


def _split(name: str) -> tuple[str, str] | None:
    for suf in _SUFFIX:
        if name.endswith(suf):
            return name[: -len(suf)], _TARGET[suf.lstrip(".")]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的搬（默认只打印）")
    args = ap.parse_args()

    plan: list[tuple[Path, Path]] = []
    for f in sorted(RUN.iterdir()):
        if not f.is_file():
            continue
        parts = _split(f.name)
        if not parts:
            continue
        run, target = parts
        if not re.fullmatch(r"(?:rerun|probe)\d+", run):
            continue                     # service.log / restart 等运行期文件不动
        d = layout.task_dir_for_run(ROOT, run)
        dst = (d / layout.RECORD / layout.RUN / target) if d \
            else (ARCHIVE / run / target)
        plan.append((f, dst))

    if not plan:
        print("没有需要搬的运行日志。")
        return

    for src, dst in plan:
        print(f"{src.name:32s} → {dst.relative_to(ROOT).as_posix()}")
    print(f"\n共 {len(plan)} 个文件，{len({p[1].parent for p in plan})} 个目标目录。")
    if not args.apply:
        print("（预览模式。加 --apply 执行。）")
        return

    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    print("已搬迁。")


if __name__ == "__main__":
    main()
