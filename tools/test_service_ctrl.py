"""侧栏服务按钮的后端支撑（webui._is_local_deployment / _restart_command）回归。

口径：云端判定以 start.bat / stop.bat 的在不在为准（镜像已排除 *.bat，
文件不在 = 云端）；重启命令必须先给 HTTP 响应留时间、先 stop 后 start。
不真的执行重启——那会把跑测试的机器服务干掉。
删掉任何一条断言，对应行为坏了这里必须变红。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webui import _is_local_deployment, _restart_command  # noqa: E402


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

# L5 重启命令的结构：响应先行、先停后启、两个脚本都点名
cmd = _restart_command()
check("L5a 先等 2s 再动手（HTTP 响应先发出去）", cmd.index("timeout") < cmd.index("stop.bat"), "")
check("L5b 先 stop 后 start", cmd.index("stop.bat") < cmd.index("start.bat"), "")
check("L5c 停与启之间留端口释放时间",
      cmd.index("stop.bat") < cmd.rfind("timeout") < cmd.index("call start.bat"), "")

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
