"""任务目录布局：一次任务的全部文件都在 `history/<任务>/` 里。

目录约定（与 dsh 的 session 目录同思路：过程与产物分开，且都落在同一处）：

    history/<任务>/
      record/                 过程记录（harness 写，不由模型写）
        evidence.jsonl        「读过哪些文件的哪些行」「每次写盘落成哪个版本」
        citations.jsonl       每次写盘的引用核查结论（refs / with_line / inside_read）
        versions/<扁平路径>/  每次成功写盘的全文快照（谁也不会被覆盖）
        work/                 模型写的中间/过渡文件（草稿、提取片段、临时脚本）
      output/                 最终产出（收尾时按声明路径镜像一份，可含子目录）

为什么要分开：复盘一次任务时只问两个问题——「产出的文件对不对」看 `output/`；
「过程有没有问题」看 `record/`（读过什么、写过哪几版、引用核查的结论）。
两件事不混在一个平铺目录里，才谈得上「用文件定位问题」。

放在 history/ 之外的东西不算任务产物：仓库里的 `evals/`、`docs/` 只放人写的
评测报告与设计文档；模型产出的文件一律进 `record/work/`（中间）或 `output/`
（最终），二者都由 harness 保证，不靠提示词。
"""
from __future__ import annotations

from pathlib import Path

RECORD = "record"
OUTPUT = "output"
WORK = "work"
# 旧版本目录名：迁移时保留读取兼容（历史任务里还有 _records）
LEGACY_RECORD = "_records"


def task_dir(outdir: str) -> str:
    """任务目录（相对工作区）。outdir 形如 history/20261020-105539。"""
    return str(outdir or "").rstrip("/")


def record_dir(outdir: str) -> str:
    return f"{task_dir(outdir)}/{RECORD}" if outdir else f"history/{RECORD}"


def work_dir(outdir: str) -> str:
    return f"{record_dir(outdir)}/{WORK}"


def output_dir(outdir: str) -> str:
    return f"{task_dir(outdir)}/{OUTPUT}" if outdir else ""


def under_history(rel: str) -> bool:
    """这个相对路径是不是已经在 history/ 任务目录里（镜像时跳过，避免自我嵌套）。"""
    parts = Path(str(rel)).parts
    return bool(parts) and parts[0].lower() == "history"


def mirror_outputs(files: list[dict] | None, root: str | Path, outdir: str,
                   cap: int = 50) -> list[str]:
    """把本任务的产出按声明路径镜像一份到 `output/`。返回镜像成功的相对路径。

    交付物本身必须留在任务书写的位置（代码类任务改的就是仓库里的文件，搬走会
    把工程改坏）。所以 `output/` 是**案卷副本**：收尾时按当时的磁盘内容拷一份，
    让一次任务的全部产出在一处可见、可对比、可整体归档。
    """
    import shutil

    base = Path(root)
    dst_root = base / output_dir(outdir)
    done: list[str] = []
    seen: set[str] = set()
    for rec in files or []:
        raw = str((rec or {}).get("path") or "")
        if not raw or raw in seen or len(done) >= cap:
            continue
        seen.add(raw)
        src = Path(raw)
        if not src.is_absolute():
            src = base / raw
        try:
            if not src.is_file():
                continue
            rel = src.resolve().relative_to(base.resolve())
        except (OSError, ValueError):
            continue
        rel_s = rel.as_posix()
        if under_history(rel_s):
            continue
        dst = dst_root / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            done.append(rel_s)
        except OSError:
            continue
    return done
