"""网络与浏览器工具族。

对照成熟 Agent 产品（Codex）的能力集：
- web_search        联网搜索（查资料第一步；**只用来找入口**，返回的是转述）
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
"""

from __future__ import annotations

import html
import json
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
_SLUG_RX = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z][\w.]*[/-][A-Za-z][\w.]*)(?![A-Za-z0-9_.-])")
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

    def _clean(s: str) -> str:
        return _WS_RE.sub(" ", _HTML_RE.sub("", html.unescape(s))).strip()

    def _search_bing(query: str, max_results: int) -> list[tuple[str, str, str]]:
        """Bing（国内直连可用）。返回 (url, title, snippet) 列表。"""
        r = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "count": max(4, int(max_results))},
            timeout=20,
            headers=_BROWSER_UA,
        )
        r.raise_for_status()
        out = []
        for block in _BING_BLOCK_RE.findall(r.text)[:max_results]:
            a = _BING_LINK_RE.search(block)
            if not a:
                continue
            p = _BING_SNIP_RE.search(block)
            out.append((a.group(1), _clean(a.group(2)), _clean(p.group(1)) if p else ""))
        return out

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

    def web_search(query: str, max_results: int = 8) -> str:
        """联网搜索：优先 Bing（国内直连），失败回退 DuckDuckGo。

        返回的是**搜索摘要**，只用来找入口。输出头显式标明「转述·未核实」——
        实测教训：摘要里的数字被直接当结论引用过（star 说 1w+，实际 17 万）。

        泛查询会由程序自动补一次收紧后的检索（`site:` + 精确词），结果排在最前——
        让「第一枪太泛」不再白烧一轮。是否收紧是程序判的，不指望模型自觉。
        """
        rows, errors = _search_once(query, max_results)
        sections: list[str] = []
        tighter = ""
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
            sections.append(_render_rows(query, rows, "原始查询" + ("（供参考）" if tighter else "")))
        if not sections:
            msg = (f"[错误] 搜索失败。{'；'.join(errors)}" if errors
                   else f"[搜索] “{query}” 没有返回结果。")
            return (msg + "\n[下一步] 换更具体的词再搜一次：带上站点限定与精确词，"
                           "如 `site:github.com \"owner/repo\"`；或直接 fetch_url 你猜测的官网地址。")
        tail = ("下一步：挑 ≥1 条最可能是官方的结果，用 fetch_url / fetch_json 抓原文核实后再下结论。"
                "这里的数字、版本、日期都不可直接写进回复。")
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
            "联网搜索（Bing，失败回退 DuckDuckGo）。**只用来找入口**：返回的是搜索摘要"
            "（转述），其中的数字 / 版本 / 日期一律不可直接引用。硬性要求：搜到候选 URL 后，"
            "必须用 fetch_url 或 fetch_json 抓至少一个一手来源核实，然后才下结论。"
            "目标是某个具体项目 / 库 / 产品时，优先找它的官方地址（官网、GitHub、官方文档），"
            "而不是第三方博客。",
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
