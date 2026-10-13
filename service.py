#!/usr/bin/env python
"""AgentForge 常驻服务：让 http://127.0.0.1:8787 一直开着。

只跑 `run.py --web` 是不够的——它一旦因为任何原因退出（异常、OOM、窗口被关、
升级时重启），网页就打不开了，而这时人往往不在电脑前。这个脚本当「保姆」：

  1. 盯着端口：发现 8787 没人服务，就把 Web 服务拉起来；
  2. **只补位，不抢占**：端口上已经有人（比如你手动双击了 start-web.bat）就什么都不做，
     也绝不杀进程——杀掉可能正在跑的任务，代价远大于「等着它自己好」；
  3. 定时复查 Ollama（本地模型的上游），掉了同样拉回来；
  4. 日志只往 .run/service.log 写，不占窗口。

用法：
    pythonw service.py            # 无窗口常驻（推荐，start-service.bat 就是这么起）
    python  service.py            # 前台跑，Ctrl+C 停（适合排查）
    python  service.py --status   # 看一眼现在什么状态
    python  service.py --stop     # 停掉常驻服务与它拉起的 Web 服务
    python  service.py --no-ollama  # 这个实例不管 Ollama
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN = ROOT / ".run"
sys.path.insert(0, str(ROOT))

from agentforge import ollamaguard  # noqa: E402
from agentforge.config import Config  # noqa: E402

CHECK_INTERVAL = 10      # 每多少秒体检一次
OLLAMA_EVERY = 6         # 每几次体检复查一次 Ollama（≈1 分钟）
START_GRACE = 30         # 拉起后最多等多少秒算「起来了」
MAX_BACKOFF = 300        # 连续失败时的退避上限，别把日志刷爆

PID_FILE = RUN / "service.pid"
WEB_PID_FILE = RUN / "web.pid"
WEB_LOG = RUN / "web.log"
LOG_FILE = RUN / "service.log"


# ---------------------------------------------------------------- 日志
def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        RUN.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    # 前台运行时也让人看得见；pythonw 下 stdout 是空的，写失败无妨
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 环境探测
def pick_config() -> str:
    """和 start-web.bat 保持同一优先级：本机配置优先，通用模板兜底。"""
    for name in ("config.lanrun.yaml", "config.local.yaml"):
        if (ROOT / name).exists():
            return name
    return "config.yaml"


def python_exe() -> str:
    """优先用项目 .venv 里的解释器，它才有装好的依赖。"""
    for rel in ("Scripts/python.exe", "bin/python"):
        p = ROOT / ".venv" / rel
        if p.exists():
            return str(p)
    return sys.executable


def pid_alive(pid: int) -> bool:
    """这个 pid 还活着吗？

    注意 Windows 上**不能**用 `os.kill(pid, 0)` 来探测：Windows 没有信号，
    Python 的 os.kill 对非 CTRL_* 的 sig 会直接调 TerminateProcess——
    「查一下你在不在」会把人杀掉。这里改用 OpenProcess + GetExitCodeProcess。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def port_open(port: int, timeout: float = 2.0) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def health_ok(port: int, timeout: float = 5.0) -> bool:
    """比 TCP 更严一点：能连上还得能答 /api/health。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def listener_pids(port: int) -> set[int]:
    """谁在监听这个端口。

    为什么需要它：Windows 上 venv 的 `.venv/Scripts/python.exe` 只是个转发器，
    真正跑 http.server 的是它 fork 出来的子进程。所以 Popen 拿到的 pid（记进 web.pid
    的那个）**不是**占着端口的那个，--stop 只杀它会漏。
    """
    if sys.platform != "win32":
        return set()
    try:
        proc = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    # 中文 Windows 控制台的 netstat 输出是 GBK，按 UTF-8 解会直接抛 UnicodeDecodeError。
    # 这里只关心 ASCII 的地址/状态/PID 三列，遇到坏字节替换掉即可。
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        # Proto  Local Address  Foreign Address  State  PID
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}"):
            try:
                pid = int(parts[4])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
    return pids


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 拉起 Web 服务
def start_server(cfg_name: str, port: int) -> None:
    cmd = [python_exe(), "-u", str(ROOT / "run.py"), "-c", cfg_name,
           "--web", "--port", str(port), "--no-open"]
    log("发现 %d 端口没人服务，拉起：run.py -c %s --web --port %d" % (port, cfg_name, port))
    RUN.mkdir(parents=True, exist_ok=True)
    out = open(WEB_LOG, "a", encoding="utf-8", errors="replace")
    out.write(f"\n--- {now_str()} 由 service.py 拉起 ---\n")
    out.flush()
    kwargs: dict = {"cwd": str(ROOT), "stdout": out, "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        # 独立进程组：服务重启 / 被关掉时，它不会跟着被连坐
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    WEB_PID_FILE.write_text(str(proc.pid), encoding="utf-8")


def wait_ready(port: int, grace: int) -> bool:
    deadline = time.time() + grace
    while time.time() < deadline:
        if health_ok(port, timeout=3.0):
            return True
        time.sleep(1.0)
    return False


# ---------------------------------------------------------------- 子命令
def cmd_status(port: int, cfg_name: str) -> int:
    pid = read_pid(PID_FILE)
    alive = pid_alive(pid)
    print(f"常驻服务: {'运行中 (PID %d)' % pid if alive else '未运行'}")
    print(f"配置文件: {cfg_name}")

    obj = None
    try:
        obj = Config.load(cfg_name)
    except Exception as e:  # noqa: BLE001
        print(f"配置读取失败: {e}")

    if health_ok(port):
        print(f"Web 服务: 正常 http://127.0.0.1:{port}")
    elif port_open(port):
        print(f"Web 服务: 端口在监听但 /api/health 没响应 http://127.0.0.1:{port}")
    else:
        print(f"Web 服务: 未在运行（{port} 端口没人监听）")

    if obj is not None:
        snap = ollamaguard.snapshot(obj)
        state = "已连接" if snap["up"] else "无响应"
        extra = ""
        if not snap["up"]:
            extra = f"；自动拉起 {'开' if snap['auto_start'] else '关'}"
            if snap.get("last", {}).get("detail"):
                extra += f"；上次尝试：{snap['last']['detail']}"
        print(f"Ollama: {state} ({snap['host']}){extra}")
        if snap["up"]:
            try:
                import requests

                tags = requests.get(f"{snap['host']}/api/tags", timeout=5).json()
                print(f"本地模型: {len(tags.get('models') or [])} 个")
            except Exception:  # noqa: BLE001
                pass
    return 0 if health_ok(port) else 1


def cmd_stop(port: int) -> int:
    """先停保姆再停 Web 服务，否则保姆会把刚停掉的服务又拉起来。

    Web 服务按「记录 pid + 扫端口占用者」两条路一起收：Windows 上
    `.venv/Scripts/python.exe` 可能只是个转发器，Popen 拿到的 pid 并不是真正
    占着端口的那个（实测见过 web.pid=11500 而监听者是 19984）。
    """
    svc = read_pid(PID_FILE)
    if pid_alive(svc):
        log(f"收到 --stop：停止常驻服务 PID {svc}")
        try:
            os.kill(svc, signal.SIGTERM)
        except OSError as e:
            print(f"停止常驻服务失败：{e}")
    else:
        print("常驻服务本来就没在跑")

    time.sleep(1.2)   # 等保姆真的退掉，否则它下一秒又把服务拉回来

    web = read_pid(WEB_PID_FILE)
    if pid_alive(web):
        print(f"停止 Web 服务 PID {web}")
        try:
            os.kill(web, signal.SIGTERM)
        except OSError as e:
            print(f"停止 Web 服务失败：{e}")
    time.sleep(1.0)

    # 兜底：不管记录对不对，把真正占着这个端口的进程收掉
    for extra in sorted(listener_pids(port)):
        if pid_alive(extra):
            print(f"停止占用 {port} 端口的进程 PID {extra}")
            try:
                os.kill(extra, signal.SIGTERM)
            except OSError as e:
                print(f"停止失败：{e}")

    time.sleep(1.0)
    PID_FILE.unlink(missing_ok=True)
    WEB_PID_FILE.unlink(missing_ok=True)
    if port_open(port, timeout=3.0):
        print(f"注意：{port} 端口仍有进程在监听（可能不是这个服务拉起的）。")
        return 1
    print("已停止。下次要用：Windows 双击 start-service.bat，"
          "Linux / macOS 跑 ./start-service.sh。")
    return 0


# ---------------------------------------------------------------- 主循环
def loop(port: int, cfg_name: str, watch_ollama: bool) -> int:
    cfg = Config.load(cfg_name)
    log(f"常驻服务启动：托管 http://127.0.0.1:{port}，配置 {cfg_name}，"
        f"Ollama 守卫 {'开' if watch_ollama else '关'}")

    fails = 0
    tick = 0
    while True:
        healthy = health_ok(port)
        if healthy:
            if fails:
                log("Web 服务已恢复")
            fails = 0
        elif port_open(port):
            # 端口有人在听但答不出 /api/health：可能正在忙（跑任务时事件流会占着），
            # 也可能真卡住了。这里只记一条，绝不重启——杀进程会连带杀掉正在跑的任务。
            if tick % OLLAMA_EVERY == 0:
                log(f"注意：{port} 端口在监听但 /api/health 未响应，先不动它")
        else:
            start_server(cfg_name, port)
            if wait_ready(port, START_GRACE):
                log("Web 服务已就绪")
                fails = 0
            else:
                fails += 1
                log(f"拉起后 {START_GRACE}s 内仍不可用（连续第 {fails} 次），"
                    f"详见 {WEB_LOG.name}")

        if watch_ollama and tick % OLLAMA_EVERY == 0:
            res = ollamaguard.ensure(cfg, log_path=RUN / "ollama.log")
            if res["action"] not in ("already_up", "cooldown"):
                log(f"Ollama: {res['detail']}")

        tick += 1
        # 一切正常就规律体检；连续失败则退避，避免每 10 秒刷一次失败日志
        delay = CHECK_INTERVAL if fails == 0 else min(CHECK_INTERVAL * (2 ** fails), MAX_BACKOFF)
        time.sleep(delay)


def main() -> int:
    ap = argparse.ArgumentParser(prog="service.py", description="AgentForge 常驻服务")
    ap.add_argument("--port", type=int, default=8787, help="Web 界面端口，默认 8787")
    ap.add_argument("-c", "--config", help="配置文件路径，默认自动挑选")
    ap.add_argument("--status", action="store_true", help="打印当前状态后退出")
    ap.add_argument("--stop", action="store_true", help="停掉常驻服务与 Web 服务")
    ap.add_argument("--no-ollama", action="store_true", help="不管 Ollama 的自动拉起")
    args = ap.parse_args()

    cfg_name = args.config or pick_config()

    if args.status:
        return cmd_status(args.port, cfg_name)
    if args.stop:
        return cmd_stop(args.port)

    RUN.mkdir(parents=True, exist_ok=True)
    old = read_pid(PID_FILE)
    if pid_alive(old):
        print(f"常驻服务已经在跑（PID {old}），这次不做任何事。"
              f"要重启就 python service.py --stop 后再来。")
        return 0

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return loop(args.port, cfg_name, watch_ollama=not args.no_ollama)
    except KeyboardInterrupt:
        log("收到中断，常驻服务退出（Web 服务仍在跑，下次启动会被重新接管）")
        return 0
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
