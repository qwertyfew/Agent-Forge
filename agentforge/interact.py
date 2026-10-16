"""用户澄清弹窗（ask_user）桥接：交互模式等真实用户点选，评测模式走脚本答案。

设计口径（延续「调用即证据」）：
- 模型发起 ask_user(question, options) 是一次真实工具调用，convo 里有据可查；
- 问答本身不经过模型：交互模式下挂起等前端回传，评测模式下按任务书里的
  ask_answers 脚本逐条弹出——判定材料随任务走（SWE-bench 口径），模型看不到；
- 关键信息缺失时「先问再猜」是架构要求（系统提示工作方式第 9 条），不是模型自觉。

线程模型：ask_user 在消费 run_task 事件流的线程里同步执行——webui 的 chat
线程、评测台 _run 线程、bench CLI 线程各是独立线程，用 threading.local
携带本线程的模式（scripted / interactive），互不串扰。
"""
from __future__ import annotations

import threading
import uuid

_LOCK = threading.Lock()
_PENDING: dict[str, tuple[threading.Event, dict]] = {}   # qid -> (Event, box)
_LOCAL = threading.local()

DEFAULT_TIMEOUT = 600      # 交互模式等用户答复的上限（秒）
_NO_USER_NOTE = ("[评测环境无用户作答] 请基于最常见的情况自行假设并继续，"
                 "并在最终回复里注明这条假设。")


def begin(answers: list[str] | None = None, interactive: bool = False) -> None:
    """设置当前线程的应答模式：评测传脚本答案列表；webui 传 interactive=True。

    answers=None 表示「非脚本模式」（交由 interactive 分支）；显式传空列表
    表示评测模式且无脚本答案（ask_user 将得到无用户作答提示）。
    """
    _LOCAL.scripted = list(answers) if answers is not None else None
    _LOCAL.interactive = bool(interactive)


def end() -> None:
    """清掉当前线程的模式（run 收尾必须调用，防止串到同线程的下一次运行）。"""
    _LOCAL.scripted = None
    _LOCAL.interactive = False


def user_reply(text: str) -> int:
    """前端把用户的点选/补充送回来，唤醒所有挂起中的提问。返回唤醒数。"""
    with _LOCK:
        items = list(_PENDING.values())
    for ev, box in items:
        box["answer"] = str(text or "")
        ev.set()
    return len(items)


def has_pending() -> bool:
    with _LOCK:
        return bool(_PENDING)


def ask(question: str, options: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """工具侧入口：返回用户的答复文本（或脚本答案/超时提示）。"""
    scripted = getattr(_LOCAL, "scripted", None)
    if scripted is not None:                      # 评测模式：按任务书脚本应答
        if scripted:
            return str(scripted.pop(0))
        return _NO_USER_NOTE
    if not getattr(_LOCAL, "interactive", False):  # CLI 直跑等无用户场景
        return _NO_USER_NOTE
    # 交互模式：挂起等前端回传（chat 线程阻塞在这里，SSE 事件照常推送）
    qid = uuid.uuid4().hex[:12]
    ev = threading.Event()
    box: dict = {"answer": ""}
    with _LOCK:
        _PENDING[qid] = (ev, box)
    try:
        ev.wait(timeout)
    finally:
        with _LOCK:
            _PENDING.pop(qid, None)
    return box["answer"] or "[等待用户答复超时] 请自行假设最合理的情况继续，并在回复中注明假设。"
