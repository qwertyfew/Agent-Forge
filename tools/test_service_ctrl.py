"""侧栏服务按钮的后端支撑（webui._is_local_deployment / _restart_locked /
_Handler._handle_service_restart）回归。

口径：云端判定以 start.bat / stop.bat 的在不在为准（镜像已排除 *.bat，
文件不在 = 云端）；重启**只写 .run/restart.flag**，停/启交给 service.py 看门狗
（父进程）——web 端自己拼命令链会变成「子进程杀父进程」，新实例来不及拉起；
60s 内连点必须被单飞锁拒绝。不真的执行重启——那会把跑测试的机器服务干掉。
删掉任何一条断言，对应行为坏了这里必须变红。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webui import (  # noqa: E402
    _Handler,
    _is_local_deployment,
    _restart_locked,
)
import webui as _webui  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "ok" if cond else "FAIL"
    print(f"  [{_n[0] + 1}] {mark}  {name}" + (f"  {detail}" if detail and not cond else ""))
    _n[0] += 1
    if not cond:
        _fails.append(name)


_n = [0]
_fails: list = []

with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    # L1 两个文件都在 → 本地
    (d / "start.bat").write_text("rem x", encoding="ascii")
    (d / "stop.bat").write_text("rem x", encoding="ascii")
    check("L1 start.bat + stop.bat 都在 → 判定本地", _is_local_deployment(d) is True, "")

    # L2 缺一个 → 云端（判定故意从严：半套脚本当云端，不给出半残的按钮）
    (d / "stop.bat").unlink()
    check("L2 只剩 start.bat → 判定云端", _is_local_deployment(d) is False, "")

    # L3 两个都没有 → 云端（云端镜像 .dockerignore 排除了 *.bat）
    check("L3 都不在 → 判定云端", _is_local_deployment(d) is False, "")

# L4 真实项目根目录：本机应有 start.bat/stop.bat，判定本地
check("L4 本项目根目录判定为本地（两个 bat 在位）",
      _is_local_deployment(ROOT) is True, "")

# L5-L7 重启链路（rerun4 根因修复后的契约）：web 端**只写 restart.flag**，
# 停/启交给 service.py 看门狗（父进程）。旧实现让 web 子进程自己拼
# `ping → service.py --stop → start pythonw` 的命令链，结果是「子进程杀父进程」，
# 新实例永远来不及拉起。所以这里钉三件事：写标记、绝不起子进程、连点被锁。
class _DummyHandler:
    """只实现 _json 的假处理器：够 _handle_service_restart 用，不碰真 socket。"""

    def __init__(self):
        self.reply = None
        self.code = 0

    def _json(self, obj, code: int = 200):
        self.reply, self.code = obj, code


def _as_local_run(root: Path):
    """把 webui 的 HERE 指到临时目录，并造出 start.bat/stop.bat（判本地的唯一口径）。"""
    (root / "start.bat").write_text("rem x", encoding="ascii")
    (root / "stop.bat").write_text("rem x", encoding="ascii")


_real_here = _webui.HERE
_real_popen = _webui.subprocess.Popen
spawns: list = []
try:
    with tempfile.TemporaryDirectory() as td3:
        local = Path(td3)
        _as_local_run(local)
        _webui.HERE = local
        # 起子进程 = 旧事故复发（自杀 or 弹窗）。这里只是记账，不阻断。
        _webui.subprocess.Popen = lambda *a, **kw: spawns.append((a, kw))

        h = _DummyHandler()
        _Handler._handle_service_restart(h)
        flag = local / ".run" / "restart.flag"
        check("L5a 重启只写 restart.flag 并即刻回成功（看门狗接管停/启）",
              h.code == 200 and bool(h.reply.get("ok")) and flag.is_file(),
              f"code={h.code} reply={h.reply}")
        check("L5b 标记内容带发令时刻（备查）",
              bool(flag.read_text(encoding="utf-8").strip()), "")
        check("L5c web 端绝不起子进程（子进程杀父进程是 rerun4 的根因）",
              spawns == [], str(spawns))

        before = flag.read_text(encoding="utf-8")
        h2 = _DummyHandler()
        _Handler._handle_service_restart(h2)
        check("L6a 60s 内连点 → 429 且不覆盖已有标记",
              h2.code == 429 and "error" in h2.reply
              and flag.read_text(encoding="utf-8") == before, f"{h2.reply}")
        check("L6b 连点也不许起子进程", spawns == [], str(spawns))

    # 云端（镜像已排除 *.bat，判定以 bat 在不在为准）→ 明确拒绝，不假装重启
    with tempfile.TemporaryDirectory() as td4:
        cloud = Path(td4)
        _webui.HERE = cloud
        h3 = _DummyHandler()
        _Handler._handle_service_restart(h3)
        check("L7 云端部署 → 400「本地服务重启不可用」且不写标记",
              h3.code == 400 and "error" in h3.reply
              and not (cloud / ".run" / "restart.flag").exists(), f"{h3.reply}")
finally:
    _webui.HERE = _real_here
    _webui.subprocess.Popen = _real_popen

# L8 重启单飞锁：60s 内重复请求被拒，过期放行，放行即续锁
with tempfile.TemporaryDirectory() as td2:
    run = Path(td2)
    check("L8a 无锁 → 放行", _restart_locked(run) is None, "")
    check("L8b 刚发起重启 → 拒绝（连点/多标签页不得叠加）",
          _restart_locked(run) is not None, "")
    # 把锁文件改成 2 小时前 → 过期放行
    lock = run / "restart.lock"
    old = time.time() - 7200
    os.utime(lock, (old, old))
    check("L8c 过期锁 → 放行", _restart_locked(run) is None, "")
    check("L8d 放行后锁被续期（mtime 回到现在）",
          abs(time.time() - lock.stat().st_mtime) < 5, "")

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
