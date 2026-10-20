"""网络与浏览器工具族。

对照成熟 Agent 产品（Codex）的能力集：
- web_search        联网搜索（查资料第一步；**只用来找入口**，返回的是转述）
- github_search     按名字搜 GitHub 仓库（实测一手来源：官方搜索接口，按 star 排序的候选）
- fetch_url         抓取网页正文（实测一手来源：官方页面 / 文档）
- fetch_json        抓取 JSON 接口并投影字段（实测一手来源：GitHub / 包仓库等 API）
- browser_snapshot  无头浏览器读取 JS 渲染页面的可访问文本
- browser_screenshot 无头浏览器截图（用于界面验证 / 视觉确认）
- open_browser      在用户本机默认浏览器中打开网页（给用户看）

【为什么要有 fetch_json】REA 检索实测的教训：模型只调 web_search，把搜索结果摘要里
的「star 1w+」当成事实写进结论（真实值是 17 万），而它手里明明有 fetch_url。
根因是「搜索摘要」与「一手来源」在工具输出里长得一样，小模型分不清。所以：
    1. 每个工具的输出头显式标注来源性质——`[搜索摘要 · 转述 · 未核实]` /
       `[实测 · 网页]` / `[实测 · API]`，让模型一眼看出哪条能引用；
    2. 专门给 API 一个工具，把「打官方接口拿真实数据」变成一次调用能办的事，
       而不是要求小模型自己拼 URL、自己读一大坨 JSON。
收尾还有系统侧核查兜底（agentforge/sources.py）：只用过搜索、没抓过一手来源、
却在总结里写了数字 → 答复末尾由程序追加「转述·未经核实」声明。

【为什么要有 github_search】REA 检索的第二轮实测：问「git 上叫 REA 的项目」，
模型连搜四枪，最后一枪抓的是 `api.github.com/repos/bbylw/rea-cn`——一个 80 star
的中文镜像站（它没错在「没抓一手来源」，抓了），而正主 `morluto/rea` 有 19 万 star。
错在**找错了对象**：搜索结果里压根没有正主，模型只能锚定它看到的第一个 GitHub 链接。

根因是工具链缺一档能力：`git_*` 全是本地仓库操作，`fetch_json` 只认已知的
`owner/repo`——**没有能把「REA」这个字符串变成候选清单的工具**。而 GitHub 官方
搜索接口一次调用就给得出「按 star 排序」的候选（正主排第 2，第 1 是 `react/react`，
"REA" 只是它的子串）。所以：选哪个由模型判断，但**「有哪些候选、各自多少 star」
必须由程序摆到台面上**。web_search 也会顺手兜这一枪（见 repo_keyword）。
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import webbrowser
from pathlib import Path

import requests

from .base import SAFE, make_tool

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _to_text(raw: str) -> str:
    raw = _TAG_RE.sub(" ", raw)
    raw = _HTML_RE.sub("\n", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    return _WS_RE.sub("\n\n", raw).strip()


_UA = {"User-Agent": "AgentForge/0.1 (local agent)"}


def _get(url: str, timeout: int = 25) -> requests.Response:
    return requests.get(url, timeout=timeout, headers=_UA)


# ---------------------------------------------------------------------------
# 失败时的一等公民降级路径（背景：REA 检索三跑）
# ---------------------------------------------------------------------------
# fetch_url 被代理拒之后，模型手里明明有 fetch_json 却没用，转而去猜了个
# browser（白烧两轮）——而「GitHub 仓库打 api.github.com/repos/o/r」这条正解，
# 就写在它刚读过的工具说明里。问题不在能力，在**失败那一刻没人把这条和眼前的
# 错误连起来**，模型只能临场猜工具名。所以由程序把下一步直接写出来。
#
# 判据全部是程序算的：按失败类型（网络层 / 4xx / 5xx）决定说哪一句，按 URL 形态
# 决定给哪条现成的接口地址。模型只有解读权，没有「想起来该换哪个工具」的义务。

_API_EQUIV = (
    # 允许尾部带 ?tab=readme / #readme 这类后缀——从浏览器地址栏复制的链接常带着
    (re.compile(r"^https?://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)/?(?:[?#].*)?$", re.I),
     "https://api.github.com/repos/{0}/{1}",
     "GitHub 仓库页 → 官方 API（star / fork / 更新时间等权威字段）"),
    (re.compile(r"^https?://(?:www\.)?npmjs\.com/package/([^/?#]+)/?(?:[?#].*)?$", re.I),
     "https://registry.npmjs.org/{0}",
     "npm 包页 → 官方 registry（版本、依赖、发布时间）"),
    (re.compile(r"^https?://(?:www\.)?pypi\.org/project/([^/?#]+)/?(?:[?#].*)?$", re.I),
     "https://pypi.org/pypi/{0}/json",
     "PyPI 包页 → 官方 JSON 接口（版本、发布时间）"),
)


def api_equivalent(url: str) -> tuple[str, str]:
    """网页地址能换算成官方接口地址就返回 (接口地址, 一句说明)，否则 ("", "")。"""
    for rx, tpl, why in _API_EQUIV:
        m = rx.match(str(url or "").strip())
        if m:
            return tpl.format(*m.groups()), why
    return "", ""


def fetch_failure_hint(url: str, err: Exception) -> str:
    """网页抓取失败时写出具体的下一步（程序写的，不靠模型猜工具名）。"""
    api, why = api_equivalent(url)
    status = getattr(getattr(err, "response", None), "status_code", 0) or 0
    if not status:
        head = ("[下一步] 这是**网络层失败**（代理 / 超时 / 连不上 / 内容被拦），"
                "网页正文这条路当前走不通——换个口子就好：")
    elif 400 <= status < 500:
        head = (f"[下一步] 服务器返回 {status}（多半是地址不对、页面已迁移或要登录）。"
                "先用 web_search 确认正确地址；若这是仓库 / 包页面，直接换官方接口更省事：")
    else:
        head = (f"[下一步] 服务器返回 {status}（站点自身故障）。可以稍后重试，"
                "或换官方接口拿同样的数据：")
    tips = []
    if api:
        tips.append(f'    fetch_json(url="{api}")   ← {why}')
    tips.append("    目标若是别的站点：先想它有没有公开 API（REST / JSON 端点），"
                "有就用 fetch_json 打官方接口——结构化数据不经页面渲染，也不受页面级拦截影响。")
    tips.append("    确认只有网页可用时，才退回 browser_snapshot（无头浏览器，能拿 JS 渲染后的文本）。")
    return head + "\n" + "\n".join(tips)


# ---------------------------------------------------------------------------
# GitHub 仓库检索：按名字找项目（背景：REA 检索四跑，见模块文档串）
# ---------------------------------------------------------------------------
_GH_SEARCH_API = "https://api.github.com/search/repositories"

# 排序口径 → 接口参数。GitHub 认 stars / forks / updated / help-wanted-issues；
# 不传 sort 就是 best-match（相关度）。对外只暴露常用三种 + 默认相关度。
_GH_SORTS: dict[str, tuple[str, str]] = {
    "stars": ("stars", "star 数"),
    "forks": ("forks", "fork 数"),
    "updated": ("updated", "最近更新"),
    "best-match": ("", "相关度"),
}
_GH_DEFAULT_SORT = "stars"


def _gh_headers() -> dict:
    """接口请求头：带上 token 能把限流从 60 次/小时抬到 5000 次/小时。"""
    head = dict(_UA)
    head["Accept"] = "application/vnd.github+json"
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        head["Authorization"] = f"Bearer {token}"
    return head


def _gh_error(err: Exception) -> str:
    """把接口异常翻译成模型/用户能照着办的下一步。"""
    status = getattr(getattr(err, "response", None), "status_code", 0) or 0
    if status in (403, 429):
        return (f"接口拒绝（{status}）：多半是未鉴权的调用次数用完了（每小时 60 次）。"
                "等一会儿再试，或设置环境变量 GITHUB_TOKEN 提高额度。")
    if status == 404:
        return "接口返回 404（地址不对）。"
    if status == 422:
        return "查询写法不被接口接受（422），换个关键词再试。"
    if status:
        return f"接口返回 {status}。"
    return f"{type(err).__name__}: {err}"


def _gh_item(raw: dict) -> dict:
    """从接口的仓库对象里只取要用的字段（原始对象有 80+ 字段、几十 KB）。"""
    name = str(raw.get("name") or "")
    full = str(raw.get("full_name") or "")
    return {
        "name": name,
        "full_name": full,
        # 名字归一化：比较「是不是同名」时用它，别让 UI-Critic / ui-critic
        # 因大小写与连字符变成两个名字（rerun50 实证：精确引号查询能命中
        # Akins20/ui-critic，但旧版渲染只给全名，模型看不出它就是 UI-Critic）
        "key": re.sub(r"[^a-z0-9]", "", (name or full).lower()),
        "url": str(raw.get("html_url") or ""),
        "stars": int(raw.get("stargazers_count") or 0),
        "forks": int(raw.get("forks_count") or 0),
        "language": str(raw.get("language") or ""),
        "license": str((raw.get("license") or {}).get("spdx_id") or ""),
        "pushed_at": str(raw.get("pushed_at") or "")[:10],
        "description": str(raw.get("description") or ""),
    }


def smart_repo_query(query: str) -> str:
    """把「UI-Critic 项目」这类检索串整理成 GitHub 搜索语法。

    不能直接把原串丢给接口（rerun50 实证）：`GitHub UI-Critic project` 的
    GitHub 搜索是「这几个词都出现在仓库元数据里」——命中 111 个，全是
    含 ui / project 的无关仓库，排序按 star 还把它们顶到前面，正主一条没有。
    把名字挑出来做**精确词**搜索（`"UI-Critic"`）后 total 只有 5，全是同名项目。
    """
    s = str(query or "").strip()
    if not s:
        return ""
    slug = _SLUG_RX.search(s)
    if slug:
        return slug.group(1)
    kw = repo_keyword(s)
    return kw or s


def exact_name_query(name: str) -> str:
    """按名字查同名项目：精确词 + 限定在名字里（比裸搜更能命中真同名）。"""
    return f'"{name}" in:name'


def _norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _exact_name_hit(found: dict, name: str) -> dict | None:
    """候选里有没有名字同名的项目（忽略大小写/连字符）；有就返回那一条。"""
    k = _norm_key(name)
    if not k:
        return None
    for it in (found or {}).get("items") or []:
        if it.get("key") == k:
            return it
    return None


def npm_package_search(query: str, limit: int = 5, timeout: int = 25) -> dict:
    """按名字搜 npm 包（官方 registry 实测），返回精简候选清单。

    形状对齐 github_repo_search：items / total / error。
    「找某个叫 XX 的项目」时 GitHub 搜不到不代表不存在——rerun50 的
    UI-Critic 正主是 npm 包（alex410000/ui-critic），GitHub 搜索接口
    压根查不到它。包仓库是另一个生态入口，该由程序补上这一枪。
    """
    q = str(query or "").strip()
    if not q:
        return {"items": [], "total": 0, "error": "查询词为空，没东西可搜"}
    try:
        resp = requests.get(
            "https://registry.npmjs.org/-/v1/search",
            params={"text": q, "size": max(1, min(int(limit or 5), 20))},
            timeout=timeout,
            headers={"Accept": "application/json", **_UA},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        return {"items": [], "total": 0, "error": f"{type(e).__name__}: {e}"}
    items = []
    for obj in (data.get("objects") or [])[: int(limit or 5)]:
        pk = obj.get("package") or {}
        if not isinstance(pk, dict):
            continue
        items.append({
            "name": str(pk.get("name") or ""),
            "key": _norm_key(pk.get("name") or ""),
            "version": str(pk.get("version") or ""),
            "url": str((pk.get("links") or {}).get("npm")
                       or f"https://www.npmjs.com/package/{pk.get('name') or ''}"),
            "repo": str((pk.get("links") or {}).get("repository") or ""),
            "date": str(pk.get("date") or "")[:10],
            "description": str(pk.get("description") or ""),
        })
    return {"items": items,
            "total": int((data.get("total") if isinstance(data.get("total"), int) else len(items))),
            "error": ""}


def render_npm_rows(found: dict, query: str, note: str = "") -> str:
    """把 npm_package_search 的结果渲染成工具输出（口径对齐 render_repo_rows）。"""
    items = (found or {}).get("items") or []
    if not items:
        why = (found or {}).get("error") or "没有命中任何包"
        return f"[实测 · API] npm 官方 registry 搜索「{query}」没拿到结果：{why}"
    head = (f"[实测 · API] npm 官方 registry 搜索 · 查询「{query}」"
            f" · 命中 {int((found or {}).get('total') or 0)} 个，取前 {len(items)}")
    if note:
        head += f"｜{note}"
    lines = [head + "："]
    for i, it in enumerate(items, 1):
        bits = [it["name"] or "(无名)"]
        if it["version"]:
            bits.append(f"v{it['version']}")
        if it["date"]:
            bits.append(f"发布={it['date']}")
        lines.append(f"  {i}. " + "  ".join(bits))
        if it["description"]:
            lines.append(f"     {it['description'][:140]}")
        if it["url"]:
            lines.append(f"     {it['url']}")
        if it["repo"]:
            lines.append(f"     源码仓库: {it['repo']}")
    return "\n".join(lines)


def exact_repo_lookup(name: str, timeout: int = 25) -> dict:
    """按名字查「名字同名的 GitHub 项目」，返回接口原始条目（_gh_item 形状）。

    两枪：精确词搜索（`"ui-critic"`）+ 名字限定（`"ui-critic" in:name`）。
    谁同名谁算命中——**不按 star 排**：rerun50 的正主 Akins20/ui-critic 是
    0 star 新项目，按 star 排永远轮不到它，模型于是去问用户。
    """
    name = str(name or "").strip()
    if not name:
        return {"items": [], "error": "名字为空"}
    seen: set[str] = set()
    picked: list[dict] = []
    errs: list[str] = []
    for q in (exact_name_query(name), f'"{name}" in:name in:readme'):
        try:
            resp = requests.get(
                _GH_SEARCH_API,
                params={"q": q, "per_page": 20},
                timeout=timeout,
                headers=_gh_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            errs.append(_gh_error(e))
            continue
        for raw in (data.get("items") or []):
            if not isinstance(raw, dict):
                continue
            item = _gh_item(raw)
            if item["key"] == _norm_key(name) and item["full_name"] not in seen:
                seen.add(item["full_name"])
                picked.append(item)
        if picked:
            break
    return {"items": picked, "error": "; ".join(errs) if not picked and errs else ""}


def github_repo_search(query: str, sort: str = _GH_DEFAULT_SORT,
                       limit: int = 5, timeout: int = 25) -> dict:
    """按名字搜 GitHub 仓库，返回精简后的候选清单。

    返回 dict：
      items —— [{full_name, url, stars, forks, language, license, pushed_at, description}]
      total —— 接口报告的命中总数
      error —— 失败原因（空串表示成功）；失败时 items 一律为空

    任何异常都吞掉转成 error 字符串——工具不能因为一次网络抖动就抛栈。
    """
    q = str(query or "").strip()
    if not q:
        return {"items": [], "total": 0, "error": "查询词为空，没东西可搜"}
    key = str(sort or _GH_DEFAULT_SORT).strip().lower()
    api_sort, _ = _GH_SORTS.get(key, _GH_SORTS[_GH_DEFAULT_SORT])
    params: dict = {"q": q, "per_page": max(1, min(int(limit or 5), 20))}
    if api_sort:
        params["sort"] = api_sort
        params["order"] = "desc"
    try:
        resp = requests.get(_GH_SEARCH_API, params=params, timeout=timeout,
                            headers=_gh_headers())
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        return {"items": [], "total": 0, "error": _gh_error(e)}
    if not isinstance(data, dict):
        return {"items": [], "total": 0, "error": "接口返回的不是 JSON 对象"}
    items = [_gh_item(it) for it in (data.get("items") or []) if isinstance(it, dict)]
    return {"items": items, "total": int(data.get("total_count") or 0), "error": ""}


def render_repo_rows(found: dict, query: str, sort: str = _GH_DEFAULT_SORT,
                     note: str = "") -> str:
    """把 github_repo_search 的结果渲染成工具输出。

    github_search 工具与 web_search 的自动兜底共用这一份渲染——两处口径必须一致，
    否则「同一件事在两条路上说法不同」。
    """
    items = (found or {}).get("items") or []
    if not items:
        why = (found or {}).get("error") or "没有命中任何仓库"
        return f"[实测 · API] GitHub 仓库搜索「{query}」没拿到结果：{why}"
    key = str(sort or _GH_DEFAULT_SORT).strip().lower()
    _, cn = _GH_SORTS.get(key, _GH_SORTS[_GH_DEFAULT_SORT])
    head = (f"[实测 · API] GitHub 官方搜索接口 · 查询「{query}」· 排序：{cn}"
            f" · 命中 {int((found or {}).get('total') or 0)} 个，取前 {len(items)}")
    if note:
        head += f"｜{note}"
    lines = [head + "："]
    want = _norm_key(query)
    for i, it in enumerate(items, 1):
        bits = [it["full_name"] or "(无名)", f"star={it['stars']}", f"fork={it['forks']}"]
        if it["language"]:
            bits.append(f"lang={it['language']}")
        # NOASSERTION 是 GitHub 对「有 LICENSE 文件但识别不出协议」的占位值，别当协议报出去
        if it["license"] and it["license"] != "NOASSERTION":
            bits.append(f"license={it['license']}")
        if it["pushed_at"]:
            bits.append(f"最近更新={it['pushed_at']}")
        if want and it.get("key") == want:
            bits.append("★名字同名")
        lines.append(f"  {i}. " + "  ".join(bits))
        if it["description"]:
            lines.append(f"     {it['description'][:140]}")
        if it["url"]:
            lines.append(f"     {it['url']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 搜索词纪律：泛查询由程序自动收紧（背景：同一批三跑的第一枪）
# ---------------------------------------------------------------------------
# 三跑的第一枪都是「REA 项目」这类裸词，返回的全是 GitHub 首页与教程站——白烧
# 一轮；第二枪加了 site:github.com "..." 才对。与其要求模型「先想想怎么搜」，
# 不如程序替它收紧一次：判定为泛查询就自动补一次精确检索，把结果拼在同一次
# 工具返回里。模型的主动权一点没少（它仍可自己再搜），但那一轮不再白烧。

# 出现了这些，说明模型已经在做精确检索了（站点限定 / 精确词 / 年份 / 版本号）
_QUERY_PRECISE_RX = re.compile(r"""(?:site:|"|\d{4}|[vV]?\d+(?:\.\d+)+)""")

