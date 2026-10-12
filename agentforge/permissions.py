from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import Config

# 「当前这次工具调用是否放行工作区之外的路径」。完全访问权限的任务里为 True。
# 用 ContextVar 而不是 Guard 的实例属性：Web 端是 ThreadingHTTPServer，多个任务
# 各占一条线程却共用同一个 Guard，改实例属性会互相串味；ContextVar 按线程隔离，
# 每个 _execute 只影响自己这一次工具调用。
OUTSIDE_OK: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "af_outside_ok", default=False
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

    # ---------- 命令 ----------
    def check_command(self, command: str) -> None:
        low = command.strip().lower()
        for bad in self.cfg.command_blocklist:
            if low.startswith(bad) or f" {bad}" in f" {low}":
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
