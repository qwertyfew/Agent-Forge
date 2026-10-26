"""运行状态的唯一事实源。

为什么要单独抽这一层：
从前「谁在跑、谁要停、当前是哪个任务」散落在四五个地方——停止标志是一个
字典、当前任务是 thread-local、任务状态又存在台账（TaskStore）里、界面还自己
维护一份 liveTask。同一件事有四种说法，于是出现「台账里已经是 stopped，界面
角标还一直亮着」这类纯粹的同步 bug（2026-10-27 实测：停止后「执行中」呼吸灯
不灭，原因只是负责重算角标的函数没在轮询里被调用）。

这里把**运行期的**状态收敛成一处：状态机 + 停止标志 + 当前任务，全部加锁，
对外只暴露派生查询。台账（TaskStore）负责持久化与历史展示，它应该在任务结束
时以本模块的状态为准落盘，两边不再各自判断「是不是还在跑」。

生命周期：
    queued  → 登记了但没抢到算力位
    running → 正在跑
    stopping→ 已请求停止、运行侧还没退出
    stopped / done / error → 终态（都不算「在跑」）

停止是协作式的：这里只立标志，真正退出由运行侧在流式循环/步骤边界检查
（见 loop.py 的 stop_check）。所以有 stopping 这个中间态——它让「点了停止但
还没退出」这件事有明确表达，而不是让界面去猜。
"""
from __future__ import annotations

import threading
from typing import Iterable

QUEUED = "queued"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
DONE = "done"
ERROR = "error"

# 「还在跑」的三个状态：任何派生查询（running_ids / is_active）都以它为准
ACTIVE = (QUEUED, RUNNING, STOPPING)
TERMINAL = (STOPPED, DONE, ERROR)

__all__ = ["RunState", "run_state", "QUEUED", "RUNNING", "STOPPING",
           "STOPPED", "DONE", "ERROR", "ACTIVE", "TERMINAL"]


class RunState:
    """一次进程内所有任务的运行状态。单例见本模块末尾的 run_state。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, str] = {}
        self._stop: dict[str, threading.Event] = {}
        self._local = threading.local()

    # ---------- 生命周期 ----------
    def register(self, task_id: str, status: str = RUNNING) -> "threading.Event":
        """登记一次任务执行，返回它的停止标志。

        同名任务重复登记时复用标志——除非那个标志已经立起来（上一次停止请求
        还没被 release 掉），这时必须换新的：否则重试的同名任务第一轮就误判
        「要停止」直接退出。
        """
        tid = str(task_id or "")
        with self._lock:
            ev = self._stop.get(tid)
            if ev is not None and ev.is_set():
                ev = None
            if ev is None:
                ev = threading.Event()
                self._stop[tid] = ev
            self._states[tid] = status
            return ev

    def set_status(self, task_id: str, status: str) -> None:
        tid = str(task_id or "")
        if not tid:
            return
        with self._lock:
            self._states[tid] = status

    def get_status(self, task_id: str) -> str:
        tid = str(task_id or "")
        with self._lock:
            return self._states.get(tid, "")

    def is_active(self, task_id: str) -> bool:
        return self.get_status(task_id) in ACTIVE

    def release(self, task_id: str, status: str = "") -> None:
        """任务收尾：摘掉停止标志；给了终态就记下来，否则连状态一起清掉。"""
        tid = str(task_id or "")
        with self._lock:
            self._stop.pop(tid, None)
            if status:
                self._states[tid] = status
            else:
                self._states.pop(tid, None)

    # ---------- 停止（协作式） ----------
    def request_stop(self, task_id: str) -> bool:
        """请求停止。返回 True 表示确实有这么个在跑的任务。"""
        tid = str(task_id or "")
        with self._lock:
            ev = self._stop.get(tid)
        if ev is None:
            return False
        ev.set()
        with self._lock:
            # 只把「正在跑」推进到 stopping：终态不该被改回 stopping
            if self._states.get(tid) in (QUEUED, RUNNING):
                self._states[tid] = STOPPING
        return True

    def is_stopping(self, task_id: str) -> bool:
        tid = str(task_id or "")
        with self._lock:
            ev = self._stop.get(tid)
        return bool(ev and ev.is_set())

    def stop_event(self, task_id: str):
        with self._lock:
            return self._stop.get(str(task_id or ""))

    # ---------- 派生查询 ----------
    def running_ids(self) -> list[str]:
        """还没被请求停止、仍占着算力位的任务。

        与旧 runcontrol 的口径保持一致（「标志没立起来」= 还在跑）：已经进
        stopping 的不再列入，这样「停止全部」不会重复去停一个正在退出的任务。
        想知道「包括正在退出的在内还有哪些没结束」，用 is_active。
        """
        with self._lock:
            return [k for k, v in self._states.items() if v in (QUEUED, RUNNING)]

    def active_ids(self) -> Iterable[str]:
        """兼容旧语义：与 running_ids 同义（旧名字容易误导，优先用 running_ids）。"""
        return self.running_ids()

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._states)

    # ---------- 当前任务（按线程） ----------
    def set_current_task(self, task_id: str) -> None:
        self._local.task_id = str(task_id or "")

    def current_task(self) -> str:
        return getattr(self._local, "task_id", "") or ""

    def clear_current_task(self) -> None:
        self._local.task_id = ""


run_state = RunState()
"""进程级单例。runcontrol 是它的门面，新代码优先直接用本对象。"""
