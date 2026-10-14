"""工具预检：任务开始前，架构先判定「这个任务需要的能力，环境里到底有没有」。

背景（架构演进第 21 步的推广）：模型问「查 GitHub 上的项目」而环境没有
github_search 时，模型不知道自己没有这能力，于是凭训练记忆编了一份"搜索结果"——
错的不是模型，是 harness 没把能力边界摆到台面上。

做法与边界（只做确定的事）：

1. 意图信号一律保守（宁漏勿误）：信号词命中才认为任务需要某能力；命中但工具
   都在 → 什么都不做；命中且工具全缺 → 记缺口台账 + 生成「无法完成通告」。
   误判方向的代价刻意设计成不对称：把"要联网"误判成"要查 GitHub"毫无代价
   （工具在就不通告），漏判则回到旧行为，也不会更糟。
2. 「无法完成通告」由架构生成、注入 system 层（和 FAIL_NOTE 一个待遇），不是
   写死在系统提示词里的通用劝导——只有真的缺能力时才出现。
3. 诚实回答本身无法用代码"保证"（那要改模型），架构能保证的是两件硬事：
   模型永远拿不到伪造的工具结果（未注册工具必得确定性报错），以及缺能力时
   一定被明确告知"做不到，要如实说明"。最终回复是否如实，由评测集的
   诚实性簇（CAR-bench 式不可满足变体）验证红绿。
"""
from __future__ import annotations

import re

from . import gaps

# 能力需求表：key → {label 人话, tools 必需工具（有任意一个即算具备）,
# signal 意图信号（保守，宁漏勿误）}
_CAPABILITIES: list[dict] = [
    {
        "key": "github",
        "label": "GitHub 仓库检索",
        "tools": ["github_search"],
        # 查仓库/找开源项目；repo_keyword 是 web.py 里验证过的判定
        "signal": re.compile(
            r"github|git\s?hub|开源项目|开源仓库|搜(一)?下?(仓库|项目)"
            r"|找(一)?个?(仓库|项目)|仓库|repo\b", re.I),
    },
    {
        "key": "web",
        "label": "联网检索",
        "tools": ["web_search", "fetch_url"],
        "signal": re.compile(
            r"联网|上网|搜索|搜一下|查(一)?下?最新|最新(的|消息|版本|新闻)"
            r"|网上|网页|url|https?://|新闻|search\b", re.I),
    },
    {
        "key": "shell",
        "label": "命令执行",
        "tools": ["run_command"],
        "signal": re.compile(
            r"运行|执行(命令|脚本)|终端|命令行|装(一)?下?|pip install|npm install", re.I),
    },
    {
        "key": "git",
        "label": "版本管理",
        "tools": ["git_status", "git_clone", "git_commit"],
        "signal": re.compile(
            r"\bgit\b|提交代码|版本提交|clone|克隆(仓库|项目)?|push|pull request|PR\b", re.I),
    },
]

# 架构生成的「无法完成通告」。措辞是硬指令：缺的能力做不到，必须如实说明，
# 绝不允许编造；其余部分照常完成。
_INABILITY_NOTE = (
    "## 能力边界通告（系统预检，非用户输入）\n"
    "本任务涉及以下能力，但当前环境**没有提供**对应工具，你**无法实际完成**这部分：\n"
    "{missing}\n"
    "对涉及这些能力的部分，你必须如实告知用户「当前缺少该能力，无法完成」，"
    "并说明缺的是什么；**绝不允许**凭记忆编造结果、假装已执行或给出虚构的数据/链接。"
    "其余不依赖这些能力的部分照常完成。"
)


def assess(user_text: str, available: list[str]) -> list[dict]:
    """返回缺失能力列表。available = 注册表里实际有的工具名。

    每项 {key,label,need,have}；空列表 = 不缺任何东西（包括信号没命中的情况）。
    """
    text = str(user_text or "")
    if not text:
        return []
    have = {str(n) for n in (available or [])}
    missing: list[dict] = []
    for cap in _CAPABILITIES:
        if not cap["signal"].search(text):
            continue
        need = [t for t in cap["tools"] if t in have]
        if need:
            continue            # 有任意一个必需工具 → 能力在，不吭声
        missing.append({"key": cap["key"], "label": cap["label"],
                        "need": cap["tools"], "have": []})
    return missing


def note_for(user_text: str, available: list[str], cfg=None) -> str:
    """评估并生成通告文本；没缺失返回空串。缺了则同时记缺口台账。"""
    missing = assess(user_text, available)
    if not missing:
        return ""
    lines = [f"- {m['label']}（需要工具 {'、'.join(m['need'])}，一个都没有）"
             for m in missing]
    if cfg is not None:
        for m in missing:
            gaps.record("capability_missing", m["key"],
                        f"意图命中但工具全缺：{m['need']}", cfg)
    return _INABILITY_NOTE.format(missing="\n".join(lines))


__all__ = ["assess", "note_for"]
