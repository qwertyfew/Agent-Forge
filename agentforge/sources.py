"""一手来源核查（收尾闸门第三道）。

背景（REA 检索实测）：给 agent 一个外部检索任务，它只调了 `web_search`，把搜索
结果摘要里的「star 已达 1w+」写进结论——GitHub API 实测是 **17 万**，差一个数量级；
「最近更新」写成「2026 年 7 月」，实际是当天。工具没坏，坏在**来源性质**：
「搜索摘要（转述）」和「一手来源（实测）」在它眼里没有区别。

第一道防线是提示词（`loop.py` 的结论纪律）与工具输出上的来源标签（`web.py` 的
`[搜索摘要 · 转述 · 未核实]` / `[实测 · 网页]` / `[实测 · API]`）。但那是「劝」——
模型可以不听。所以再加一道系统侧核查，与收尾验证闸门同一思路：
**模型只有解读权，没有裁定权**。

判定（三个条件同时成立才出手）：

    1. 任务里调用过联网搜索（web_search）
    2. 任务里**从未**抓过一手来源（fetch_url / fetch_json / browser_snapshot）
    3. 最终总结里出现了外部事实的量化或时效性措辞（数字、版本、日期、star/fork…）

命中就在答复末尾追加程序写的声明：这些内容属「转述 · 未经核实」。**不改写正文、
不阻塞收尾**——只是把证据等级写清楚，让读的人知道该不该采信。要真正采信，得先抓
一手来源，那不归这一层管（这一层只负责把话说清楚）。
"""

from __future__ import annotations

import re

# 联网搜索类工具：只提供「转述」（别人的摘要），不能作为事实依据。
SEARCH_TOOLS = ("web_search",)

# 一手来源类工具：抓回来的就是原始内容 / 官方接口的字段，可以直接引用。
# browser_screenshot 不算——它拿的是图片，无法作为文字事实的来源。
FETCH_TOOLS = ("fetch_url", "fetch_json", "browser_snapshot")

# ---------------------------------------------------------------------------
# 「外部可核实事实」的判据
# ---------------------------------------------------------------------------
# 英文指标词：加前后非字母断言，避免 "star" 命中 "started"、"issue" 命中 "issues" 以外
# 的单词（如 "tissue"）。
_METRIC_EN = r"(?<![A-Za-z])(?:stars?|forks?|watchers?|issues?|contributors?|version|releases?)(?![A-Za-z])"
# 中文指标词：数字/日期类的外部事实常见载体
_METRIC_ZH = (
    r"(?:星标|标星|加星|涨星|星数|收藏数|复刻|分叉|贡献者|下载量|安装量|装机量"
    r"|用户数|活跃用户|市占率?|市场份额|市值|营收|融资|估值|股价|汇率|利率"
    r"|版本号?|发布日期|发布时间|更新时间|最后更新|最近更新|上线时间)"
)
_METRIC = r"(?:" + _METRIC_EN + r"|" + _METRIC_ZH + r")"
# 数字（千分位 / 小数 / 中文数量级 / k-w-m 后缀 / + 号）。刻意不吞空白：
# 让它跨行去凑「版本 3\n\n第 2026 行」这种组合会把误报放大。
_NUM = r"\d[\d,]*(?:\.\d+)?(?:万|亿|千|百|[kKwWmM])?\+?"
# 指标词与数字之间的夹字：「Star **数已达** 1w+」——中文表达里这块可以有好几个字。
# 限 8 个非数字字符、且不许跨行，兼顾召回与误报。
_GAP = r"[^0-9\n]{0,8}"

_EXTERNAL_CLAIM = re.compile(
    # 「star 1w+」「Star 数已达 1w+」「涨星超 4600」「版本 v3.2.1」
    r"(?:" + _METRIC + r")" + _GAP + _NUM +
    # 「171067 star」「1w+ 星标」「40,631 fork」
    r"|" + _NUM + _GAP + r"(?:" + _METRIC + r")"
    # 年月 / 年月日（时效性结论最常见的载体）
    r"|(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月)?"
    r"|(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}",
    re.I,
)

# 声明块里最多列几条（防极端输入把答复撑爆）
MAX_HITS = 6


def tool_names(convo: list[dict] | None):
    """遍历对话历史里模型调用过的工具名（与 verify.py 同一口径）。"""
    for m in convo or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            name = ((tc.get("function") or {}).get("name")
                    or tc.get("name") or "")
            if name:
                yield name


def ran_search(convo: list[dict] | None) -> bool:
    """任务过程中是否调用过联网搜索。"""
    return any(n in SEARCH_TOOLS for n in tool_names(convo))


def ran_first_party_fetch(convo: list[dict] | None) -> bool:
    """任务过程中是否抓过一手来源（网页正文 / JSON 接口 / 浏览器可访问文本）。"""
    return any(n in FETCH_TOOLS for n in tool_names(convo))


def external_claim_hits(summary: str, cap: int = MAX_HITS) -> list[str]:
    """总结里出现的外部量化/时效性措辞（去重保序，限量）。

    返回的是命中片段本身，用于在声明块里逐条列出来——比笼统说一句
    「本回复含未核实内容」有用得多：读的人一眼看到是哪几个数字在裸奔。
    """
    hits: list[str] = []
    seen: set[str] = set()
    for m in _EXTERNAL_CLAIM.finditer(str(summary or "")):
        s = " ".join(m.group(0).split())
        k = s.lower()
        if not s or k in seen:
            continue
        seen.add(k)
        hits.append(s)
        if len(hits) >= cap:
            break
    return hits


def needs_external_note(summary: str, convo: list[dict] | None) -> list[str]:
    """是否需要追加「转述·未核实」声明；需要就返回命中片段，否则空表。"""
    hits = external_claim_hits(summary)
    if not hits:
        return []
    if not ran_search(convo):
        return []
    if ran_first_party_fetch(convo):
        return []
    return hits


def external_unverified_block(hits: list[str]) -> str:
    """附在最终答复末尾的系统侧声明。程序写的，模型只有解读权。"""
    lines = [
        "【系统侧核查】本回复含来自联网检索的外部事实，但整个任务没有抓取过任何"
        "一手来源（`fetch_url` / `fetch_json` / `browser_snapshot`），下列内容属"
        "「转述 · 未经核实」，请勿直接采信：",
    ]
    lines += [f"- {h}" for h in hits]
    lines.append(
        "此核查由程序完成，不依赖模型自报；要采信这些数字，应先抓取一手来源"
        "（官方站点 / 官方 API，如 GitHub 仓库用 `https://api.github.com/repos/<owner>/<repo>`）核实。"
    )
    return "\n".join(lines)


__all__ = [
    "SEARCH_TOOLS",
    "FETCH_TOOLS",
    "tool_names",
    "ran_search",
    "ran_first_party_fetch",
    "external_claim_hits",
    "needs_external_note",
    "external_unverified_block",
]
