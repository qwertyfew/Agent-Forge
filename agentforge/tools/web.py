"""网络与浏览器工具族。

对照成熟 Agent 产品（Codex）的能力集：
- web_search        联网搜索（查资料第一步）
- fetch_url         抓取网页正文（静态页面 / 文档）
- browser_snapshot  无头浏览器读取 JS 渲染页面的可访问文本
- browser_screenshot 无头浏览器截图（用于界面验证 / 视觉确认）
- open_browser      在用户本机默认浏览器中打开网页（给用户看）
"""

from __future__ import annotations

import html
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
        head = f"[URL] {url}\n[标题] {title}\n"
        return head + body[: int(max_chars)]

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

    def web_search(query: str, max_results: int = 6) -> str:
        """联网搜索：优先 Bing（国内直连），失败回退 DuckDuckGo。"""
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
        lines = [f"[搜索] {query} — 前 {len(rows)} 条结果:"]
        for i, (url, title, snippet) in enumerate(rows, 1):
            lines.append(f"{i}. {title[:80]}\n   {url}\n   {snippet[:160]}")
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

    # ---------- fetch_url ----------
    return [
        make_tool(
            "web_search",
            "联网搜索（DuckDuckGo，免 key）。查资料、找文档、核对事实的第一步；拿到结果后用 fetch_url 或 browser_snapshot 读详情。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "description": "返回条数，默认 6"},
                },
                "required": ["query"],
            },
            web_search,
            SAFE,
        ),
        make_tool(
            "fetch_url",
            "抓取网页并转成纯文本，用于查文档、核对信息。静态页面用这个；JS 渲染的页面请用 browser_snapshot。",
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
