"""来源核查（收尾闸门第三、四道）：结论必须有来源。

同一个病的两种表现——**拿没查过的东西当查过的写出来**。

**第三道 · 外部事实**（背景：REA 检索实测）：给 agent 一个外部检索任务，它只调了
`web_search`，把搜索结果摘要里的「star 已达 1w+」写进结论——GitHub API 实测是
**17 万**，差一个数量级；「最近更新」写成「2026 年 7 月」，实际是当天。工具没坏，
坏在**来源性质**：「搜索摘要（转述）」和「一手来源（实测）」在它眼里没有区别。

判定（三个条件同时成立才出手）：

    1. 任务里调用过联网搜索（web_search）
    2. 任务里**从未**抓过一手来源（fetch_url / fetch_json / browser_snapshot / github_search）
    3. 最终总结里出现了外部事实的量化或时效性措辞（数字、版本、日期、star/fork…）

**3 的补充（背景：同一批三跑的 A 组）**：它通篇是定性转述——「10 月初走红」
这种说法一个阿拉伯数字都没有，于是旧判据静默放行。**「没写数字」不等于「有据」**，
时效性说法同样是转述。所以第 3 条扩成两路，命中任一即可：

    3a. 量化措辞（数字 / 版本 / 年月日）——判据在 _EXTERNAL_CLAIM；
    3b. 时效性措辞（最近 / 月初 / 刚发布 / 最新版…）——**且**总结确实在谈搜过的
        那个东西（从 web_search 的 query 里抽实词比对）。这一层附带的「谈的是
        搜过的东西」条件，是为了不让本地任务里的「最近改的那块代码」误伤。

**第四道 · 本机事实**（背景：端到端实测）：让它「列出工作区根目录下的文件」，
25 秒、**0 次工具调用**，答复里凭空列着 `agent-forge.json` / `core/` / `ui/` /
`venv/`——这些在工作区里都不存在。外部检索有闸门，本机检索却没有。

判定（同样三条同时成立）：

    1. 任务要的就是「工作区里现在有什么」（列目录 / 有哪些文件 / 看现状…），
       且不是在让它造东西（造东西时出现的文件名是它自己起的，不算对现状的陈述）
    2. 总结里陈述了工作区现状——明确的列举标记，或 ≥2 个文件 / 目录名
    3. 整个任务**没有调用过任何能读工作区的工具**（list_dir / read_file /
       find_files / search_code…，跑过命令也算读过）

两道都在命中后由程序在答复末尾追加声明：内容属「未经核实」。**不改写正文、
不阻塞收尾**——只是把证据等级写清楚，让读的人知道该不该采信。要真正采信，得先
去查，那不归这一层管（这一层只负责把话说清楚）。

第一道防线是提示词（`loop.py` 的结论纪律）与工具输出上的来源标签（`web.py` 的
`[搜索摘要 · 转述 · 未核实]` / `[实测 · 网页]` / `[实测 · API]`）。但那是「劝」——
模型可以不听。所以再加系统侧核查，与收尾验证闸门同一思路：
**模型只有解读权，没有裁定权**。
"""

from __future__ import annotations

import json
import re

# 联网搜索类工具：只提供「转述」（别人的摘要），不能作为事实依据。
SEARCH_TOOLS = ("web_search",)

# 一手来源类工具：抓回来的就是原始内容 / 官方接口的字段，可以直接引用。
# browser_screenshot 不算——它拿的是图片，无法作为文字事实的来源。
# github_search 算：它打的同样是 api.github.com（官方搜索接口），star / fork
# 这些字段就是权威值本身——不能因为「它不是 fetch_* 开头」就把它当转述。
FETCH_TOOLS = ("fetch_url", "fetch_json", "browser_snapshot", "github_search")

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


# ---------------------------------------------------------------------------
# 时效性定性转述：没有数字，也还是「转述」
# ---------------------------------------------------------------------------
# 「10 月初走红」「最近刚发布的那个版本」——一个阿拉伯数字都没有，旧判据（只认
# 数字 / 年月）静默放行。可它和「star 已达 1w+」是同一个东西：都是把别人摘要里的
# 说法当结论写出来。所以要认时效性措辞，而不是只认数字。
_TIMELINESS_RX = re.compile(
    r"(?:最近|近期|日前|前些天|前几天|这两天|这段时间|本周|这周|上周|本月|这个月|上个月"
    r"|月初|月底|上旬|中旬|下旬|今年|去年|刚发布|刚刚发布|刚上线|刚更新|刚推出|刚放出"
    r"|最新版本|最新版|新版|新版本|新发布|新上线|近期更新|刚刚|才发布|近日)"
)