# 搜索结果「看起来没打到目标」：前几条都是门户首页
_GENERIC_HOST_RX = re.compile(
    r"^https?://(?:www\.)?"
    r"(?:github|gitlab|bitbucket|zhihu|bilibili|csdn|juejin|cnblogs|wikipedia|baidu)\.(?:com|org|net)/?$",
    re.I,
)
# slug 只认 ASCII（rerun50：「git上的项目UI-Critic」被 \w 的 Unicode 语义粘成一坨，
# 模型拿这坨去搜必空）。CJK 一律排除在 slug 外，让「UI-Critic」单独露出来。
_SLUG_RX = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9._-]*[/-][A-Za-z][A-Za-z0-9._-]*)(?![A-Za-z0-9_.-])")
_WORD_RX = re.compile(r"[A-Za-z][A-Za-z0-9.+-]*|[\u4e00-\u9fff]+")
# 常见噪声词：查询里几乎必然出现，但从来不指向「要找的那个东西」
_QUERY_STOP = {
    "git", "github", "the", "and", "for", "how", "what", "why", "with", "http", "https",
    "www", "com", "org", "api", "json", "url", "about", "info", "news", "doc", "docs",
    "one", "new", "top", "best", "get", "make", "use", "run", "open", "source", "code",
    "app", "web", "site", "star", "fork",
}


