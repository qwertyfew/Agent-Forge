"""长期记忆工具：让任务能把用户偏好写进工作区记忆，供后续任务复用。

只有一条工具 `remember`。它写进 `<工作区>/.agentforge/memory.md`，
下次任务开始时会自动出现在系统提示里。

记忆**只放用户偏好**（稳定的倾向 / 风格 / 习惯）：环境事实工具能现查、
项目约定由架构保证，两类都不该往这里写。详见 `memory.py` 的模块说明。
"""

from __future__ import annotations

from pathlib import Path

from ..autonote import dupe_of
from ..memory import MemoryStore
from ..runcontrol import current_task
from .base import WRITE, make_tool


def memory_path(cfg) -> Path:
    return cfg.state() / "memory.md"


def build_memory_tools(guard, cfg):
    store = MemoryStore(memory_path(cfg))

    def remember(note: str) -> str:
        text = str(note or "").strip()
        if not text:
            return "[错误] note 为空，没有写入任何内容。"
        # 判重：同一件事不要写第二遍（小模型常连着调好几次 remember）
        dup = dupe_of(store, text)
        if dup:
            return f"这条已经记过了，没有重复写入：{dup[:120]}"
        entry = store.append(text, current_task())
        if not entry:
            return "[错误] 记忆写入失败。"
        return (
            f"已写入长期记忆：{entry['text'][:120]}\n"
            f"（文件 {store.path}，后续任务会自动读到；只写用户偏好，不要写过程）"
        )

    return [
        make_tool(
            "remember",
            "把「用户的长期偏好」写入工作区长期记忆——稳定的倾向、风格、习惯，"
            "如「回复先给结论」「产出文档一律简体中文」。后续任何任务开始前都会读到它，"
            "所以只适合记偏好；环境事实（路径 / 端口 / 版本）工具能现查、"
            "项目约定（工作方式 / 流程规则）由架构保证，两类都**不要**往这里写。"
            "不要记录本次任务的中间过程。",
            {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "一条用户偏好，建议以「用户偏好：」开头",
                    },
                },
                "required": ["note"],
            },
            remember,
            WRITE,
        )
    ]
