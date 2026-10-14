"""侧栏服务按钮的后端支撑（webui._is_local_deployment / _restart_command /
_restart_locked）回归。

口径：云端判定以 start.bat / stop.bat 的在不在为准（镜像已排除 *.bat，
文件不在 = 云端）；重启命令必须先给 HTTP 响应留时间、先 stop 后 start、
全程不弹可见窗口、不依赖控制台、不借道 bat 转发层。不真的执行重启——
那会把跑测试的机器服务干掉。
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
    _is_local_deployment,
    _restart_command,
    _restart_locked,
)


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

# L5 重启命令的结构：响应先行、先停后启、中间留端口释放时间
# （新版直接点名解释器调 service.py，不再 call stop.bat / start.bat）
cmd = _restart_command()
check("L5a 先 ping 延时再动手（HTTP 响应先发出去）",
      cmd.index("ping") < cmd.index("service.py --stop"), "")
check("L5b 先 --stop 后 start", cmd.index("service.py --stop") < cmd.rindex("start "), "")
check("L5c 停与启之间留端口释放时间（第二个 ping）",
      cmd.index("service.py --stop") < cmd.rindex("ping") < cmd.rindex("start "), "")

# L6 上一版事故的三个根因，一个都不许回来
check("L6a 不用 timeout /nobreak（无控制台时立即失效）", "timeout" not in cmd, cmd)
check("L6b 不再借道任何 .bat（pause 误判 / 弹窗都出自转发层）", ".bat" not in cmd, cmd)
check("L6c 解释器路径必须带引号（防路径空格断裂）",
      cmd.count('"') >= 4 and '"' in cmd.split("service.py --stop")[0], cmd)
check("L6d start 用空标题占位（路径不被当窗口名）", 'start "" ' in cmd, cmd)
check("L6e 无参调用可用（默认解析 .venv / 当前解释器）", isinstance(cmd, str) and len(cmd) > 40, "")

# L7 显式传入解释器路径要被采纳（便于测试与未来换解释器）
c2 = _restart_command(py="C:/x/py.exe", pyw="C:/x/pyw.exe")
check("L7a 自定义 python 被采纳", '"C:/x/py.exe" service.py --stop' in c2, c2)
check("L7b 自定义 pythonw 被采纳", 'start "" "C:/x/pyw.exe" service.py' in c2, c2)

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
