from __future__ import annotations

import contextvars
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from .core.config import Config
from .layout import output_dir, record_dir

# 「当前这次工具调用是否放行工作区之外的路径」。完全访问权限的任务里为 True。
# 用 ContextVar 而不是 Guard 的实例属性：Web 端是 ThreadingHTTPServer，多个任务
# 各占一条线程却共用同一个 Guard，改实例属性会互相串味；ContextVar 按线程隔离，
# 每个 _execute 只影响自己这一次工具调用。
OUTSIDE_OK: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "af_outside_ok", default=False
)

# 产出目录（架构级写入保证）：任务设有产出目录时，write_file 的相对路径
# 一律强制重定向进该目录——不依赖模型自觉。随每次工具调用注入（与
# OUTSIDE_OK 同一模式），泳道线程各自持有，互不串扰。
OUTDIR_CTX: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "af_outdir", default=None
)
# 写入放行名单：任务书里点名的相对路径（编排层从任务文本提取）。
# 命中名单的路径按原样解析——这是「必须放特定位置才能生效」的例外通道。
WRITE_ALLOW: "contextvars.ContextVar[frozenset]" = contextvars.ContextVar(
    "af_write_allow", default=frozenset()
)


class PermissionDenied(Exception):
    pass


class Guard:
    """工作区边界 + 命令黑名单 + 人工审批。

    路径边界有两个放行开关，命中任意一个就不再拦：
    - cfg.allow_outside：config.yaml [workspace] 里的静态开关，永远生效；
    - OUTSIDE_OK：随每次工具调用注入，完全访问权限（auto_approve=True）的任务
      里为 True，见 loop.Agent._execute。
    命令黑名单不随权限放开——那是防误删误格盘的底线，两种模式都拦。
    """

    def __init__(self, cfg: Config, approver=None):
        self.cfg = cfg
        # approver(tool_name, args) -> bool，None 表示全部自动放行
        self.approver = approver
        self.auto_approve_all = False

    # ---------- 路径 ----------
    def resolve(self, raw: str | Path) -> Path:
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = self.cfg.workspace_root / p
        p = p.resolve()
        if self.cfg.allow_outside or OUTSIDE_OK.get():
            return p
        root = self.cfg.workspace_root.resolve()
        try:
            p.relative_to(root)
        except ValueError:
            raise PermissionDenied(
                f"路径越出工作区，已拒绝：{p}\n工作区根目录：{root}"
            )
        return p

    def resolve_for_write(self, raw: str | Path) -> Path:
        """写类工具的路径解析：任务目录两级落点的**架构级保证**（P28）。

        任务设有产出目录（OUTDIR_CTX = history/<任务>）时：
        - 任务书点名的相对路径（WRITE_ALLOW，编排层从任务书提取，含 P23 裸
          文件名与 P27 组合路径）→ **交付区** `history/<任务>/output/<路径>`；
        - 其余相对路径 → **中间/过渡文件区** `history/<任务>/record/work/<路径>`；
        - 已经落在任务目录里的路径（模型写了 history/…、output/…、record/…）
          按原样解析，不二次嵌套；
        - 绝对路径不重定向（模型显式意图，工作区边界规则照常生效）。

        「中间文件进 record/、交付物进 output/」由代码保证，不赌提示词。"""
        p = Path(str(raw))
        outdir = OUTDIR_CTX.get()
        if outdir is None or p.is_absolute():
            return self.resolve(raw)
        norm = str(raw).strip().replace("\\", "/").lstrip("./")
        if not norm or norm == ".":
            return self.resolve(raw)
        if norm == outdir or norm.startswith(outdir.rstrip("/") + "/"):
            return self.resolve(raw)
        if norm == "output" or norm.startswith("output/"):
            # 模型视角的 output/… 就是交付区根下的同路径
            return self.resolve(Path(output_dir(outdir)) / norm[len("output/"):].lstrip("/"))
        if norm == "record" or norm.startswith("record/"):
            return self.resolve(Path(record_dir(outdir)) / norm[len("record/"):].lstrip("/"))
        allow = WRITE_ALLOW.get() or frozenset()
        if norm in allow:
            # 点名交付：进交付区，按声明路径保留子目录
            return self.resolve(Path(output_dir(outdir)) / norm)
        # 中间/过渡文件统一进「过程记录」的 work/ 下，按模型给的相对路径
        # 保留子目录（草稿、提取片段、临时脚本各归各位）。
        return self.resolve(Path(record_dir(outdir)) / "work" / norm)

    def resolve_for_read(self, raw: str | Path) -> Path:
        """读类工具的路径解析：与写侧镜像（P28 读侧对称）。

        解析顺序：交付区 output/ → 中间文件区 record/work/ → 工作区根。
        「写进哪、从哪读」自洽：模型写 notes/step1.json（进 work/）后用同一路径
        read_file 必须能读回；任务书点名的交付物（进 output/）同理。都不存在时
        返回第一候选（output/），报错信息由调用方给出。
        """
        p = Path(str(raw)).expanduser()
        if p.is_absolute():
            return self.resolve(p)
        outdir = OUTDIR_CTX.get()
        norm = str(raw).strip().replace("\\", "/").lstrip("./")
        if not outdir:
            return self.resolve(raw or ".")
        if not norm:
            # "." / "./" → 模型的家 = 交付区 output/（list_dir 的默认视角）
            return self.resolve(Path(output_dir(outdir)))
        # 模型显式写了 output/… / record/… 前缀：直接映射进任务目录
        if norm == "output" or norm.startswith("output/"):
            return self.resolve(Path(output_dir(outdir)) / norm[len("output/"):].lstrip("/"))
        if norm == "record" or norm.startswith("record/"):
            return self.resolve(Path(record_dir(outdir)) / norm[len("record/"):].lstrip("/"))
        out_p = self.resolve(Path(output_dir(outdir)) / norm)
        if out_p.exists():
            return out_p
        work_p = self.resolve(Path(record_dir(outdir)) / "work" / norm)
        if work_p.exists():
            return work_p
        root_p = self.resolve(norm)
        if root_p.exists():
            return root_p
        return out_p

    # ---------- 命令 ----------
    def check_command(self, command: str) -> None:
        # 黑名单是「两种权限模式下都拦的底线」。匹配前先把连续空白归一成单个
        # 空格（黑名单条目同样归一）：否则「del  /s」「del\t/s」「rm  -rf」这类
        # 空白变体按原样做子串匹配时全部漏网，底线就成了筛子。
        low = re.sub(r"\s+", " ", command.strip().lower())
        for bad in self.cfg.command_blocklist:
            bad = re.sub(r"\s+", " ", str(bad).strip().lower())
            if bad and (low.startswith(bad) or f" {bad}" in f" {low}"):
                raise PermissionDenied(f"命令命中黑名单，已拒绝：{command}")

    # ---------- 审批 ----------
    def needs_approval(self, tool_name: str) -> bool:
        if self.auto_approve_all:
            return False
        return tool_name in self.cfg.require_approval

    def approve(self, tool_name: str, args: dict) -> bool:
        if not self.needs_approval(tool_name):
            return True
        if self.approver is None:
            return False
        return bool(self.approver(tool_name, args))


class _WriteClaims:
    """并发写同一个文件时的串行闸（进程级、按绝对路径）。

    多 agent 并行执行时，两个互不相关的任务仍可能恰好改到同一个文件：
    谁先写完谁后写，后写的会盖掉先写的（丢失更新）。这里给每个文件一把锁，
    同一个文件的写入排队进行；写入不同文件的任务互不影响，不损失并行度。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk
            return lk

    @contextmanager
    def hold(self, key: str):
        lk = self._lock_for(key)
        lk.acquire()
        try:
            yield
        finally:
            lk.release()


WRITE_CLAIMS = _WriteClaims()


@contextmanager
def claim_write(path: str):
    """给某个文件的写入排队；不同文件互不阻塞，path 为空时直接放行。"""
    if not path:
        yield
        return
    with WRITE_CLAIMS.hold(path):
        yield
