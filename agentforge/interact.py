"""用户澄清弹窗（ask_user）桥接：交互模式等真实用户点选，评测模式走脚本答案。

设计口径（延续「调用即证据」）：
- 模型发起 ask_user(question, options) 是一次真实工具调用，convo 里有据可查；
- 问答本身不经过模型：交互模式下挂起等前端回传，评测模式下按任务书里的
  ask_answers 脚本逐条弹出——判定材料随任务走（SWE-bench 口径），模型看不到；
- 关键信息缺失时「先问再猜」是架构要求（系统提示工作方式第 9 条），不是模型自觉。

无人值守超时（rerun50 实证，用户裁定 2026-10-20）：后台跑评测时没有任何人在
浏览器前，但旧版无条件 `interact.begin(interactive=True)` 让 ask_user 挂死 600s
（实测阻塞 268s 后被手动停）。**「在不在」应由发起方声明，不是「谁调的 API」**：

- interactive=True + no_user=False：交互模式，等用户点选（默认超时 180s，
  超时降级成「自行假设并注明」——弹窗不该比任务本身还长）；
- interactive=True + no_user=True：后台 / 定时 / 派发脚本发起，**直接降级**，
  不弹窗、不阻塞，返回的提示里写明「本轮发起方声明无用户在场」；
- interactive=False + answers：评测脚本模式（ask_answers 按序弹）；
- interactive=False + 无 answers：CLI 直跑等无用户场景（同 no_user 降级）。

另外有 **ask 预算**：单轮任务最多 ask_user 的次数（默认 3）。用完之后再问
一律自动降级——防止模型「检索不努力 → 反复弹窗把活儿丢给用户」。
预算按线程计（run_task 一次运行一条线程），收尾 end() 清零。
"""
from __future__ import annotations

import re
import threading

from .runtime import runcontrol

_LOCK = threading.Lock()
_PENDING: dict[str, tuple[threading.Event, dict]] = {}   # qid -> (Event, box)
_LOCAL = threading.local()

DEFAULT_TIMEOUT = 180      # 交互模式等用户答复的上限（秒）：弹窗不该比任务还长
ASK_BUDGET_DEFAULT = 3     # 单轮任务最多 ask_user 几次（无人值守更严，见 begin）
_NO_USER_NOTE = ("[评测环境无用户作答] 请基于最常见的情况自行假设并继续，"
                 "并在最终回复里注明这条假设。")
_NO_USER_DECLARED = ("[本轮发起方声明无用户在场] 澄清弹窗已自动降级：请基于最常见的情况"
                     "自行假设并继续，并在最终回复里注明这条假设。这是后台/定时/派发"
                     "任务的标准行为——别再反复调 ask_user 问同一类问题。")
_BUDGET_NOTE = ("[澄清次数已用完] 本轮最多问 {n} 次，已全部用完。请自行假设最合理的情况"
                "继续，把不确定性写进最终回复——不要再调 ask_user。")
_TIMEOUT_NOTE = ("[等待用户答复超时（{t} 秒）] 请自行假设最合理的情况继续，"
                 "并在回复中注明假设。")


def begin(answers: list[str] | None = None, interactive: bool = False,
          no_user: bool = False, ask_budget: int | None = None) -> None:
    """设置当前线程的应答模式。

    answers=None 表示「非脚本模式」（交由 interactive 分支）；显式传空列表
    表示评测模式且无脚本答案（ask_user 将得到无用户作答提示）。

    no_user=True 表示发起方声明「无人值守」（后台脚本 / 定时 / 派发任务）：
    ask_user 不弹窗、不阻塞，直接降级。**这是超时降级的根治**——不再靠
    「等一个没人会点的弹窗超时」，而是根本不发弹窗。

    ask_budget 覆盖单轮 ask_user 预算（None = 默认 3）。
    """
    _LOCAL.scripted = list(answers) if answers is not None else None
    _LOCAL.interactive = bool(interactive)
    _LOCAL.no_user = bool(no_user)
    _LOCAL.ask_budget = ASK_BUDGET_DEFAULT if ask_budget is None else max(0, int(ask_budget))
    _LOCAL.ask_used = 0


