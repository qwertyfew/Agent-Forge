"""ask_user 澄清弹窗工具：关键信息检索不到时，向用户发起简明追问。

口径（用户拍板，2026-10-15）：检索不到用户提到的项目/文件/概念时，如实说没有
是对的，但更好的行为是**先问再猜**——弹一个足够简单易懂的选择弹窗（2-4 个
简明选项 + 可自由补充），用户轻松一点就能把信息补上；拿到答复再继续任务，
而不是编造或停在一堆建议上。问答本身不经过模型（见 interact 模块）。
"""
from __future__ import annotations

import re

from .. import interact
from .base import make_tool


def build_ask_tools():
    def ask_user(question: str, options: list[str] | None = None) -> str:
        q = re.sub(r"\s+", " ", str(question or "")).strip()[:200]
        if not q:
            return "[错误] question 不能为空：一句话说清要澄清什么。"
        opts = [re.sub(r"\s+", " ", str(o or "")).strip()[:30]
                for o in (options or []) if str(o or "").strip()]
        if not opts:
            opts = ["是", "否"]
        return interact.ask(q, opts[:6])

    return [
        make_tool(
            "ask_user",
            "向用户发起澄清弹窗。任务所需的关键信息在检索后仍然缺失（如用户提到的"
            "项目/文件/概念在本地与已查来源里都找不到）时调用：question 用一句话点明"
            "要澄清什么；options 给 2-4 个简明选项（每个不超过 12 字，覆盖最可能的"
            "情况，用户也可以自行输入补充）。用户的答复会作为本工具的返回值，"
            "拿到后按答复继续任务。不要在信息充足时调用；不要用本工具代替检索。",
            {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "一句话澄清问题，如：您说的REA是指什么？",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 个简明选项，如：本地项目、git上的项目、其他",
                    },
                },
                "required": ["question", "options"],
            },
            ask_user,
        ),
    ]