def is_generic_query(query: str) -> bool:
    """查询是不是「泛查询」：没有站点限定 / 精确词 / 年份版本号，且词少、句子短。

    这是**辅助判据**——决定要不要收紧的主判据是「返回的结果有没有打到具体条目」
    （见 results_look_generic）。它只在需要向用户解释「为什么补搜」时说清原因。
    """
    s = str(query or "").strip()
    if not s:
        return False
    if _QUERY_PRECISE_RX.search(s):
        return False
    return len(_WORD_RX.findall(s)) <= 5 and len(s) <= 40


def results_look_generic(rows) -> bool:
    """结果是不是清一色的门户 / 站内首页（说明这枪没打到具体条目）。"""
    if not rows:
        return True
    head = list(rows)[:4]
    n = sum(1 for r in head if _GENERIC_HOST_RX.match(str((r or [""])[0] or "")))
    return n >= 2


def _key_word(w: str):
    """挑「最像项目名」的那个词：全大写缩写 > 带数字 > 长词。"""
    return (1 if (len(w) > 1 and w.isupper()) else 0,
            1 if any(ch.isdigit() for ch in w) else 0,
            len(w))


def tighten_query(query: str) -> tuple[str, str]:
    """把泛查询收紧成一条更可能一枪命中的查询，返回 (查询串, 依据)。

    优先认 owner/repo 形态；否则在全大写缩写里找（REA、GPT 这种最像现成项目名），
    再去掉 git / the 这类噪声词后取最长的英文词；最后才退到中文词做精确匹配。
    """
    s = str(query or "").strip()
    m = _SLUG_RX.search(s)
    if m:
        return f'site:github.com "{m.group(1)}"', f"看起来在找某个仓库（{m.group(1)} 是 owner/repo 形态）"
    picks = _WORD_RX.findall(s)
    en = [w for w in picks if w[:1].isascii() and w[:1].isalpha()
          and w.lower() not in _QUERY_STOP]
    if en:
        key = max(en, key=_key_word)
        return f'site:github.com "{key}"', f"以「{key}」为精确词限定在 GitHub"
    zh = [w for w in picks if w[:1] and not w[:1].isascii()]
    if zh:
        key = max(zh, key=len)
        return f'"{key}"', f"以「{key}」做精确匹配"
    return f'"{s}"', "加引号做精确匹配"