def end() -> None:
    """清掉当前线程的模式（run 收尾必须调用，防止串到同线程的下一次运行）。"""
    _LOCAL.scripted = None
    _LOCAL.interactive = False
    _LOCAL.no_user = False
    _LOCAL.ask_budget = None
    _LOCAL.ask_used = 0


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


def set_auto_answer(fn) -> None:
    """设置本线程的澄清自动答复钩子（任务现场用，见 tasksession.py）。

    fn(question, options) -> str | None：返回答案文本则跳过真实弹窗直接作答；
    返回 None 则照常走原流程。编排器在员工运行前挂上、收尾摘掉，线程局部
    互不串扰。
    """
    _LOCAL.auto_answer = fn


def clear_auto_answer() -> None:
    _LOCAL.auto_answer = None


def _over_budget() -> bool:
    budget = getattr(_LOCAL, "ask_budget", None)
    if budget is None:
        return False            # 没 begin 过（未知线程）不拦——由 no_user 分支管
    return int(getattr(_LOCAL, "ask_used", 0)) >= int(budget)


def ask(question: str, options: list[str], timeout: int | None = None) -> str:
    """工具侧入口：返回用户的答复文本（或脚本答案/超时提示）。"""
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    # 任务现场的自动答复最先裁决：文件是本任务自己的产物/预期产物时，
    # 系统直接作答，不把用户当答案机（rerun13 实证的错位升级在这里堵死）。
    hook = getattr(_LOCAL, "auto_answer", None)
    if hook is not None:
        try:
            auto = hook(question, options)
        except Exception:                     # noqa: BLE001  钩子坏了不该卡死员工
            auto = None
        if auto:
            return str(auto)
    scripted = getattr(_LOCAL, "scripted", None)
    if scripted is not None:                      # 评测模式：按任务书脚本应答
        if scripted:
            return str(scripted.pop(0))
        return _NO_USER_NOTE
    # 无人值守（后台/定时/派发发起方声明）→ 直接降级，不弹窗、不阻塞。
    # 这是 rerun50 那种「评测跑一半弹窗挂死 4.5 分钟」的根治：不是「等它超时」，
    # 而是根本不发弹窗。
    if getattr(_LOCAL, "no_user", False):
        return _NO_USER_DECLARED
    if not getattr(_LOCAL, "interactive", False):  # CLI 直跑等无用户场景
        return _NO_USER_NOTE
    # ask 预算：用完了自动降级，防止「检索不努力 → 反复弹窗」
    if _over_budget():
        n = int(getattr(_LOCAL, "ask_budget", ASK_BUDGET_DEFAULT))
        return _BUDGET_NOTE.format(n=n)
    # 记账（含后续挂起的这一次）
    _LOCAL.ask_used = int(getattr(_LOCAL, "ask_used", 0)) + 1
    # 交互模式：挂起等前端回传（chat 线程阻塞在这里，SSE 事件照常推送）
    qid = uuid_hex()
    ev = threading.Event()
    box: dict = {"answer": ""}
    with _LOCK:
        _PENDING[qid] = (ev, box)
    try:
        ev.wait(timeout)
    finally:
        with _LOCK:
            _PENDING.pop(qid, None)
    return box["answer"] or _TIMEOUT_NOTE.format(t=int(timeout))


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


# runcontrol 只为「知道当前是不是评测/定时任务」留的查询口（未来可按任务来源
# 再收紧）；当前无人值守判定完全由发起方声明（begin(no_user=True)）。
def current_task() -> str:
    return runcontrol.current_task()


__all__ = [
    "begin", "end", "ask", "user_reply", "has_pending",
    "set_auto_answer", "clear_auto_answer",
    "DEFAULT_TIMEOUT", "ASK_BUDGET_DEFAULT",
]