_WORD_EN = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,}")
_CJK_SEG = re.compile(r"[\u4e00-\u9fff]{2,}")

# 高频泛词：出现在几乎任何任务书里，不指向「要查的那个东西」，比对时要滤掉，
# 否则「命中搜索词」这个条件会退化成恒真。
_TERM_STOP = {
    "项目", "东西", "什么", "怎么", "介绍", "查询", "一下", "帮我", "看看", "那个", "这个",
    "资料", "信息", "知道", "了解", "关于", "情况", "相关", "内容", "上面", "下面", "以及",
    "可以", "需要", "要求", "工作", "使用", "进行", "它是", "是什", "是一", "一个", "一下",
}


def _terms_of(text: str) -> set[str]:
    """把一段文字切成比对用的「实词」（英文词 + 中文词段与二元组，去泛词）。"""
    t = str(text or "")
    out: set[str] = set()
    for w in _WORD_EN.findall(t):
        out.add(w.lower())
    for seg in _CJK_SEG.findall(t):
        if seg not in _TERM_STOP:
            out.add(seg)
        for i in range(len(seg) - 1):
            g = seg[i:i + 2]
            if g not in _TERM_STOP:
                out.add(g)
    return {x for x in out if x not in _TERM_STOP}


def search_queries(convo: list[dict] | None) -> list[str]:
    """从对话历史里取出所有联网搜索用过的查询串（工具调用的参数，不是模型自报）。"""
    out: list[str] = []
    for m in convo or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or ""
            if name not in SEARCH_TOOLS:
                continue
            args = fn.get("arguments")
            if args is None:
                args = tc.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    args = {}
            if isinstance(args, dict):
                q = args.get("query") or args.get("q") or ""
                if str(q).strip():
                    out.append(str(q))
    return out


def timeliness_claim_hits(summary: str, convo: list[dict] | None,
                          cap: int = MAX_HITS) -> list[str]:
    """总结里的时效性措辞，且**总结确实在谈搜过的那个东西**时才算命中。

    只认措辞会把本地任务误伤（「最近改的那块代码」也是「最近」），所以要求
    总结与某次搜索的查询串有实词交集——用过搜索 + 谈的又是搜过的东西，
    这时说「最近」才是在转述外部事实。
    """
    s = str(summary or "")
    if not s:
        return []
    qterms: set[str] = set()
    for q in search_queries(convo):
        qterms |= _terms_of(q)
    if not qterms:
        return []
    if not (qterms & _terms_of(s)):
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for m in _TIMELINESS_RX.finditer(s):
        k = m.group(0)
        if k in seen:
            continue
        seen.add(k)
        hits.append(k)
        if len(hits) >= cap:
            break
    return hits


