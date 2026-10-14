"""悬停卡片延迟偏好（PrefsStore.tip_delay）回归。

口径：默认 1500ms、钳制区间 [300, 10000]、非法输入回落、落盘持久化、
快照带出；权限偏好与延迟互不干扰。删掉任何一条断言，对应行为坏了
这里必须变红。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.prefs import (DEFAULT_TIP_DELAY, PrefsStore)  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "ok" if cond else "FAIL"
    print(f"  [{_n[0] + 1}] {mark}  {name}" + (f"  {detail}" if detail and not cond else ""))
    _n[0] += 1
    if not cond:
        _fails.append(name)


_n = [0]
_fails: list = []

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / ".prefs.json"

    # P1 新库默认 1500ms
    s1 = PrefsStore(p)
    check("P1 默认延迟 1500ms", s1.tip_delay == DEFAULT_TIP_DELAY == 1500, str(s1.tip_delay))

    # P2 设置 + 持久化：换一个实例读同一份文件，值还在
    got = s1.set_tip_delay(2000)
    check("P2 设置生效", got == 2000 and s1.tip_delay == 2000, str(got))
    s2 = PrefsStore(p)
    check("P3 落盘持久化（新实例读到同值）", s2.tip_delay == 2000, str(s2.tip_delay))

    # P4 钳制：越界拉回 [300, 10000]，非数字回落默认
    check("P4a 下界钳制 0→300", s2.set_tip_delay(0) == 300, "")
    check("P4b 负数→300", s2.set_tip_delay(-5) == 300, "")
    check("P4c 上界钳制 99999→10000", s2.set_tip_delay(99999) == 10000, "")
    check("P4d 非法字符串→1500", s2.set_tip_delay("abc") == 1500, "")
    check("P4e None→1500", s2.set_tip_delay(None) == 1500, "")
    check("P4f 浮点取整 1200.7→1200", s2.set_tip_delay(1200.7) == 1200, "")
    check("P4g 边界内合法值原样通过", s2.set_tip_delay(500) == 500, "")

    # P5 快照带出延迟与权限（前端 /api/state 一次拿全）
    snap = s2.snapshot()
    check("P5 快照含 tip_delay 与 permission",
          snap.get("tip_delay") == 500 and snap.get("permission") in ("full", "default"),
          json.dumps(snap, ensure_ascii=False))

    # P6 两类偏好互不干扰：写延迟不碰权限，写权限不碰延迟
    s2.set_permission("default")
    check("P6a 权限写入不影响延迟", s2.snapshot()["tip_delay"] == 500
          and s2.snapshot()["permission"] == "default", "")
    s2.set_tip_delay(800)
    check("P6b 延迟写入不影响权限", s2.snapshot()["permission"] == "default"
          and s2.snapshot()["tip_delay"] == 800, "")

    # P7 文件损坏 → 双双回落默认，不崩
    p.write_text("not json", encoding="utf-8")
    s3 = PrefsStore(p)
    check("P7 损坏文件回落默认", s3.tip_delay == 1500
          and s3.snapshot()["permission"] == "full", "")

    # P8 半截数据（只有 permission）：延迟回落默认，permission 保留
    p.write_text(json.dumps({"permission": "default"}), encoding="utf-8")
    s4 = PrefsStore(p)
    check("P8 半截数据各回各位", s4.tip_delay == 1500
          and s4.snapshot()["permission"] == "default", "")

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
