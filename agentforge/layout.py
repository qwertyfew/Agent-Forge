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

import datetime as _dt
import json
import os
import re
from pathlib import Path

RECORD = "record"
OUTPUT = "output"
WORK = "work"
RUN = "run"
# 旧版本目录名：迁移时保留读取兼容（历史任务里还有 _records）
LEGACY_RECORD = "_records"

_RUN_TAG_RX = re.compile(r"【本轮编号：([A-Za-z0-9_\-]+)】")


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


def task_dir_for_run(root: str | Path, run: str,
                     tolerance: float = 300.0) -> Path | None:
    """按任务台账把「轮次编号」映射到 `history/<任务>`（找不到返回 None）。

    派发器只知道本轮编号（rerun33），并不知道任务目录名——目录名是编排层按
    发起时间算的。台账里的任务 prompt 带【本轮编号：X】，created_at 就是那个
    时间，两边一对就能把「这一轮的运行日志」归进「这一轮的案卷」。

    目录名与 created_at 可能差 1 秒（截断 vs 取整），所以先试算出来的名字，
    再在 tolerant 窗口内找最近的目录。
    """
    base = Path(root)
    ledger = base / ".tasks.json"
    try:
        tasks = json.loads(ledger.read_text(encoding="utf-8"))["tasks"]
    except (OSError, ValueError, KeyError):
        return None
    ts = None
    for t in tasks:
        m = _RUN_TAG_RX.search(t.get("prompt") or t.get("title") or "")
        if not m or m.group(1) != run:
            continue
        cand = float(t.get("created_at") or 0)
        if cand and (ts is None or cand < ts):
            ts = cand
    if not ts:
        return None
    name = _dt.datetime.fromtimestamp(ts).strftime("%Y%m%d-%H%M%S")
    exact = base / "history" / name
    if exact.is_dir():
        return exact
    try:
        dirs = [p for p in (base / "history").iterdir()
                if p.is_dir() and not p.name.startswith("_")]
    except OSError:
        return None
    best, gap = None, tolerance
    for p in dirs:
        try:
            d = abs(_dt.datetime.strptime(p.name, "%Y%m%d-%H%M%S").timestamp() - ts)
        except ValueError:
            continue
        if d <= gap:
            best, gap = p, d
    return best


def record_run_dir(root: str | Path, run: str) -> Path | None:
    """这一轮的运行日志该放哪：`history/<任务>/record/run/`。"""
    d = task_dir_for_run(root, run)
    return (d / RECORD / RUN) if d else None


# ---------- output/ 交付区（P28：主写位置，不是收尾镜像） ----------
#
# 2026-10-23 起 output/ 从「收尾时镜像的案卷副本」升级为**交付物主写区**：
# 任务书点名的路径（WRITE_ALLOW）由 resolve_for_write 直接写进
# `history/<任务>/output/<声明路径>`；未点名的相对路径仍进 record/work/。
# 工作区根回归「种子 + harness 自身文件」。
#
# 模型的可见世界 = output/（种子在任务发起时拷一份进来，读写都在这里），
# 读侧镜像（resolve_for_read）按 output/ → record/work/ → 根 的顺序解析，
# 保证「写进哪、从哪读」自洽。

_SEED_COPY_EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    "history", ".agentforge", ".sessions", ".workbuddy", ".idea", ".vscode",
}
_SEED_COPY_MAX_FILE = 5 * 1024 * 1024      # 单文件 >5MB 不拷（大文件工具读不“顺”）
_SEED_COPY_MAX_FILES = 500                 # 总数上限：真实仓库任务也不至于失控
_SEED_COPY_MAX_BYTES = 64 * 1024 * 1024    # 总量上限：500 个文件也不该吃掉几百 MB


def seed_output(root: str | Path, outdir: str) -> Path:
    """任务发起时把工作区种子拷进 `history/<任务>/output/`（有界复制）。

    这是「模型可见世界 = output/」的前提：种子（评测用例材料 / 用户工程文件）
    拷一份进交付区，之后模型的读写、run_command、ops_batch 脚本的 cwd 都在
    output/ 上——「写完就运行」「改种子文件」与「新写交付物」三种工作流在
    同一个目录里自洽。返回 output 目录的绝对路径。

    有界：跳过排除目录（.git/.venv/history…）、>5MB 的单文件、总量 500 文件 +
    64MB 字节——评测沙盒只有几颗种子必然全量拷入；真实仓库任务也不会失控。
    拷不全的部分由读侧镜像的「根回退」兜底（工具读仍可达），只是脚本内裸 IO
    看不见。

    为什么用 os.walk 剪枝而不是 rglob：rglob 会把排除目录**整棵走一遍**再判断
    要不要拷——.venv / node_modules / history 动辄几万个文件，走目录本身就要
    1~4 秒（实测），而真正拷进去的不过几百个。剪枝后同一仓库降到 0.1 秒级，
    等于把开局白等的时间直接还给了用户。
    """
    import shutil

    base = Path(root).resolve()
    dst_root = base / output_dir(outdir)
    dst_root.mkdir(parents=True, exist_ok=True)
    if outdir.rstrip("/").split("/")[-1] in _SEED_COPY_EXCLUDE_DIRS:
        return dst_root
    n = 0
    copied = 0
    for cur, dirs, files in os.walk(base):
        # 原地剪枝：排除目录既不进、也不出现在后续遍历里
        dirs[:] = sorted(d for d in dirs if d not in _SEED_COPY_EXCLUDE_DIRS)
        rel_dir = Path(cur).relative_to(base)
        for name in sorted(files):
            if n >= _SEED_COPY_MAX_FILES or copied >= _SEED_COPY_MAX_BYTES:
                return dst_root
            src = Path(cur) / name
            try:
                size = src.stat().st_size
            except OSError:
                continue
            if size > _SEED_COPY_MAX_FILE:
                continue
            if copied + size > _SEED_COPY_MAX_BYTES:
                return dst_root
            dst = dst_root / rel_dir / name
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError:
                continue
            n += 1
            copied += size
    return dst_root


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
