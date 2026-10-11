from __future__ import annotations

import os
import subprocess

from .base import EXEC, make_tool


def build_shell_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    def run_command(command: str, cwd: str = "", timeout: int = 120) -> str:
        guard.check_command(command)

        work_dir = guard.resolve(cwd) if cwd else guard.cfg.workspace_root.resolve()
        if not work_dir.is_dir():
            return f"[错误] 工作目录不存在: {work_dir}"

        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("NO_COLOR", "1")

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, min(int(timeout), 600)),
                env=env,
            )
        except subprocess.TimeoutExpired:
            return f"[错误] 命令超时（>{timeout}s）: {command}"
        except OSError as e:
            return f"[错误] 命令执行失败: {e}"

        parts = [f"$ {command}", f"[cwd] {work_dir}", f"[exit] {proc.returncode}"]
        if proc.stdout.strip():
            parts.append("--- stdout ---\n" + proc.stdout.strip())
        if proc.stderr.strip():
            parts.append("--- stderr ---\n" + proc.stderr.strip())
        out = "\n".join(parts)
        if len(out) > max_out:
            out = out[: max_out // 2] + "\n...[中间已截断]...\n" + out[-max_out // 2 :]
        return out

    return [
        make_tool(
            "run_command",
            "在系统 shell 中执行命令（Windows 为 cmd）。用于跑测试、执行脚本、查看 git 状态等。命令前会做安全检查。",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的完整命令"},
                    "cwd": {"type": "string", "description": "执行目录，默认工作区根目录"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 120，上限 600"},
                },
                "required": ["command"],
            },
            run_command,
            EXEC,
        )
    ]