# 查询里出现这些，说明是在找 GitHub 上的东西（不然「找最大的质数」也会被拖去搜仓库）。
# 注意中文句里 git/github 常紧跟汉字（「git上的项目」「github仓库」），`\b` 在这种
# 边界上认不出——用 lookaround 让 ASCII 字母与 CJK 都算边界。
_GH_INTENT_RX = re.compile(
    r"github|\bgit\b|git(?=[\u4e00-\u9fff])|[\u4e00-\u9fff]git"
    r"|仓库|开源项目|\brepo(?:sitory)?\b", re.I)

# 「找项目」这件事本身的词，不指向任何具体仓库——拿它去搜只会拉回一堆不相干的
_GENERIC_PROJECT_WORDS = {
    "project", "projects", "repository", "repositories", "repo", "repos",
    "library", "libraries", "framework", "frameworks", "tool", "tools",
}


def repo_keyword(query: str) -> str:
    """查询像在找「某个 GitHub 仓库」时返回该搜的词，否则返回空串。

    两道判据都要过，缺一不可：

      ① **信号**：查询里得有 git / github / 仓库 / repo 这类字样。没有就说明这
         不是一次仓库检索（「找最大的质数」不能被拖去打 GitHub 接口）；
      ② **词**：还要能挑出一个像项目名的词——owner/repo 形态 > 全大写缩写 >
         带数字 > 长词。挑词规则与 tighten_query 共用，两处口径不许打架。

    只有「项目 / 仓库」这类泛词时返回空串——那不是名字，搜不出东西。
    """
    text = str(query or "").strip()
    if not text or not _GH_INTENT_RX.search(text):
        return ""
    slug = _SLUG_RX.search(text)
    if slug:
        return slug.group(1)
    picks = _WORD_RX.findall(text)
    words = [w for w in picks if w[:1].isascii() and w[:1].isalpha()
             and w.lower() not in _QUERY_STOP]
    if not words:
        return ""
    key = max(words, key=_key_word)
    return "" if key.lower() in _GENERIC_PROJECT_WORDS else key



