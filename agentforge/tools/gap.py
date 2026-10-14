"""缺口登记工具：模型发现「完成任务必需但环境没有」的工具/技能时，由此登记。

设计口径（延续第 23 步：缺什么由架构说了算，但"发现"允许模型发起）：
模型无法凭空调用一个不存在的工具，而它如实拒绝时架构也无从得知缺的是什么——
report_gap 就是补这个通道的：调用本身是一次真实的工具调用（convo 里有据可查），
参数（能力名 + 功能/规范文案）由架构原样入账到缺口台账，**不做任何模型判断**；
返回值是架构写死的后续动作指令（工具类→如实说明；技能类→先检索再总结流程），
模型只负责执行指令，不负责决定记录什么格式。

台账去向：kind=tool → capability_missing（能力缺失）；kind=skill →
skill_missing（技能缺失）。管理界面在独立的「能力缺口」栏目。
"""
from __future__ import annotations

import re

from .. import gaps
from .base import SAFE, make_tool

_TOOL_REPLY = (
    "已登记缺失能力「{name}」（写入了能力缺口台账）。\n"
    "接下来：\n"
    "1. 如实告知用户：当前缺少该能力，这部分无法完成，并说明缺的是什么；\n"
    "2. 绝不允许编造结果或假装已执行；\n"
    "3. 其余不依赖该能力的部分照常完成。"
)
_SKILL_REPLY = (
    "已登记缺失技能「{name}」（写入了能力缺口台账）。\n"
    "接下来：\n"
    "1. 用 github_search / web_search 检索这类任务的通用做法"
    "（一手来源优先，搜索摘要只能当入口）；\n"
    "2. 在最终回复里总结出一套可操作的流程（分步、可执行）；\n"
    "3. 流程若可复用，收尾自检机制可能会把它沉淀为技能。"
)


def build_gap_tools(guard, cfg):
    def report_gap(kind: str, name: str, spec: str = "", detail: str = "") -> str:
        k = str(kind or "").strip().lower()
        name = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
        spec = re.sub(r"\s+", " ", str(spec or detail or "")).strip()[:600]
        if k not in ("tool", "skill"):
            return "[错误] kind 只能是 tool（缺工具/能力）或 skill（缺技能/方法论）。"
        if not name:
            return "[错误] name 为空：请给出缺失能力/技能的名字（如 send_email）。"
        # 调用即证据：登记动作全在架构侧，参数原样入账
        gaps.record("capability_missing" if k == "tool" else "skill_missing",
                    name, spec or "模型报告缺失", cfg)
        tpl = _TOOL_REPLY if k == "tool" else _SKILL_REPLY
        return tpl.format(name=name) + (f"\n登记说明：{spec}" if spec else "")

    return [
        make_tool(
            "report_gap",
            "登记「完成任务必需但环境里没有」的工具/能力或技能/方法论。"
            "发现缺工具时 kind=tool（如 send_email）；缺某类任务的可复用方法时 "
            "kind=skill（如 excel 批量处理）。调用后按返回的指令继续："
            "缺工具→如实告知用户无法完成；缺技能→先检索资料再总结操作流程。"
            "不要在环境里已有对应工具/技能时调用本工具。",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["tool", "skill"],
                             "description": "tool=缺工具/能力；skill=缺技能/方法论"},
                    "name": {"type": "string",
                             "description": "缺失项的名字（如 send_email、excel_batch）"},
                    "spec": {"type": "string",
                             "description": "功能/规范文案：它需要做什么、输入输出是什么"},
                },
                "required": ["kind", "name"],
            },
            report_gap,
            risk=SAFE,
        )
    ]
