"""git 工具集：只读操作默认放行，写操作走审批。

注意：为避免交互式卡住（credential 弹窗 / 编辑器等待），
所有命令都加了 GIT_TERMINAL_PROMPT=0 与 -c core.editor=true。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import EXEC, SAFE, make_tool

# 非交互兜底：不让 git 去问密码或打开编辑器
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "true",
    "GCM_INTERACTIVE": "never",
}


def _run(args: list[str], cwd: str) -> str:
    env = dict(os.environ)
    env.update(_ENV)
    try:
        p = subprocess.run(
            ["git", "-c", "core.editor=true", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        return "错误：本机没有安装 git，或 git 不在 PATH 里。"
    except subprocess.TimeoutExpired:
        return "错误：git 命令超时（60 秒）。"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    body = out or err or "(无输出)"
    if len(body) > 12000:
        body = body[:6000] + "\n...(截断)...\n" + body[-4000:]
    return f"[exit {p.returncode}]\n{body}"


def _detect_repo(cfg) -> str:
    """确定 git 命令的工作目录。

    工作区根目录本身常常不是仓库（真正的项目在它的子目录里），
    所以按 配置 → 工作区根 → 一层子目录 的顺序找一个含 .git 的地方。
    """
    if cfg.git_root:
        return str(cfg.git_root)
    root = Path(cfg.workspace_root)
    if (root / ".git").exists():
        return str(root)
    try:
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and (sub / ".git").exists():
                return str(sub)
    except OSError:
        pass
    return str(root)


def build_git_tools(guard, cfg):
    root = _detect_repo(cfg)

    return [
        make_tool(
            name="git_status",
            description="查看当前仓库的改动状态（git status --short）。",
            parameters={"type": "object", "properties": {}, "required": []},
            fn=lambda: _run(["status", "--short", "-b"], root),
            risk=SAFE,
        ),
        make_tool(
            name="git_diff",
            description="查看未提交的改动内容。可指定文件路径缩小范围。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "可选，限定某个文件或目录"}
                },
                "required": [],
            },
            fn=lambda path="": _run(
                ["diff", "--", path] if path else ["diff"], root
            ),
            risk=SAFE,
        ),
        make_tool(
            name="git_log",
            description="查看最近的提交记录。默认 10 条，一行一条。",
            parameters={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "条数，默认 10"}
                },
                "required": [],
            },
            fn=lambda count=10: _run(
                ["log", "--oneline", "-n", str(max(1, min(int(count or 10), 100)))], root
            ),
            risk=SAFE,
        ),
        make_tool(
            name="git_show",
            description="查看某次提交的完整内容（git show）。",
            parameters={
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "提交哈希或 HEAD~1 之类"}
                },
                "required": ["ref"],
            },
            fn=lambda ref: _run(["show", "--stat", "--patch", ref], root),
            risk=SAFE,
        ),
        make_tool(
            name="git_add",
            description="把文件加入暂存区（git add）。",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件或目录路径"}},
                "required": ["path"],
            },
            fn=lambda path: _run(["add", "--", path], root),
            risk=EXEC,
        ),
        make_tool(
            name="git_commit",
            description="提交暂存区的改动。message 必填，写清楚改了什么。",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "提交说明"},
                },
                "required": ["message"],
            },
            fn=lambda message: _run(["commit", "-m", message], root),
            risk=EXEC,
        ),
        make_tool(
            name="git_clone",
            description=(
                "克隆远程仓库到指定目录。走本机 git 配置，"
                "GitHub 已配 SSH，不会弹授权窗口。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "仓库地址"},
                    "dest": {"type": "string", "description": "目标目录名"},
                    "depth": {"type": "integer", "description": "浅克隆深度，默认 1"},
                },
                "required": ["url", "dest"],
            },
            fn=lambda url, dest, depth=1: _run(
                ["clone", "--depth", str(int(depth or 1)), "--", url, dest], root
            ),
            risk=EXEC,
        ),
        make_tool(
            name="git_push",
            description=(
                "推送到远程。已通过 SSH + insteadOf 配置免密，不会弹窗。"
                "若网络不通会直接报错而不是卡住。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "remote": {"type": "string", "description": "默认 origin"},
                    "branch": {"type": "string", "description": "默认当前分支"},
                },
                "required": [],
            },
            fn=lambda remote="origin", branch="": _run(
                ["push", remote, branch] if branch else ["push", remote], root
            ),
            risk=EXEC,
        ),
    ]
