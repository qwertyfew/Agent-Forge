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
            return f"[错误] 请求失败: {e}\n提示: 若页面依赖 JS 渲染，请改用 browser_snapshot。"
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

    def web_search(query: str, max_results: int = 8) -> str:
        """联网搜索：优先 Bing（国内直连），失败回退 DuckDuckGo。

        返回的是**搜索摘要**，只用来找入口。输出头显式标明「转述·未核实」——
        实测教训：摘要里的数字被直接当结论引用过（star 说 1w+，实际 17 万）。
        """
        rows: list[tuple[str, str, str]] = []
        errors = []
        for name, fn in (("bing", _search_bing), ("duckduckgo", _search_ddg)):
            try:
                rows = fn(query, max(1, int(max_results)))
                if rows:
                    break
            except requests.RequestException as e:
                errors.append(f"{name}: {e}")
        if not rows:
            msg = f"[错误] 搜索失败。{'；'.join(errors)}" if errors else f"[搜索] “{query}” 没有返回结果。"
            return msg + "\n建议：直接 fetch_url 你猜测的官网地址。"
        lines = [f"[搜索摘要 · 转述 · 未核实] {query} — 前 {len(rows)} 条（摘要不可直接当事实引用）:"]
        for i, (url, title, snippet) in enumerate(rows, 1):
            lines.append(f"{i}. {title[:80]}\n   {url}\n   {snippet[:160]}")
        lines.append("下一步：挑 ≥1 条最可能是官方的结果，用 fetch_url / fetch_json 抓原文核实后再下结论。"
                     "这里的数字、版本、日期都不可直接写进回复。")
        return "\n".join(lines)[:max_out]

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
