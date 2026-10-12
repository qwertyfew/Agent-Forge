"""长期记忆工具：让任务能把结论写进工作区记忆，供后续任务复用。

只有一条工具 `remember`。它写进 `<工作区>/.agentforge/memory.md`，
下次任务开始时会自动出现在系统提示里。
"""

from __future__ import annotations

from pathlib import Path

from ..autonote import dupe_of
from ..memory import LAYER_LABEL, MemoryStore
from ..runcontrol import current_task
from .base import WRITE, make_tool


def memory_path(cfg) -> Path:
    return cfg.state() / "memory.md"


def build_memory_tools(guard, cfg):
    store = MemoryStore(memory_path(cfg))

    def remember(note: str, layer: str = "") -> str:
        text = str(note or "").strip()
        if not text:
            return "[错误] note 为空，没有写入任何内容。"
        # 判重：同一件事不要写第二遍（小模型常连着调好几次 remember）
        dup = dupe_of(store, text)
        if dup:
            return f"这条已经记过了，没有重复写入：{dup[:120]}"
        # layer 留空时由 MemoryStore 自动判定（偏好→L3 / 事实→L1 / 其余→L2）
        entry = store.append(text, current_task(), layer=layer)
        if not entry:
            return "[错误] 记忆写入失败。"
        tag = f"{entry['layer']} {LAYER_LABEL.get(entry['layer'], '')}".strip()
        return (
            f"已写入长期记忆（{tag}）：{entry['text'][:120]}\n"
            f"（文件 {store.path}，后续任务会自动读到；只写结论与偏好，不要写过程）"
        )

    return [
        make_tool(
            "remember",
            "把「以后还用得上的结论、偏好或约定」写入工作区长期记忆。"
            "后续任何任务开始前都会读到它，所以适合记录用户偏好、项目约定、"
            "踩过的坑；不适合记录本次任务的中间过程。",
            {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "一句话结论，建议以「用户偏好：」「项目约定：」「注意：」等前缀开头",
                    },
                    "layer": {
                        "type": "string",
                        "enum": ["L1", "L2", "L3"],
                        "description": "记忆的语义层：L1 事实与环境（路径/端口/版本等可核对信息）、"
                                       "L2 项目约定（工作方式、流程约定）、L3 用户偏好（稳定的个人偏好与习惯）。"
                                       "留空则自动判定。",
                    },
                },
                "required": ["note"],
            },
            remember,
            WRITE,
        )
    ]
