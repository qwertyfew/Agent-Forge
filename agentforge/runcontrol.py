"""任务停止开关（协作式取消）。

为什么要单开一个模块：Agent 主循环是个生成器，长跑在 HTTP 请求里，
没法从外面直接掐断。所以用「标志位 + 主动检查」的方式：

    运行侧  Agent.run / run_task 每轮推理前、每次调工具前问一次 stop_check()
    控制侧  Web 界面点「停止任务」→ request_stop(task_id) 把标志立起来
    清理侧  SSE 结束时 release(task_id)，避免标志越攒越多

停止是**协作式**的：当前正在生成的那一次模型回复会先跑完，框架在下一个
检查点退出，然后把已有产出照常返回。这样不会把半截工具调用留在历史里。
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_flags: dict[str, threading.Event] = {}

# 「当前正在跑的是哪个任务」。工具的入参里没有任务 id，但写入记忆时要知道
# 这条记忆是哪次任务留下的（清空时才能按任务精确删除）。Web 端每个请求
# 独占一个线程，所以用线程局部变量传递是安全的。
_local = threading.local()


def set_current_task(task_id: str) -> None:
    _local.task_id = str(task_id or "")


def current_task() -> str:
    return getattr(_local, "task_id", "") or ""


def clear_current_task() -> None:
    _local.task_id = ""


def register(task_id: str) -> threading.Event:
    """给一次任务注册停止标志（同名任务重复注册则复用）。"""
    tid = str(task_id or "")
    with _lock:
        ev = _flags.get(tid)
        if ev is None:
            ev = threading.Event()
            _flags[tid] = ev
        return ev


def request_stop(task_id: str) -> bool:
    """请求停止。返回 True 表示确实有这么个在跑的任务。"""
    tid = str(task_id or "")
    with _lock:
        ev = _flags.get(tid)
    if ev is None:
        return False
    ev.set()
    return True


def is_stopping(task_id: str) -> bool:
    tid = str(task_id or "")
    with _lock:
        ev = _flags.get(tid)
    return bool(ev and ev.is_set())


def release(task_id: str) -> None:
    tid = str(task_id or "")
    with _lock:
        _flags.pop(tid, None)


def running_ids() -> list[str]:
    with _lock:
        return [k for k, v in _flags.items() if not v.is_set()]


__all__ = [
    "register", "request_stop", "is_stopping", "release", "running_ids",
    "set_current_task", "current_task", "clear_current_task",
]
