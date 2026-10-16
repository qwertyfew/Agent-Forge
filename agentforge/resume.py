"""会话恢复（断点续跑）。

服务重启 / 用户停止 / 出错后，任务在台账里留有被裁剪过的对话历史
（TaskStore._trim_history：工具输出截到 4K、整任务上限 150K）。
恢复 = 把这份历史修复成协议合法的上下文，作为新任务的初始 history
灌回 /api/chat，让模型从中断处接着干，而不是从零重跑。

为什么需要「修复」而不能直接回灌：

1. 中断可能恰好落在「assistant 已发出 tool_calls、工具还没执行完」的
   缝隙里（停止检查点在每次工具执行前）。悬空的 tool_calls 没有对应的
   tool 应答，下一轮请求会被 Ollama / OpenAI 接口直接拒掉（协议要求
   assistant 的每个调用都要有应答）。修法：给每个悬空调用补一条「该调用
   未执行完成」的合成应答——如实告知结果未知，引导模型现场重查，而不是
   假装它成功过。
2. 台账是给人回看的，_trim_history 保留的是 role/content/tool_calls 结构，
   顺序不保证在异常路径下完好。开头出现没有来源的 tool 消息（孤儿）同样
   会让严格接口 400，直接丢弃。
3. 历史末尾若是 assistant 的悬空调用（后面什么都没有），与 1 同理补齐。
"""

from __future__ import annotations

INTERRUPTED_TOOL_NOTE = (
    "[任务中断] 上次执行在这个调用完成前被中断，结果未知。"
    "如仍需要这条信息，请重新调用；不确定就先用只读工具现场核实。"
)


def repair_history(history: list[dict]) -> list[dict]:
    """把台账里裁剪过的历史修复成协议合法的消息列表。原列表不被修改。"""
    msgs: list[dict] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant", "tool", "system"):
            continue
        msgs.append(dict(m))

    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") == "tool":
            # 孤儿 tool 消息：前面没有挂着 tool_calls 的 assistant，协议不收。
            i += 1
            continue
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = m["tool_calls"] or []
            # 收集紧随其后的 tool 应答（原样保留），缺的补合成应答
            j = i + 1
            while j < len(msgs) and msgs[j].get("role") == "tool":
                out.append(msgs[j])
                j += 1
            answered = j - (i + 1)
            missing = len(calls) - answered
            if missing > 0:
                # 中断缝隙：有调用没应答，补合成应答（assistant 消息本身保留——
                # 它可能还带着正文；调用也没删，模型可据此决定要不要重调）
                for _ in range(missing):
                    out.append({"role": "tool", "content": INTERRUPTED_TOOL_NOTE})
            i = j
            continue
        i += 1
    return out


def resume_brief(prompt: str) -> str:
    """恢复任务时发给模型的续跑指令。prompt 是原任务的完整指令。"""
    p = (prompt or "").strip()
    return (
        "【恢复被中断的任务】原始指令：" + (p[:800] or "（记录缺失）") + "\n\n"
        "上面的对话记录是这次任务上次执行到中断为止的过程（较早的工具输出可能已被截断，"
        "个别调用在中断前没有执行完、结果未知）。请先核对中断前已完成的部分——"
        "用 list_dir / read_file 现场核实产出是否真的落盘——已经完成的不要重做，"
        "从中断处继续把剩余部分做完，并照常收尾总结。"
    )
