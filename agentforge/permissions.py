from __future__ import annotations

from pathlib import Path

from .config import Config


class PermissionDenied(Exception):
    pass


class Guard:
    """工作区边界 + 命令黑名单 + 人工审批。"""

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
        if self.cfg.allow_outside:
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
