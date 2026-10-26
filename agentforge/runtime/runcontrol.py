"""任务停止开关（协作式取消）。

为什么要单开一个模块：Agent 主循环是个生成器，长跑在 HTTP 请求里，
没法从外面直接掐断。所以用「标志位 + 主动检查」的方式：

    运行侧  Agent.run / run_task 每轮推理前、每次调工具前问一次 stop_check()
    控制侧  Web 界面点「停止任务」→ request_stop(task_id) 把标志立起来
    清理侧  SSE 结束时 release(task_id)，避免标志越攒越多

停止是**协作式**的：当前正在生成的那一次模型回复会先跑完，框架在下一个
检查点退出，然后把已有产出照常返回。这样不会把半截工具调用留在历史里。

实现已下沉到 `agentforge.runstate.RunState`（运行状态的唯一事实源），本模块
保留旧调用方在用的函数名，语义一字不改：调用方散在 webui / loop / orchestrator
/ steering / interact 与两个工具模块里，一次性改签名风险太大；先收敛状态，
再逐步把调用改为直连 run_state。
"""

from __future__ import annotations

import threading

from .runstate import RunState, run_state

__all__ = [
    "register", "request_stop", "is_stopping", "release", "running_ids",
    "set_current_task", "current_task", "clear_current_task", "state",
]


def state() -> RunState:
    """底层状态对象：要读状态机/快照就用它。"""
    return run_state


def register(task_id: str) -> threading.Event:
    """给一次任务注册停止标志（同名任务重复注册则复用，立过的作废换新）。"""
    return run_state.register(task_id)


def request_stop(task_id: str) -> bool:
    """请求停止。返回 True 表示确实有这么个在跑的任务。"""
    return run_state.request_stop(task_id)


def is_stopping(task_id: str) -> bool:
    return run_state.is_stopping(task_id)


def release(task_id: str) -> None:
    run_state.release(task_id)


def running_ids() -> list[str]:
    return run_state.running_ids()


def set_current_task(task_id: str) -> None:
    run_state.set_current_task(task_id)


def current_task() -> str:
    return run_state.current_task()


def clear_current_task() -> None:
    run_state.clear_current_task()
