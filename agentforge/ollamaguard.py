"""本地模型的上游守卫：Ollama 没起来就把它拉起来。

为什么需要这一层：本机 Ollama 虽然设了开机自启，但机器长期不关机——它可能被手动
退出、升级时重启、或干脆崩掉。这时项目里所有本地模型会静默失效，界面上只有一句
「Ollama 无响应」，得用户自己去开。这里把「检测 → 拉起 → 等它就绪」收成一件事，
让三条路径复用同一份实现：Web 启动时、界面上的健康检查、以及常驻服务脚本。

三个动作：
  probe()   问一句 /api/tags，判断在不在（Ollama 的原生探活接口）；
  locate()  找可执行文件（环境变量 → PATH → 常见安装位置）；
  ensure()  不在就拉起来，**并且等它真能应答**才算成功——只看进程有没有生出来
            是不够的，冷启动要几秒才监听端口。

只做「拉起」，不做「杀掉」：关 Ollama 是用户的决定，不该由我们代劳。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

# 拉起后默认最多等这么久（秒）。冷启动要加载模型索引，磁盘忙时会更慢。
DEFAULT_WAIT = 45
# 两次自动拉起之间的最小间隔（秒）。界面每 30 秒轮询一次健康状态，
# 没有这个冷却会变成「每次轮询都 spawn 一个 ollama」。
RETRY_COOLDOWN = 120
DEFAULT_PORT = 11434

_lock = threading.Lock()
_last_attempt = 0.0
_last_result: dict = {}


def probe(host: str, timeout: float = 5.0) -> bool:
    """Ollama 在不在？/api/tags 是它的原生探活接口，比 /v1/models 更直接。"""
    try:
        r = requests.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def locate() -> str:
    """找 ollama 可执行文件（命令行版）。找不到返回空串。"""
    env = os.environ.get("OLLAMA_EXE")
    if env and Path(env).exists():
        return env
    found = shutil.which("ollama")
    if found:
        return found
    return _first_existing(_cli_candidates())


def locate_app() -> str:
    """找 Ollama 桌面版（托盘）的可执行文件。找不到返回空串。

    优先用它而不是 `ollama serve`：桌面版本就是用户平时开机自启那一份，
    拉起它等于还原用户熟悉的状态（带托盘图标），而且是单实例的——
    已经在跑就什么都不做，不会跟我们抢 11434 端口。
    """
    env = os.environ.get("OLLAMA_APP_EXE")
    if env and Path(env).exists():
        return env
    return _first_existing(_app_candidates())


def _cli_candidates() -> list[Path]:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        out = []
        if local:
            out.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
        out.append(Path(pf) / "Ollama" / "ollama.exe")
        return out
    return [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"),
            Path.home() / ".local" / "bin" / "ollama"]


def _app_candidates() -> list[Path]:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        out = []
        if local:
            out.append(Path(local) / "Programs" / "Ollama" / "ollama app.exe")
        out.append(Path(pf) / "Ollama" / "ollama app.exe")
        return out
    # macOS 的桌面版是一个 .app 包
    return [Path("/Applications/Ollama.app/Contents/MacOS/Ollama")]


def _first_existing(cands: list[Path]) -> str:
    for c in cands:
        try:
            if c.exists():
                return str(c)
        except OSError:
            continue
    return ""


def _launch_cmd() -> tuple[str, list[str], str]:
    """选一条拉起命令：返回 (可执行文件, 参数, 人话说明)。找不到就返回空串。"""
    app = locate_app()
    if app:
        return app, [], "Ollama 桌面版"
    cli = locate()
    if cli:
        return cli, ["serve"], "ollama serve"
    return "", [], ""


def _spawn(exe: str, args: list[str], host: str, log_path: Path) -> subprocess.Popen:
    """把 Ollama 拉起来，日志并到一个文件里，方便事后看它为什么没起来。"""
    env = dict(os.environ)
    # 让新起的服务监听在我们探测的那个地址上。只在端口非默认时设置，
    # 免得覆盖用户自己已经配好的 OLLAMA_HOST。
    if not env.get("OLLAMA_HOST"):
        parsed = urlparse(host)
        if parsed.port and parsed.port != DEFAULT_PORT:
            env["OLLAMA_HOST"] = f"{parsed.hostname or '127.0.0.1'}:{parsed.port}"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a", encoding="utf-8", errors="replace")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} 自动拉起：{exe} {' '.join(args)} ---\n")
    log.flush()

    kwargs: dict = {
        "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
        "env": env, "close_fds": True,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS：不挂到我们的控制台，也不跟我们的进程树一起被回收；
        # 这样 Web 界面重启、Ollama 都还在。
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([exe, *args], **kwargs)


def ensure(cfg, wait: int | None = None, force: bool = False,
           log_path: Path | None = None) -> dict:
    """确保 Ollama 可用。返回 {ok, action, detail, waited, exe}。

    action 取值：
      already_up  本来就在跑，什么都没做
      started     拉起来了，并且等到它应答
      cooldown    刚试过没多久，这次不重复拉（避免界面轮询把进程刷爆）
      disabled    配置里关掉了自动拉起
      not_found   本机找不到 Ollama，得人工装
      failed      拉起来了但一直不应答（超时）
    """
    global _last_attempt, _last_result

    host = getattr(cfg, "ollama_host", "http://localhost:11434")
    if probe(host):
        return {"ok": True, "action": "already_up", "detail": "Ollama 本来就在跑",
                "waited": 0, "exe": ""}

    if not getattr(cfg, "ollama_auto_start", True):
        return {"ok": False, "action": "disabled",
                "detail": "Ollama 无响应，且配置里关了「自动拉起」", "waited": 0, "exe": ""}

    budget = int(wait if wait is not None else getattr(cfg, "ollama_wait", DEFAULT_WAIT))
    log_path = log_path or (Path(getattr(cfg, "workspace_root", ".")) / ".run" / "ollama.log")

    with _lock:
        now = time.time()
        if not force and _last_attempt and (now - _last_attempt) < RETRY_COOLDOWN:
            last = dict(_last_result)
            left = int(RETRY_COOLDOWN - (now - _last_attempt))
            last["action"] = "cooldown"
            last["detail"] = (last.get("detail") or "Ollama 仍无响应") + f"（{left}s 后可再试）"
            return last

        exe, args, label = _launch_cmd()
        if not exe:
            _last_attempt = now
            _last_result = {
                "ok": False, "action": "not_found", "waited": 0, "exe": "",
                "detail": "本机找不到 Ollama，请先安装（https://ollama.com/download），"
                          "或把 OLLAMA_EXE 设成 ollama 可执行文件的完整路径",
            }
            return dict(_last_result)

        _last_attempt = now
        try:
            _spawn(exe, args, host, log_path)
        except OSError as e:
            _last_result = {
                "ok": False, "action": "failed", "waited": 0, "exe": exe,
                "detail": f"拉起 {label} 失败：{e}",
            }
            return dict(_last_result)

        started = time.time()
        while time.time() - started < budget:
            if probe(host, timeout=2.0):
                _last_result = {
                    "ok": True, "action": "started", "exe": exe,
                    "waited": round(time.time() - started, 1),
                    "detail": f"已自动拉起 {label}（耗时 {round(time.time() - started, 1)}s）",
                }
                return dict(_last_result)
            time.sleep(1.0)

        _last_result = {
            "ok": False, "action": "failed", "exe": exe, "waited": budget,
            "detail": f"已尝试拉起 {label}，但 {budget}s 内仍未应答；"
                      f"日志见 {log_path}",
        }
        return dict(_last_result)


def last_result() -> dict:
    """上一次 ensure() 的结果。不触发任何探测，供高频接口（/api/health）安全调用。"""
    return dict(_last_result) if _last_result else {}


def launcher_label() -> str:
    """本机会用哪条命令拉起 Ollama（只查文件，不 spawn）。找不到返回空串。"""
    _exe, _args, label = _launch_cmd()
    return label


def snapshot(cfg) -> dict:
    """给界面看的当前状态（不触发任何拉起动作）。"""
    host = getattr(cfg, "ollama_host", "http://localhost:11434")
    up = probe(host)
    exe, _args, label = _launch_cmd()
    return {
        "up": up,
        "host": host,
        "auto_start": bool(getattr(cfg, "ollama_auto_start", True)),
        "exe": exe,
        "launcher": label,
        "last": dict(_last_result) if _last_result else {},
    }
