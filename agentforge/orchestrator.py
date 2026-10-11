"""多模型编排：拆解任务 → 分配模型 → 串行执行，全过程产出可观测事件。

和单模型 Agent.run 的区别：
    Agent.run     一个模型从头干到尾
    run_task      先规划，把不同性质的步骤交给池子里最擅长的模型，逐步接力

每一步都是一次完整的 Agent 循环（可调用工具、可写文件），上一步的产出留在
对话历史里，下一步直接在此基础上继续——所以接力不会丢上下文。

事件流（前端按需渲染）：
    combo     当前启用组合与各维度得分
    plan      拆解出的子任务清单
    assign    第 i/n 步分配给哪个模型、为什么
    subtask   非首步的步骤指令（前端显示为中间步骤）
    delta / thinking / tool_start / tool_result / stats / error / assistant
    complete  全部步骤结束
"""

from __future__ import annotations

from typing import Any, Iterator

from .planner import KIND_DIM, KIND_LABEL, Subtask, kind_of, make_plan, needs_plan, plan_summary

# 首步与后续步骤的提示词模板。都强调"只做这一步"，否则模型会越界把后面的活也干了。
STEP_FIRST = """【总体任务】
{message}

【本次只执行第 1/{total} 步】{title}
这一步的要求：{detail}

请只完成这一步，不要越界去做后面的步骤。做完后用一两句话说明本步结果。
"""

STEP_NEXT = """【本次执行第 {index}/{total} 步】{title}
这一步的要求：{detail}

前面步骤的产出已经在对话历史里，请在此基础上继续，不要重复已完成的操作。
做完后用一两句话说明本步结果。
"""


def _entries_of(pool) -> list[dict]:
    return [
        {"id": e.id, "key": e.key, "name": e.display, "scores": e.scores, "total": e.total}
        for e in pool
    ]


def _short(text: str, limit: int = 26) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _single_task(message: str, entries: list[dict]) -> Subtask:
    """不需要拆解时，按整体任务性质挑一个模型。"""
    kind = kind_of(message)
    dim = KIND_DIM[kind]
    best = max(entries, key=lambda e: (e["scores"].get(dim, 0), e["total"]))
    return Subtask(
        index=1,
        title=_short(message),
        detail=message,
        kind=kind,
        model_key=best["key"],
        model_name=best["name"],
        model_id=best["id"],
        score=int(best["scores"].get(dim, 0)),
        reason=f"{KIND_LABEL.get(kind, kind)}维度最强（{best['scores'].get(dim, 0)} 分）",
    )


def run_task(
    agent,
    store,
    history: list[dict],
    message: str,
    auto_plan: bool = True,
    model_key: str = "",
    stop_check=None,
    auto_approve: bool | None = None,
    file_log: list | None = None,
) -> Iterator[dict[str, Any]]:
    """执行一次用户请求。store 提供当前启用的模型池。

    model_key 非空且不是 "auto" 时表示用户手动指定了模型：跳过自动分配，
    所有步骤钉在该模型上执行。

    stop_check 返回 True 时在下一个步骤边界停下，已完成的步骤产出照常保留。

    auto_approve 本次任务的权限模式（True = 完全访问），file_log 用来收集
    本任务写过的文件，两者都透传给每一步的 Agent.run。
    """
    stopped = stop_check or (lambda: False)
    pinned_key = (model_key or "").strip()
    pinned: dict | None = None
    pool = store.enabled()
    if pinned_key and pinned_key != "auto":
        pinned = next(
            (e for e in ({ "id": x.id, "key": x.key, "name": x.display,
                           "scores": x.scores, "total": x.total } for x in pool)
             if e["id"] == pinned_key or e["key"] == pinned_key),
            None,
        )
        if pinned is None:
            yield {"type": "error", "message": f"指定的模型不可用：{pinned_key}"}
            return

    entries = _entries_of(pool if pinned else store.enabled())
    if not entries:
        yield {
            "type": "error",
            "message": "当前没有启用任何模型。请打开左下角设置 → 可用模型管理，勾选至少一个模型。",
        }
        return

    yield {
        "type": "combo",
        "combo": store.combo_info(pool),
        "models": [{"id": e["id"], "name": e["name"]} for e in entries],
    }

    plan: list[Subtask] = []
    if auto_plan and needs_plan(message, len(entries)):
        plan = [s for s in make_plan(message, entries) if (s.detail or "").strip()]

    multi = len(plan) >= 2
    if not multi:
        plan = [_single_task(message, entries)]

    if pinned:
        for st in plan:
            st.model_key = pinned["key"]
            st.model_name = pinned["name"]
            st.model_id = pinned["id"]
            st.reason = "手动指定模型"

    if multi:
        yield {
            "type": "plan",
            "subtasks": [s.as_dict() for s in plan],
            "summary": plan_summary(plan),
        }

    hist = list(history)
    total = len(plan)
    for i, st in enumerate(plan):
        if stopped():
            # 先回吐已完成部分的上下文，再报停止，界面与台账都不会丢已干的活；
            # 首步就停时 hist 还是空的，至少把用户的原始要求放进去
            yield {"type": "history",
                   "messages": hist or [{"role": "user", "content": message}]}
            yield {
                "type": "stopped",
                "steps": i,
                "total": total,
                "reason": "用户停止",
            }
            return
        # 模型可能刚从设置里被删掉，退回池内第一个可用模型
        if st.model_key not in agent.cfg.models:
            st.model_key = entries[0]["key"]
            st.model_name = entries[0]["name"]

        agent.set_model(st.model_key)
        yield {
            "type": "assign",
            "index": i + 1,
            "total": total,
            "model_key": st.model_key,
            "model_name": st.model_name,
            "kind": st.kind,
            "kind_label": KIND_LABEL.get(st.kind, st.kind),
            "reason": st.reason,
            "title": st.title,
            "score": st.score,
            "multi": multi,
        }

        if i == 0:
            prompt = STEP_FIRST.format(
                message=message, total=total, title=st.title, detail=st.detail
            ) if multi else message
        else:
            prompt = STEP_NEXT.format(
                index=i + 1, total=total, title=st.title, detail=st.detail
            )
            yield {"type": "subtask", "index": i + 1, "total": total, "text": st.detail}

        failed = False
        halted = False
        for ev in agent.run(hist, prompt, auto_route=False, stop_check=stopped,
                            auto_approve=auto_approve, file_log=file_log):
            t = ev.get("type")
            if t == "user":
                continue                      # 用户气泡由前端本地渲染
            if t == "assistant":
                hist = ev["history"]          # 接力上下文
            if t == "route":
                continue                      # 编排模式下路由由我们决定
            if t == "error":
                failed = True
            if t == "stopped":
                # 单步内部被停：把步数补齐后如实上报，不要伪装成正常收尾。
                # 带上 agent 内部已经累积的上下文，用户回看时能看到问的是什么。
                halted = True
                yield {"type": "history", "messages": ev.get("history") or hist}
                yield {"type": "stopped", "steps": i + 1, "total": total,
                       "reason": ev.get("reason") or "用户停止"}
                return
            yield ev
        if failed:
            return

    yield {"type": "complete", "steps": total, "multi": multi}


__all__ = ["run_task"]
