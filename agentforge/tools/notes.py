"""任务笔记（task_notes）：跨「上下文压缩」持久的任务内便签。

为什么需要它：keep_recent_tool_results 只保留最近几条工具结果的完整内容，
更早的都被压成一行摘要。于是一个 40+ 轮的探索型任务里，模型第 10 轮查到的
关键信息到第 30 轮已经「失忆」，只能用同样的参数再查一遍——这就是反复
迭代的根源。人类工程师做这种事靠的是草稿纸：查到关键点随手记下来。

任务笔记就是那张草稿纸：
- 写入：append 一行结论（查到的定义位置、关键参数、下一步打算）；
- 读取：read 看全部笔记；
- **每轮注入**：loop 在每轮请求前把笔记尾部（≤2000 字）注入系统提示，
  压缩碰不到它——笔记里的发现永远「在场」。

它和长期记忆（remember）的区别：任务笔记只属于当前任务，随任务结束失效；
remember 写的是跨任务长期结论。别把过程性的中间发现塞进 remember。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..runcontrol import current_task
from .base import SAFE, make_tool


def notes_path(cfg) -> Path:
    """当前任务的笔记文件。没有任务上下文时落到 adhoc（CLI 直连等场景）。"""
    tid = str(current_task() or "adhoc")
    safe = re.sub(r"[^\w.-]", "_", tid)[:64] or "adhoc"
    return cfg.state() / "task-notes" / f"{safe}.md"


def build_notes_tools(guard, cfg):
    def task_notes(action: str = "append", text: str = "") -> str:
        p = notes_path(cfg)
        act = (action or "append").strip().lower()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"[错误] 笔记目录创建失败：{e}"
        if act == "read":
            if not p.is_file():
                return "（还没有任务笔记。查到关键信息时用 append 记一条，它们不受上下文压缩影响）"
            body = p.read_text(encoding="utf-8", errors="replace").strip()
            if len(body) > 4000:
                body = "…（更早的笔记已截断）\n" + body[-4000:]
            return body or "（笔记为空）"
        text = (text or "").strip()
        if not text:
            return "[错误] append 模式需要 text：一句话写清「发现了什么/结论是什么」。"
        text = text.replace("\r", "")
        with p.open("a", encoding="utf-8") as f:
            for line in text.splitlines() or [""]:
                f.write(f"- {line}\n")
        n = 0
        try:
            with p.open(encoding="utf-8") as f:
                n = sum(1 for _ in f)
        except OSError:
            pass
        return (f"已记入任务笔记（现共 {n} 条）。"
                "笔记每轮都会注入你的上下文，旧工具结果被压缩后笔记仍在——"
                "查到关键定义位置、参数、结论时请随手 append。")

    return [
        make_tool(
            "task_notes",
            "任务便签：把「本轮查到的关键发现」记下来，防止稍后被上下文压缩清掉。\n"
            "适合记：定位到的文件/行号、关键参数与结论、已排除的方案、下一步打算。\n"
            "不适合记：长期偏好（用 remember）、大段原文（说行号就行）。",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["append", "read"],
                        "description": "append=记一条（默认）；read=读全部笔记",
                    },
                    "text": {
                        "type": "string",
                        "description": "append 的一句话内容，如「入口在 web/index.html 的 openModal()，勾选联动逻辑在 onchange」",
                    },
                },
                "required": ["action"],
            },
            task_notes,
            SAFE,
        )
    ]