def needs_external_note(summary: str, convo: list[dict] | None) -> list[str]:
    """是否需要追加「转述·未核实」声明；需要就返回命中片段，否则空表。

    两路判据命中任一即可：量化措辞（数字 / 年月日），或时效性措辞 + 谈的是搜过的
    东西。后面两个「没抓一手来源」的否决条件对两路一视同仁。
    """
    hits = external_claim_hits(summary)
    if not hits:
        hits = timeliness_claim_hits(summary, convo)
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
        "一手来源（`fetch_url` / `fetch_json` / `browser_snapshot` / `github_search`），"
        "下列内容属「转述 · 未经核实」，请勿直接采信。**时效性说法同样是转述**——「最近」"
        "「月初」这类词和具体数字一样，需要一手来源支撑：",
    ]
    lines += [f"- {h}" for h in hits]
    lines.append(
        "此核查由程序完成，不依赖模型自报；要采信这些数字，应先抓取一手来源"
        "（官方站点 / 官方 API，如 GitHub 仓库用 `https://api.github.com/repos/<owner>/<repo>`、"
        "找仓库用 `github_search`）核实。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 第四道：本机事实（工作区里有什么）
# ---------------------------------------------------------------------------
# 结论的来源不光有「外部站点」，还有「工作区本身」。工作区里有什么只能看，不能想。

# 只读工具：调用过任意一个，就说明它真的看过现场。
LOCAL_READ_TOOLS = (
    "list_dir", "find_files", "read_file", "search_code", "impact_of",
    "git_status", "git_log", "git_diff", "git_show",
    "novel_stats", "task_notes", "ssh_info",
)
# 也可能顺带看过现场的（跑命令 / 远程命令）。判据宁宽不宁窄——这是「提醒」不是「拦截」，
# 宁可漏报一次，也不要对着真查过现场的回答说「你根本没查」。
LOCAL_EVIDENCE_TOOLS = LOCAL_READ_TOOLS + ("run_command", "ssh_run")

# 文件 / 目录名的识别。刻意只认「像文件名」的（带已知扩展名）和「以 / 结尾的目录」，
# 不去猜裸词——裸词里的误报（「框架」「工具」）会把这一层变成噪音。
_EXT = (
    "py|pyi|js|mjs|cjs|jsx|ts|tsx|vue|svelte|java|go|rs|c|h|cpp|hpp|cs|rb|php"
    "|sh|bash|ps1|bat|cmd|sql|html|htm|css|scss|json|yaml|yml|toml|ini|cfg|conf"
    "|md|rst|txt|csv|xml|env|lock|lnk|exe|dll|pem|key|log|png|jpg|jpeg|svg|pdf"
)
_FILE_RX = re.compile(
    r"(?<![\w./\\-])((?:[\w\-.]+/)*[\w\-.]+\.(?:" + _EXT + r"))(?![\w])", re.I)
_DIR_RX = re.compile(r"(?<![\w./\\-])([\w\-.]+/)(?!/?[\w])")
_WINPATH_RX = re.compile(r"[A-Za-z]:\\[^\s，。；、）)】\"']+")

# 「工作区现状」在任务书里的说法。三种句式都算：
#   ① 动词在前：列出…文件 / 看一下…loop.py
#   ② 名词在前：代码是怎么实现的 / 项目里有哪些目录
#   ③ 直接点「现状」：工作区、根目录、目录结构…
# 只认 ①② 会漏掉「看一下 agentforge/loop.py 里工具超时是怎么处理的」这种最日常的问法。
_WS_NOUN = (r"(?:文件|目录|文件夹|源码|代码|内容|清单|列表|结构|实现|定义|配置"
            r"|[\w\-.]+\.(?:" + _EXT + r")|[\w\-.]+/)")
_ASK_VERB = (r"(?:列出|列一下|罗列|有哪些|哪些|有没有|存在吗|查看|查一下|看一下|看看"
             r"|读一下|读读|打开|显示|展示|里面(?:有|是)什么|包含(?:哪些|什么))")
_ASK_TAIL = r"(?:是什么|有什么|干什么|怎么样|如何|怎么|在哪儿|在哪)"
_ASK_RX = re.compile(
    _ASK_VERB + r"[^。；\n]{0,14}" + _WS_NOUN
    + r"|" + _WS_NOUN + r"[^。；\n]{0,10}" + _ASK_TAIL
    + r"|(?:文件列表|目录列表|目录结构|项目结构|文件结构|文件夹结构|工作区|根目录)",
)
# 任务书在让「造东西」时不算：那种任务里冒出来的文件名是它自己起的，
# 不是对现有目录的陈述（产物是否存在另有一道核查，见 verify.py）。
# 「实现」单独拉出来说：检视提问的最高频句式恰恰是「X 是怎么实现的」「X 的实现」
# （planner 的检视信号词里就收着「当前的实现」）——前面紧邻「的 / 怎么 / 如何 /
# 怎样」时它是在问现状，不是在让造东西；「实现一个导出」「重新实现 parse」
# 这类真创建动作不受影响。
_CREATE_RX = re.compile(
    r"(?:写|撰写|起草|创建|新建|生成|设计|规划|建议|推荐|重构|修改|改成|改为"
    r"|添加|新增|(?<!的)(?<!怎么)(?<!如何)(?<!怎样)实现|补上|加上|加个|做个)",
)

# 回复里「在陈述工作区现状」的标记。光有文件名不够——「已创建 tools/x.py」里也有
# 文件名，但那是在说自己干了什么，不是对现有目录的陈述。
_LISTING_RX = re.compile(
    r"(?:根目录|目录结构|文件列表|目录列表|文件结构|文件夹结构|如下文件|以下(?:文件|目录)"
    r"|文件如下|目录如下|包含以下|工作区(?:里|中|内)|当前(?:目录|文件)|现有(?:文件|目录))",
)


def asks_workspace_state(message: str) -> bool:
    """任务书是不是在问「工作区里现在有什么」（且不是在让它造东西）。"""
    m = str(message or "")
    if not _ASK_RX.search(m):
        return False
    return not _CREATE_RX.search(m)


def ran_local_inspection(convo: list[dict] | None) -> bool:
    """任务过程中是否读过工作区（任意读取类工具，或跑过命令）。"""
    return any(n in LOCAL_EVIDENCE_TOOLS for n in tool_names(convo))


def local_fact_tokens(summary: str, exclude_text: str = "", cap: int = MAX_HITS) -> list[str]:
    """总结里出现的「工作区现状」具体项（文件名 / 目录名 / 绝对路径，去重保序限量）。

    exclude_text 里出现过的名字不算——那可能是任务书本身点名的对象
    （「给 web/index.html 加个按钮」），不是凭空的现状陈述。
    """
    txt = str(summary or "")
    ex = str(exclude_text or "").lower()
    out: list[str] = []
    seen: set[str] = set()
    # Windows 反斜杠的相对路径（agentforge\loop.py）：_FILE_RX/_DIR_RX 只认 /
    # 分隔、_WINPATH_RX 只认盘符开头——反斜杠相对路径三条路全走不通，总结里
    # 按这种风格写路径时「列了不存在的东西」闸门整条放行。先把 \ 归一成 / 再跑
    # 后两条（盘符绝对路径在原文里已由 _WINPATH_RX 抓过，归一不会让它丢）。
    fwd = txt.replace("\\", "/")
    ex_fwd = ex.replace("\\", "/")
    for rx, hay in ((_WINPATH_RX, txt), (_FILE_RX, fwd), (_DIR_RX, fwd)):
        for m in rx.finditer(hay):
            t = (m.group(1) if m.groups() else m.group(0)).strip()
            if not t:
                continue
            k = t.lower()
            if k in seen or k in ex_fwd:
                continue
            seen.add(k)
            out.append(t)
            if len(out) >= cap:
                return out
    return out


def needs_local_note(summary: str, convo: list[dict] | None,
                     message: str = "") -> list[str]:
    """是否需要追加「本机事实未经核实」声明；需要就返回命中的文件/目录名。

    与 needs_external_note 同一路数：判据全部由程序算，不看模型自报。
    """
    if not asks_workspace_state(message):
        return []
    if ran_local_inspection(convo):
        return []
    tokens = local_fact_tokens(summary, exclude_text=message)
    if not tokens:
        return []
    # 单个名字可能是顺手提一句；要「≥2 项」或「明确在列清单」才当成现状陈述，
    # 免得把「已创建 foo.py」这类正常收尾也标一遍。
    if len(tokens) >= 2 or _LISTING_RX.search(str(summary or "")):
        return tokens
    return []


def local_unverified_block(tokens: list[str]) -> str:
    """附在最终答复末尾的系统侧声明。程序写的，模型只有解读权。"""
    lines = [
        "【系统侧核查】本回复描述了当前工作区里的文件 / 目录，但整个任务没有调用过任何"
        "读取工作区的工具（`list_dir` / `find_files` / `read_file` / `search_code`），"
        "下列内容未经核实，请勿直接采信：",
    ]
    lines += [f"- {t}" for t in tokens]
    lines.append(
        "工作区里到底有什么只能看、不能想：先 `list_dir` 列一遍，再写结论。"
        "（此核查由程序完成，不依赖模型自报。）"
    )
    return "\n".join(lines)


# 命中时给模型的一轮修正机会（硬约束：不能就这么把编的内容交出去）。
LOCAL_FACTS_CORRECTIVE_PROMPT = """【系统侧核查 · 需要修正】
你刚才的答复描述了工作区里的文件 / 目录，但这次任务从头到尾没有调用过任何读取
工作区的工具——这些内容不是看来的。被点出的部分：

{tokens}

现在按顺序做两件事：
1. 用 list_dir（必要时配合 find_files / read_file）真实看一遍工作区，把写错的地方改对；
2. 重新给出答复。查到什么写什么，查不到的如实说「未核实」，不要凭印象补全。

若你认为这些内容其实来自任务书本身（不是在陈述现状），说明一句即可。
你刚才的答复：
{summary}"""


__all__ = [
    "SEARCH_TOOLS",
    "FETCH_TOOLS",
    "LOCAL_READ_TOOLS",
    "LOCAL_EVIDENCE_TOOLS",
    "tool_names",
    "ran_search",
    "ran_first_party_fetch",
    "external_claim_hits",
    "search_queries",
    "timeliness_claim_hits",
    "needs_external_note",
    "external_unverified_block",
    "asks_workspace_state",
    "ran_local_inspection",
    "local_fact_tokens",
    "needs_local_note",
    "local_unverified_block",
    "LOCAL_FACTS_CORRECTIVE_PROMPT",
]
