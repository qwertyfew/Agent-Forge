"""中途补充（Steering）收件箱 —— DeepSeek Harness Inbox 三通道之一。

参照口径（docs/harness-redesign-20261019.md）：任务执行中用户发现方向不对，
补一条提示，**当前 Turn 的下一个 Step 立刻采纳**，而不是只能停止重来。

AgentForge 的映射：
- submit  ：用户在任务运行中回车 → 进收件箱（任务没在跑则返回 False，
            调用方回落为普通新消息）；
- take    ：运行侧在检查点取走。两个消费点——
            ① 员工循环每轮推理前（loop.py 的 steer_check，consumer=步骤 id，
               下一轮模型调用立即采纳）；
            ② leader 每批规划前（consumer="leader"，下一批必须采纳）；
- 每条消息带 seen 集合：并行批次里每个员工各看到一次，互不抢占；
- clear   ：任务收尾清箱（webui 与 runcontrol.release 同点调用）。

与停止开关（runcontrol.py）同一套协作式模型：标志进箱、运行侧主动检查，
不从外面掐。
"""
from __future__ import annotations

import threading
import time

from . import runcontrol

_lock = threading.Lock()
_boxes: dict[str, list[dict]] = {}     # task_id -> [{"id","text","ts","seen":set}]


def submit(task_id: str, text: str) -> bool:
    """投递一条中途补充。返回 True 表示已入箱（任务确在运行）。"""
    tid = str(task_id or "")
    text = str(text or "").strip()
    if not tid or not text:
        return False
    if tid not in runcontrol.running_ids():
        return False                   # 没有在跑的任务：调用方回落为普通消息
    with _lock:
        _boxes.setdefault(tid, []).append({
            "id": f"st{int(time.time() * 1000) % 10 ** 9}",
            "text": text[:2000],
            "ts": time.strftime("%H:%M:%S"),
            "seen": set(),
        })
    return True


def take(task_id: str, consumer: str) -> list[str]:
    """取走该消费者还没见过的补充文本（标记已投递，其它消费者不受影响）。"""
    out: list[str] = []
    with _lock:
        for m in _boxes.get(str(task_id or ""), []):
            if consumer not in m["seen"]:
                m["seen"].add(consumer)
                out.append(m["text"])
    return out


def pending(task_id: str) -> list[str]:
    """还没投递完的补充（前端/调试用，只读不清除）。"""
    with _lock:
        return [m["text"] for m in _boxes.get(str(task_id or ""), [])
                if len(m["seen"]) == 0]


def clear(task_id: str) -> None:
    """任务收尾清箱，防止残留串进同名重试任务。"""
    with _lock:
        _boxes.pop(str(task_id or ""), None)


__all__ = ["submit", "take", "pending", "clear"]
