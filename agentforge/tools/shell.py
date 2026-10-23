from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .base import EXEC, make_tool
from .. import skeleton

# 写盘类 shell 动词（Windows cmd + 常见 POSIX/PowerShell 形态）
_WRITE_VERBS = {"cp", "mv", "copy", "move", "robocopy", "tee",
                "set-content", "out-file", "add-content",
                "copy-item", "move-item"}
_REDIRECT_RX = re.compile(r">{1,2}\s*([^\s>]+)")


def _shell_write_block(command: str, guard) -> str | None:
    """登记过的交付物（骨架建档文件）禁止 shell 落盘。

    写闸只覆盖 write_file / fill_section / edit_file 三个入口，shell 的
    cp/重定向是第四条路——rerun39 实证：模型被证据列闸门拒绝后按旧话术用
    run_command cp 把无锚点草稿盖过闸门，交付 0 引用还全绿。登记文件对
    shell 关闭，内容变更必须走核查链覆盖的入口。
    """
    try:
        registered = skeleton.registered_paths(guard.cfg.workspace_root)
    except Exception:  # noqa: BLE001 —— 拦截失败不该影响命令执行
        return None
    if not registered:
        return None
    cmd = str(command or "")
    tokens = re.findall(r"[^\s=]+", cmd)
    targets: list[str] = []
    words = [t.lower().strip("\"'") for t in tokens]
    if words and words[0] in _WRITE_VERBS:
        for t in reversed(tokens):              # 目的地 = 最后一个非 flag 参数
            if t.lower().startswith(("-", "/")) and len(t) <= 3:
                continue
            targets.append(t)
            break
    for m in _REDIRECT_RX.finditer(cmd):
        targets.append(m.group(1))
    for t in targets:
        t = t.strip("\"'")
        if not t:
            continue
        try:
            cand = guard.resolve(t)
        except Exception:  # noqa: BLE001
            continue
        if str(Path(cand).resolve()).lower() in registered:
            return ("[错误] shell 落盘已被拦：" + str(cand) +
                    " 是登记过的交付物（骨架建档），cp/重定向会绕过证据核查链。"
                    "内容变更请走 write_file（把草稿改好后原样提交）或 "
                    "fill_section / edit_file。")
    return None


def build_shell_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    def run_command(command: str, cwd: str = "", timeout: int = 120) -> str:
        guard.check_command(command)

        block = _shell_write_block(command, guard)
        if block:
            return block

        # workspace_root 强转 Path：调用方（如 bench）若赋了 str，这里直接
        # AttributeError 会把整条 run_command 炸掉，宁在此处兜住。
        # 默认 cwd 走读侧镜像（P28）：任务设有产出目录时落 `output/`——种子
        # 在任务发起时已拷进去，「写完就运行」「跑测试」都在模型可见世界里。
        _rd = getattr(guard, "resolve_for_read", None)
        if _rd is not None:
            work_dir = _rd(cwd or ".")
        else:
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
            # rerun12 实测：同一条全库 grep 原样重跑 3 次，每次都顶格 16K 截断，
            # 纯烧上下文。截断时必须把「怎么缩小范围」写在结果里，而不是指望
            # 模型自己意识到该换命令。
            out = (
                out[: max_out // 2]
                + "\n...[中间已截断]...\n"
                + out[-max_out // 2 :]
                + f"\n[提示] 输出超过 {max_out} 字符上限已截断，原样重跑只会得到同样的截断。"
                "请缩小范围后重试：指定更深的子目录、--include=*.py 限定文件类型、"
                "管道接 head -n 30 限制行数。"
            )
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