def build_web_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars
    browser_dir = Path(cfg.workspace_root) / ".browser"

    # ---------- fetch_url ----------
    def fetch_url(url: str, max_chars: int = 6000) -> str:
        if not url.lower().startswith(("http://", "https://")):
            return "[错误] 只支持 http/https 链接"
        try:
            r = _get(url)
            r.raise_for_status()
        except requests.RequestException as e:
            return f"[错误] 请求失败: {e}\n{fetch_failure_hint(url, e)}"
        ctype = r.headers.get("content-type", "")
        raw = r.text
        m = _TITLE_RE.search(raw)
        title = html.unescape(m.group(1)).strip() if m else ""
        if "html" in ctype.lower() or raw.lstrip()[:200].lower().startswith("<!doctype"):
            body = _to_text(raw)
        else:
            body = raw
        head = f"[实测 · 网页] {url}\n[标题] {title}\n"
        return head + body[: int(max_chars)]

    # ---------- fetch_json ----------
    def _dig(obj, path: str):
        """按点分路径取字段（支持列表下标）："owner.login"、"items.0.name"。"""
        cur = obj
        for part in str(path).split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
            if cur is None:
                return None
        return cur

    def fetch_json(url: str, keys: str = "", max_chars: int = 6000) -> str:
        """抓 JSON 接口并返回真实数据（fields 投影）。

        这是「实测」路径：结果直接来自官方 API，可以直接引用。适合 GitHub
        （api.github.com/repos/owner/repo）、包仓库（registry.npmjs.org /
        pypi.org/pypi/<pkg>/json）这类结构化来源。
        """
        if not url.lower().startswith(("http://", "https://")):
            return "[错误] 只支持 http/https 链接"
        try:
            r = requests.get(url, timeout=25, headers={
                "User-Agent": "AgentForge/0.1 (local agent)",
                "Accept": "application/json, text/plain, */*",
            })
            r.raise_for_status()
        except requests.RequestException as e:
            return f"[错误] 请求失败: {e}"
        try:
            data = r.json()
        except ValueError:
            return ("[错误] 该地址返回的不是 JSON（多半是个网页）。"
                    "查网页正文请改用 fetch_url；确认是接口再检查 URL。\n"
                    f"返回内容开头：{r.text[:300]}")
        head = f"[实测 · API] {url}\n"
        picks = [k.strip() for k in (keys or "").split(",") if k.strip()]
        if picks:
            lines = []
            for k in picks[:40]:
                v = _dig(data, k)
                if v is None:
                    lines.append(f"{k} = （无此字段）")
                elif isinstance(v, (dict, list)):
                    lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)[:400]}")
                else:
                    lines.append(f"{k} = {v}")
            return (head + "\n".join(lines))[:max_out]
        return (head + json.dumps(data, ensure_ascii=False, indent=1))[:max_out]

    # ---------- web_search ----------
    _BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.S | re.I)
    _BING_LINK_RE = re.compile(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
    _BING_SNIP_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.S | re.I)
    _DDG_RESULT_RE = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</a>',
        re.S | re.I,
    )
    _HREF_RE = re.compile(r"uddg=([^&]+)")
    _BROWSER_UA = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36",
    }
    # Bing 的国内入口：www.bing.com 偶发被劫到 cn.bing / 结果里塞满门户首页
    # （rerun50 实证：`GitHub UI-Critic project` 搜回 GitHub 首页与知乎教程），
    # 两个入口轮着试，谁返回具体条目用谁。
    _BING_HOSTS = ("https://www.bing.com/search", "https://cn.bing.com/search")

    def _clean(s: str) -> str:
        return _WS_RE.sub(" ", _HTML_RE.sub("", html.unescape(s))).strip()

    def _search_bing(query: str, max_results: int) -> list[tuple[str, str, str]]:
        """Bing（国内直连可用）。返回 (url, title, snippet) 列表。

        两个入口都试：www.bing.com 与 cn.bing.com 返回的结果质量会因地区
        劫持而不同，谁解析出更多具体条目用谁。
        """
        best: list[tuple[str, str, str]] = []
        last_err: Exception | None = None
        for base in _BING_HOSTS:
            try:
                r = requests.get(
                    base,
                    params={"q": query, "count": max(4, int(max_results))},
                    timeout=20,
                    headers=_BROWSER_UA,
                )
                r.raise_for_status()
            except requests.RequestException as e:
                last_err = e
                continue
            out = []
            for block in _BING_BLOCK_RE.findall(r.text)[:max_results]:
                a = _BING_LINK_RE.search(block)
                if not a:
                    continue
                p = _BING_SNIP_RE.search(block)
                out.append((a.group(1), _clean(a.group(2)), _clean(p.group(1)) if p else ""))
            # 优先返回「打到具体条目」的那份（不是门户首页堆）
            if out and not results_look_generic(out):
                return out
            if len(out) > len(best):
                best = out
        if not best and last_err:
            raise last_err
        return best

    def _search_ddg(query: str, max_results: int) -> list[tuple[str, str, str]]:
        """DuckDuckGo HTML 端点（部分网络环境下不可用，作兜底）。"""
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=25,
            headers=_UA,
        )
        r.raise_for_status()
        out = []
        for href, title, snippet in _DDG_RESULT_RE.findall(r.text)[:max_results]:
            m = _HREF_RE.search(href)
            url = requests.utils.unquote(m.group(1)) if m else href
            out.append((url, _clean(title), _clean(snippet)))
        return out

    def _search_once(query: str, n: int):
        """跑一次搜索（Bing 优先、DDG 兜底），返回 (rows, errors)。"""
        rows: list[tuple[str, str, str]] = []
        errs: list[str] = []
        for name, fn in (("bing", _search_bing), ("duckduckgo", _search_ddg)):
            try:
                rows = fn(query, max(1, int(n)))
                if rows:
                    break
            except requests.RequestException as e:
                errs.append(f"{name}: {e}")
        return rows, errs

    def _render_rows(query: str, rows, note: str = "") -> str:
        out = [f"[搜索摘要 · 转述 · 未核实] {query} — 前 {len(rows)} 条"
               f"（摘要不可直接当事实引用）" + (f"｜{note}" if note else "") + ":"]
        for i, (url, title, snippet) in enumerate(rows, 1):
            out.append(f"{i}. {title[:80]}\n   {url}\n   {snippet[:160]}")
        return "\n".join(out)

    def github_search(query: str, sort: str = _GH_DEFAULT_SORT, limit: int = 5) -> str:
        """按名字搜 GitHub 仓库，返回官方接口给出的候选清单（默认按 star 排序）。

        程序先替模型把查询整理好（smart_repo_query），并补一枪「名字同名」
        检索——按 star 排时 0 star 的同名正主会被淹掉（rerun50 实证），
        名字同名的候选单独摆出来，并标 ★。
        """
        name = smart_repo_query(query)
        found = github_repo_search(name or query, sort=sort, limit=limit)
        sections = [render_repo_rows(found, name or query, sort)]
        exact = exact_repo_lookup(name or query)
        if exact["items"]:
            sections.insert(0, render_repo_rows(
                {"items": exact["items"], "total": len(exact["items"]), "error": ""},
                name or query, "best-match",
                note="★名字同名的项目（按名字匹配，不按 star）"))
        return "\n\n".join(sections)[:max_out]

    def web_search(query: str, max_results: int = 8) -> str:
        """联网搜索：优先 Bing（国内直连），失败回退 DuckDuckGo。

        返回的是**搜索摘要**，只用来找入口。输出头显式标明「转述·未核实」——
        实测教训：摘要里的数字被直接当结论引用过（star 说 1w+，实际 17 万）。

        程序侧兜底（都不指望模型自觉）：

        ① **仓库 / 包意图**——查询像在找某个具体项目时，顺手打官方接口：
           a. GitHub 名字同名检索（`"名" in:name`）——按 star 排的候选会把
              0 star 的同名正主淹掉，同名匹配的单独摆出来；
           b. star 排序的常规候选（「哪个更有名」）；
           c. npm 官方 registry 检索——项目可能根本不在 GitHub 生态
              （rerun50 的 UI-Critic 正主是 npm 包，GitHub 搜索查不到）。
        ② **泛查询**——结果没打到具体条目时，自动补一枪收紧后的检索
           （`site:` + 精确词）。
        """
        rows, errors = _search_once(query, max_results)
        sections: list[str] = []
        tighter = ""
        gh_key = ""
        kw = repo_keyword(query) or (smart_repo_query(query) if _GH_INTENT_RX.search(query or "") else "")
        if kw:
            # ① 名字同名：0 star 的正主靠它浮出水面（不按 star 排）
            exact = exact_repo_lookup(kw)
            if exact["items"]:
                sections.append(render_repo_rows(
                    {"items": exact["items"], "total": len(exact["items"]), "error": ""},
                    kw, "best-match",
                    note="★名字同名的项目（按名字匹配，不按 star）——「是不是它」以这份为准"))
            # ② star 排序的常规候选（「哪个更有名」）
            found = github_repo_search(kw, sort=_GH_DEFAULT_SORT, limit=5)
            # 接口抖了/限流了就当没补过——web_search 的本职是搜索，
            # 不该因为兜底失败往结果里塞一段错误（模型真要这个数据时
            # 会自己调 github_search，那里会把错误原样告诉它）。
            if found["items"]:
                gh_key = kw
                sections.append(render_repo_rows(
                    found, kw, _GH_DEFAULT_SORT,
                    note="系统侧自动补 ·「哪个更有名」以这份为准，别凭搜索摘要判断"))
            # ③ npm 官方 registry：项目可能是包不是 GitHub 热门仓库
            npm = npm_package_search(kw, limit=5)
            if npm["items"]:
                sections.append(render_npm_rows(
                    npm, kw,
                    note="系统侧自动补 · 项目可能是 npm 包（GitHub 搜不到 ≠ 不存在）"))
        # 判据是「这枪有没有打到具体条目」，不是「查询词长得像不像泛查询」——
        # 结果全是门户首页（或干脆没结果）就说没打到，程序补一枪精确的。
        # 打中了就不补（查询泛但一把命中，没什么可收紧的）。
        if results_look_generic(rows):
            tq, why = tighten_query(query)
            if tq and tq.strip().lower() != str(query).strip().lower():
                trows, _ = _search_once(tq, max(3, int(max_results) // 2))
                if trows:
                    tighter = tq
                    cause = ("查询过泛" if is_generic_query(query)
                             else "结果全是门户首页，没打到具体条目")
                    sections.append(_render_rows(tq, trows, f"系统侧自动收紧（{cause}）· 依据：{why}"))
        if rows:
            ref = "（供参考）" if (tighter or gh_key) else ""
            sections.append(_render_rows(query, rows, "原始查询" + ref))
        if not sections:
            msg = (f"[错误] 搜索失败。{'；'.join(errors)}" if errors
                   else f"[搜索] “{query}” 没有返回结果。")
            return (msg + "\n[下一步] 换更具体的词再搜一次：带上站点限定与精确词，"
                           "如 `site:github.com \"owner/repo\"`；或直接 fetch_url 你猜测的官网地址。")
        tail = ("下一步：挑 ≥1 条最可能是官方的结果，用 fetch_url / fetch_json 抓原文核实后再下结论。"
                "这里的数字、版本、日期都不可直接写进回复。")
        if gh_key:
            tail = (f"注：你要找的是 GitHub 上的项目，系统已直接查官方搜索接口"
                    f"（查询「{gh_key}」），★标记的是**名字同名**的候选——"
                    f"「是不是它」以同名为准，别拿搜索摘要里的转述判断，"
                    f"也别只看 star（新项目的 star 可能是 0）；"
                    f"选定后用它给的地址交给 fetch_json，拿 star / fork / 语言等权威字段。\n" + tail)
        if tighter:
            tail = (f"注：原查询过泛，系统已自动补搜 `{tighter}`（结果排在上面）。"
                    f"以后直接用这种写法能省一轮。\n" + tail)
        return ("\n\n".join(sections) + "\n" + tail)[:max_out]

    # ---------- 浏览器 CLI 封装 ----------
    def _run_browser_chain(chain: str, timeout: int) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                chain,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "") + (
                "\n--- stderr ---\n" + proc.stderr if proc.stderr.strip() else ""
            )
        except subprocess.TimeoutExpired:
            return -1, f"[错误] 浏览器操作超时（>{timeout}s）"
        except FileNotFoundError:
            return -1, "[错误] 未找到 agent-browser CLI。请先: npm install -g agent-browser && agent-browser install"

    def browser_snapshot(url: str, max_chars: int = 8000) -> str:
        """无头浏览器打开页面，返回可访问性文本（能拿到 JS 渲染后的内容）。"""
        if not url.lower().startswith(("http://", "https://")):
            return "[错误] 只支持 http/https 链接"
        code, out = _run_browser_chain(
            f'agent-browser open "{url}" && agent-browser wait --load load && agent-browser snapshot && agent-browser close',
            timeout=90,
        )
        if code != 0:
            return f"[错误] 浏览器快照失败(exit {code}):\n{out[:1500]}"
        text = out.strip()
        return f"[browser_snapshot] {url}\n" + text[: int(max_chars)]

    def browser_screenshot(url: str, filename: str = "") -> str:
        """无头浏览器对页面截图，保存到工作区 .browser/ 下，返回文件路径。"""
        if not url.lower().startswith(("http://", "https://")):
            return "[错误] 只支持 http/https 链接"
        name = re.sub(r"[^\w.-]+", "_", (filename or "screenshot").strip()) or "screenshot"
        if not name.lower().endswith(".png"):
            name += ".png"
        browser_dir.mkdir(parents=True, exist_ok=True)
        dest = browser_dir / name
        dest = Path(guard.resolve(str(dest)))
        code, out = _run_browser_chain(
            f'agent-browser open "{url}" && agent-browser wait --load load'
            f' && agent-browser screenshot "{dest}" && agent-browser close',
            timeout=90,
        )
        if code != 0 or not dest.exists():
            return f"[错误] 截图失败(exit {code}):\n{out[:1500]}"
        size = dest.stat().st_size
        return f"已截图: {dest} ({size} 字节)。这是图片文件，把该路径告知用户即可查看。"

    def open_browser(url: str) -> str:
        """在用户本机的默认浏览器里打开网页（前台可见窗口）。"""
        if not url.lower().startswith(("http://", "https://")):
            return "[错误] 只支持 http/https 链接"
        try:
            ok = webbrowser.open(url, new=2)
            if not ok:
                # Windows 上注册表浏览器信息缺失时，尝试常见浏览器兜底
                for exe in ("msedge", "chrome"):
                    try:
                        subprocess.Popen(["cmd", "/c", "start", url], shell=False)
                        ok = True
                        break
                    except OSError:
                        continue
        except Exception as e:  # noqa: BLE001
            return f"[错误] 打开浏览器失败: {e}"
        return f"已在你的默认浏览器打开: {url}" if ok else "[错误] 系统未返回成功状态"

    # ---------- 工具注册 ----------
    return [
        make_tool(
            "web_search",
            "联网搜索（Bing 双入口，失败回退 DuckDuckGo）。**只用来找入口**：返回的是搜索摘要"
            "（转述），其中的数字 / 版本 / 日期一律不可直接引用。硬性要求：搜到候选 URL 后，"
            "必须用 fetch_url 或 fetch_json 抓至少一个一手来源核实，然后才下结论。"
            "目标是某个具体项目 / 库 / 产品时，系统会自动补官方接口检索"
            "（GitHub ★名字同名候选 + star 候选 + npm registry）——这些是实测数据，"
            "优先看它们，而不是第三方博客的转述。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "description": "返回条数，默认 8"},
                },
                "required": ["query"],
            },
            web_search,
            SAFE,
        ),
        make_tool(
            "github_search",
            "按名字搜 GitHub 仓库（官方接口实测）：一次调用返回候选清单——★名字同名的"
            "项目（按名字匹配、不按 star，新项目 star 可能是 0）+ 按 star 排序的"
            "「哪个更有名」候选，含 star / fork 数、主语言、许可证、最近更新、简介与地址。"
            "想知道某个项目是哪个、它有多少 star，就用它；「是不是它」以★同名候选为准，"
            "别拿搜索摘要猜。GitHub 搜不到时换 npm_search（项目可能是包）。"
            "已知仓库全名时，直接用 fetch_json 打 api.github.com/repos/<owner>/<repo>。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "仓库关键词，如 REA、UI-Critic、openai/whisper"},
                    "sort": {"type": "string", "enum": ["stars", "forks", "updated", "best-match"],
                             "description": "排序口径，默认 stars（最出名的排前面）"},
                    "limit": {"type": "integer", "description": "返回几个候选，默认 5，最多 20"},
                },
                "required": ["query"],
            },
            github_search,
            SAFE,
        ),
        make_tool(
            "npm_search",
            "按名字搜 npm 包（官方 registry 实测）：返回包名、版本、发布时间、简介、"
            "包页与源码仓库地址。GitHub 搜不到某个名字的项目时用它——很多前端工具"
            "（lint / 插件 / CLI）只以 npm 包形式发布，GitHub 搜索接口查不到。"
            "已知包名时直接用 fetch_json 打 https://registry.npmjs.org/<包名>。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "包名关键词，如 ui-critic"},
                    "limit": {"type": "integer", "description": "返回几个候选，默认 5，最多 20"},
                },
                "required": ["query"],
            },
            lambda query, limit=5: render_npm_rows(
                npm_package_search(query, limit=limit), query)[:max_out],
            SAFE,
        ),
        make_tool(
            "fetch_url",
            "抓取网页正文并转成纯文本——这是**实测**路径，抓到的内容可以直接引用。"
            "查官方文档、官网、核实事实用这个；JS 渲染的页面改用 browser_snapshot；"
            "JSON 接口改用 fetch_json。",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 URL"},
                    "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 6000"},
                },
                "required": ["url"],
            },
            fetch_url,
            SAFE,
        ),
        make_tool(
            "fetch_json",
            "抓取 JSON 接口并返回真实数据——**实测**路径，结果可直接引用。"
            "核对数字与结构化事实用它，一次调用就能拿到官方 API 的权威字段。"
            "常用：GitHub 仓库 https://api.github.com/repos/<owner>/<repo>"
            "（keys 可填 stargazers_count,forks_count,open_issues_count,pushed_at,language）；"
            "npm 包 https://registry.npmjs.org/<pkg>；PyPI 包 https://pypi.org/pypi/<pkg>/json。"
            "keys 用逗号分隔的点分路径投影字段，省略则返回完整 JSON。",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "接口完整 URL（返回 JSON）"},
                    "keys": {"type": "string",
                             "description": "要取的字段，逗号分隔的点分路径，如 stargazers_count,pushed_at；省略返回全部"},
                    "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 6000"},
                },
                "required": ["url"],
            },
            fetch_json,
            SAFE,
        ),
        make_tool(
            "browser_snapshot",
            "用无头浏览器打开网页并读取渲染后的页面文本（可访问性树）。适用于依赖 JS 的现代网页、SPA。",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 URL"},
                    "max_chars": {"type": "integer", "description": "最多返回多少字符，默认 8000"},
                },
                "required": ["url"],
            },
            browser_snapshot,
            SAFE,
        ),
        make_tool(
            "browser_screenshot",
            "用无头浏览器对网页截图，保存到工作区 .browser/ 目录并返回文件路径。用于界面验证、视觉确认。",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 URL"},
                    "filename": {"type": "string", "description": "截图文件名（不含目录），默认 screenshot.png"},
                },
                "required": ["url"],
            },
            browser_screenshot,
            SAFE,
        ),
        make_tool(
            "open_browser",
            "在用户本机的默认浏览器中打开网页（会弹出可见窗口给用户看）。需要向用户展示页面时用这个。",
            {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整 URL"}},
                "required": ["url"],
            },
            open_browser,
            SAFE,
        ),
    ]
