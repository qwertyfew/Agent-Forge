"""远程服务器（SSH）工具族。

让 Agent 在用户配置好 SSH 免密（公钥已放服务器、~/.ssh/config 可选）后：
- ssh_run        在云服务器上执行任意命令
- ssh_info       连接诊断 + 系统/硬件/Ollama 环境探测
- ssh_upload     上传工作区文件到服务器
- ollama_deploy  在服务器上部署 Ollama 并拉取模型

设计要点：
- 一律 BatchMode=yes：免密失败立刻报错，绝不挂起等密码输入
- host 参数直接透传给 ssh，支持 user@ip、IP、或 ~/.ssh/config 里的别名
- 除 ssh_info 外均为 EXEC 风险级，默认列入 require_approval 人工确认
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .base import EXEC, SAFE, make_tool

_SSH_BASE = [
    "ssh",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
]

_INFO_SCRIPT = (
    "echo '--- 系统 ---'; cat /etc/os-release 2>/dev/null | head -2; uname -m; "
    "echo '--- CPU/内存 ---'; nproc; free -h | awk 'NR==2{print $2}'; "
    "echo '--- 磁盘 ---'; df -h / | awk 'NR==2{print $2\" 总 / \"$4\" 余\"}'; "
    "echo '--- GPU ---'; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo '无 NVIDIA GPU 或未装驱动'; "
    "echo '--- Ollama ---'; command -v ollama >/dev/null && ollama --version || echo '未安装'; "
    "systemctl is-active ollama 2>/dev/null || true"
)

_DEPLOY_SCRIPT = (
    "set -e; "
    "if ! command -v ollama >/dev/null 2>&1; then "
    "  echo '[1/3] 安装 Ollama...'; "
    "  curl -fsSL https://ollama.com/install.sh | sh >/dev/null 2>&1 || "
    "  curl -fsSL https://gitee.com/mirrors/ollama-install/raw/branch/main/install.sh | sh; "
    "else echo '[1/3] Ollama 已安装，跳过'; fi; "
    "echo '[2/3] 拉取模型 {model}...'; "
    "ollama pull {model}; "
    "echo '[3/3] 验证:'; ollama list | grep -F '{model}' || (echo '拉取失败'; exit 1)"
)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[中间已截断]...\n" + text[-limit // 2 :]


def build_remote_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return -1, f"[错误] 操作超时（>{timeout}s）"
        except FileNotFoundError:
            return -1, "[错误] 本机未找到 ssh/scp（Windows 请在「可选功能」里安装 OpenSSH 客户端）"
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()

    def _fail_hint(code: int, out: str) -> str:
        if "Permission denied" in out or "publickey" in out:
            return (
                f"[错误] SSH 认证失败（exit {code}）。\n"
                "免密配置方法：\n"
                "  1. ssh-keygen -t ed25519（已有密钥可跳过，本机已有 id_ed25519）\n"
                "  2. 把 ~/.ssh/id_ed25519.pub 内容追加到服务器的 ~/.ssh/authorized_keys\n"
                "     （或用 ssh-copy-id user@服务器，Windows 可手动追加）\n"
                "  3. 可选：在 ~/.ssh/config 里加 Host 别名"
            )
        if "Could not resolve hostname" in out:
            return f"[错误] 主机名无法解析（exit {code}）。请检查 host 写法（user@ip 或 ~/.ssh/config 别名）。"
        if "Connection timed out" in out or "Connection refused" in out:
            return f"[错误] 连接失败（exit {code}）。检查 IP/端口，以及云厂商安全组是否放行 22 端口。\n{out[:300]}"
        return f"[错误] SSH 操作失败（exit {code}）:\n{out[:800]}"

    # ---------- ssh_run ----------
    def ssh_run(host: str, command: str, timeout: int = 120) -> str:
        code, out = _run(
            [*_SSH_BASE, host, command], max(5, min(int(timeout), 1800))
        )
        if code != 0:
            return _fail_hint(code, out)
        return f"[{host}] $ {command}\n{out}"[:max_out]

    # ---------- ssh_info ----------
    def ssh_info(host: str) -> str:
        code, out = _run([*_SSH_BASE, host, _INFO_SCRIPT], timeout=60)
        if code != 0:
            return _fail_hint(code, out)
        return f"[{host}] 连接成功\n{out}"[:max_out]

    # ---------- ssh_upload ----------
    def ssh_upload(host: str, local_path: str, remote_path: str) -> str:
        p = Path(guard.resolve(local_path))
        if not p.exists():
            return f"[错误] 本地文件不存在: {p}"
        if p.is_dir():
            return "[错误] 暂只支持单文件上传，目录请先打包"
        code, out = _run(
            [
                "scp", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new",
                str(p), f"{host}:{remote_path}",
            ],
            timeout=600,
        )
        if code != 0:
            return _fail_hint(code, out)
        return f"已上传 {p} → {host}:{remote_path}"

    # ---------- ollama_deploy ----------
    def ollama_deploy(host: str, model: str, timeout: int = 900) -> str:
        if not re.fullmatch(r"[\w./:-]+", model):
            return f"[错误] 模型名不合法: {model}"
        script = _DEPLOY_SCRIPT.replace("{model}", model)
        code, out = _run(
            [*_SSH_BASE, host, script], max(60, min(int(timeout), 1800))
        )
        if code != 0:
            body = _fail_hint(code, out)
            if "curl" in out or "install.sh" in out:
                body += "\n提示: 服务器访问 ollama.com 可能被墙，可手动下载 install.sh 上传后执行。"
            return body
        return f"[{host}] 部署完成:\n{out}"[:max_out]

    return [
        make_tool(
            "ssh_info",
            "SSH 连接诊断：连上服务器并汇报系统版本、CPU/内存/磁盘、GPU、Ollama 安装情况。远程操作前先用它摸底。",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "user@ip、IP 或 ~/.ssh/config 里的 Host 别名"},
                },
                "required": ["host"],
            },
            ssh_info,
            SAFE,
        ),
        make_tool(
            "ssh_run",
            "在云服务器上执行 shell 命令（需已配 SSH 免密）。用于运维、查状态、改配置、部署服务等。",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "user@ip、IP 或 ~/.ssh/config 里的 Host 别名"},
                    "command": {"type": "string", "description": "要在服务器上执行的命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 120，上限 1800"},
                },
                "required": ["host", "command"],
            },
            ssh_run,
            EXEC,
        ),
        make_tool(
            "ssh_upload",
            "把工作区内的一个文件上传到服务器（scp）。目标路径需为绝对路径。",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "user@ip、IP 或 Host 别名"},
                    "local_path": {"type": "string", "description": "工作区内的本地文件路径"},
                    "remote_path": {"type": "string", "description": "服务器上的目标绝对路径"},
                },
                "required": ["host", "local_path", "remote_path"],
            },
            ssh_upload,
            EXEC,
        ),
        make_tool(
            "ollama_deploy",
            "在云服务器上部署 Ollama 并拉取指定模型（未安装会自动装，国内服务器走镜像源）。完成后自动验证。",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "user@ip、IP 或 Host 别名"},
                    "model": {"type": "string", "description": "Ollama 模型名，如 qwen3.5:4b"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 900，上限 1800（拉大模型建议调高）"},
                },
                "required": ["host", "model"],
            },
            ollama_deploy,
            EXEC,
        ),
    ]
