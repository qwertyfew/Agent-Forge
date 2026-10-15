from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .base import EXEC, make_tool


def build_shell_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    def run_command(command: str, cwd: str = "", timeout: int = 120) -> str:
        guard.check_command(command)

        # workspace_root 强转 Path：调用方（如 bench）若赋了 str，这里直接
        # AttributeError 会把整条 run_command 炸掉，宁在此处兜住。
        work_dir = guard.resolve(cwd) if cwd else Path(guard.cfg.workspace_root).resolve()
        if not work_dir.is_dir():
            return f"[错误] 工作目录不存在: {work_dir}"

        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("NO_COLOR", "1")
        # PATH 把**当前解释器所在目录**放最前：模型命令里写的 `python` 必须解析到
        # 跑 harness 的同一个解释器（评测在 .venv 里就是 .venv 的 python），而不是
        # 继承 PATH 碰到的——Windows 上那个经常是微软商店的占位符（exit=9009
        # "Python was not found"，第四轮评测 c2/c5 的脚本没错却跑不起来，f4 没了
        # python 验证只能肉眼复制）。与 verify.run_verify_cmd 同一套处理。
        exe_dir = str(Path(sys.executable).resolve().parent)
        env["PATH"] = (exe_dir + os.pathsep + str(Path(exe_dir) / "Scripts")
                       + os.pathsep + env.get("PATH", ""))

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
