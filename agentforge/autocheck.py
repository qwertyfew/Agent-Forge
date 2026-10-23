"""核查打满后的「程序补查」一轮（用户 2026-10-21 定的规则）。

背景：grounding 闸门只打回 2 轮，第 3 轮起就由程序在交付物尾部盖一句「未经核实」。
当年这么定是为了省云端 token；本地 agent 只花电费，把「核实」这件事原样推给用户
说不过去——**能抓的源，程序自己抓**。

所以打满之后不再是「盖章了事」，而是：

    1. 程序按诊断报告决定去抓哪个一手源（本模块的 plan，纯确定性）；
    2. 抓回来后用「模型看到的源 + 程序抓到的源」重新诊断：
       · 能对上 → 核查通过，正常交付（不盖章）；
       · 对不上 → 把这些一手源交给模型做**最后一轮**对齐（render_prompt）；
       · 一个源都没抓到 → 才走声明，并且声明里写清「程序抓过、没抓到」。
    3. 最后一轮之后再诊断，仍不合格才盖章。

边界：本模块不判断「答复对不对」，只负责「抓什么、抓到的是什么」。判定仍归
grounding.diagnose，避免出现第二套判据。
"""
from __future__ import annotations

import re

from . import grounding

# 诊断里「缺材料」的根因：出现这些，说明问题在源上，补抓才有意义。
_NEED_SOURCE = {
    "zero_retrieval",     # 一次都没抓
    "retrieval_miss",     # 检索没命中任务要的项目
    "retrieval_generic",  # 检索落到通用词上
    "meta_source",        # 抓的是入口/总览仓库，本身没有用法
    "ungrounded_command", # 命令在已抓的源里找不到
}

_PER_SRC_CHARS = 3000     # 单个源进「最后一轮」提示的预算（本地模型上下文有限）
_TOTAL_CHARS = 6000
_GITHUB = "https://github.com/{slug}"
_BAD_RX = re.compile(r"^\s*\[错误\]")


# ---------------------------------------------------------------- 抓什么
def plan(diag: dict, *, task: str = "", reply: str = "", history=None,
         limit: int = 3) -> list[dict]:
    """决定程序该补抓哪些源。返回 [{kind, target, why}]，kind ∈ readme/url/search。

    判断全部是确定性的：
      · 答复自己引用的地址 → 抓它（一次请求就能把「没来路」变成「一手源」或证伪）；
      · 诊断给出的候选仓库 → 抓 README（诊断层已按匹配度挑过）；
      · 一个候选都没有 → 用任务关键词做一次程序侧检索，命中的仓库再抓 README。
    """
    if not diag or diag.get("ok"):
        return []
    limit = max(1, int(limit))
    kinds = {f.get("kind") for f in (diag.get("findings") or [])}
    refs = list(diag.get("refs") or [])
    repos = list((diag.get("retrieval") or {}).get("repos") or [])
    seen_urls = set(grounding.fetched_pages(history or []))

    out: list[dict] = []

    def _seen(target: str) -> bool:
        return any(it["target"].lower() == target.lower() for it in out)

    def _add(kind: str, target: str, why: str) -> None:
        target = (target or "").strip()
        if not target or len(out) >= limit or _seen(target):
            return
        # 模型自己已经抓过这个源，不重复烧一次
        if any(target.lower() in u.lower() for u in seen_urls):
            return
        out.append({"kind": kind, "target": target, "why": why})

    need_source = bool(kinds & _NEED_SOURCE)
    # 顺序就是优先级：缺材料的场次先抓「任务要的那个项目」，其余先抓答复自报的引用。
    first, second = (repos, refs) if need_source else (refs, repos)
    for r in first:
        _add_ref(_add, r)
    for r in second:
        _add_ref(_add, r)

    if not out:
        kw = str((diag.get("retrieval") or {}).get("task_keyword") or "").strip()
        if kw:
            _add("search", kw, "本轮没有任何可用候选，程序按任务关键词重搜一次")
    return out


def _add_ref(add, ref: str) -> None:
    """把一条引用（URL 或 owner/repo）转成补抓计划项。"""
    s = (ref or "").strip().strip("`\"'<>()[]{}，。、；")
    if not s:
        return
    if s.lower().startswith(("http://", "https://")):
        slug = slug_of(s)
        if slug:
            add("readme", slug, "答复引用了这个仓库，程序直接抓 README 复核")
        else:
            add("url", s, "答复引用了这个地址，程序直接抓正文复核")
        return
    slug = slug_of(s)
    if slug:
        add("readme", slug, "答复引用了这个仓库，程序直接抓 README 复核")


def slug_of(ref: str) -> str:
    """把 `owner/repo`、github 链接、`@scope/name` 归一成 `owner/repo`；不是仓库返回 ""。"""
    s = (ref or "").strip().strip("`\"'<>()[]{}，。、；")
    m = re.search(r"github\.com/([\w.-]+)/([\w.-]+)", s, re.I)
    from_url = bool(m)
    if m:
        owner, name = m.group(1), m.group(2)
    else:
        if s.lower().startswith(("http://", "https://")) or "/" not in s:
            return ""
        owner, _, name = s.lstrip("@").partition("/")
        name = name.split("#")[0].split("?")[0].strip("/")
    if name.endswith(".git"):
        name = name[:-4]
    if not owner or not name:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*", owner):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*", name):
        return ""
    # 来自 GitHub 地址的：带点的仓库名合法（next.js、marp-cli），只查标识符形状；
    # 光秃秃的 `owner/name`：按候选池口径过滤，避免把 `src/components`、`HTML/JS`
    # 这种路径/并列写法当成仓库去抓。
    if not from_url and not grounding._is_pool_repo(owner, name):
        return ""
    return f"{owner}/{name}"


# ---------------------------------------------------------------- 抓
def run(items: list[dict], call, *, limit: int = 3, max_chars: int = 6000) -> list[dict]:
    """执行补抓计划。`call(name, **kwargs) -> str` 是工具入口（注入便于测试）。

    返回 [{kind, target, url, text, ok, pool?}]；ok=False 表示这次抓失败（网络错误、
    404…），失败也算一次尝试，用来在声明里写清「抓过没抓到」。
    """
    limit = max(1, int(limit))
    out: list[dict] = []
    for it in items or []:
        if len(out) >= limit:
            break
        kind = it.get("kind")
        target = it.get("target") or ""
        if kind == "search":
            txt = _safe(call, "github_search", query=target, limit=3)
            ok = bool(txt) and not _BAD_RX.match(txt) and len(txt) >= 40
            out.append({"kind": "search", "target": target, "url": "",
                        "text": txt, "ok": ok, "pool": txt if ok else ""})
            if not ok:
                continue
            for slug in grounding.pool_repos(txt, limit=limit)[: max(1, limit - len(out))]:
                _fetch_into(out, call, "readme", slug, max_chars)
                if len(out) >= limit:
                    break
            continue
        _fetch_into(out, call, kind or "readme", target, max_chars)
    return out


def _fetch_into(out: list[dict], call, kind: str, target: str, max_chars: int) -> None:
    url = _GITHUB.format(slug=target) if kind == "readme" else target
    txt = _safe(call, "fetch_url", url=url, max_chars=max_chars)
    ok = bool(txt) and not _BAD_RX.match(txt) and len(txt) >= 80
    out.append({"kind": kind, "target": target, "url": url, "text": txt, "ok": ok})


def _safe(call, name: str, **kwargs) -> str:
    try:
        return str(call(name, **kwargs) or "")
    except Exception as e:  # noqa: BLE001  网络/工具侧任何异常都不该把收尾搞崩
        return f"[错误] {name} 调用失败: {e}"


# ---------------------------------------------------------------- 抓回来的怎么用
def sources(results: list[dict]) -> str:
    """抓到的一手源正文（喂给 diagnose 的 sources 语料）。"""
    return "\n".join(r["text"] for r in results
                     if r.get("ok") and r.get("text") and r.get("kind") != "search")


def pool(results: list[dict]) -> str:
    """程序侧检索的候选池（喂给 diagnose 的 candidates，用于证据准入）。"""
    return "\n".join(r.get("pool") or "" for r in results if r.get("pool"))


def urls(results: list[dict]) -> list[str]:
    """真正抓到的源地址（用于声明与提示）。"""
    return [r["url"] for r in results if r.get("ok") and r.get("url")]


def failures(results: list[dict]) -> list[dict]:
    return [r for r in results if not r.get("ok")]


def render_prompt(results: list[dict]) -> str:
    """交给模型的「最后一轮」提示：附上程序抓到的一手源，只做对齐，不许再搜。"""
    srcs = [r for r in results if r.get("ok") and r.get("text")
            and r.get("kind") != "search"]
    if not srcs:
        return ""
    lines = [
        "【程序补查 · 最后一轮】",
        "你的答复两轮未通过核查。程序已经替你抓到了下面这些一手源（你此前的答复"
        "没有依据它们）。从这一步起只做一件事：拿这份材料逐条核对答复里的命令 / "
        "目录 / 引用。",
        "",
        "· 材料里确有对应写法 → 改成材料里的写法（路径、参数一字不差）；",
        "· 材料里没有 → 删掉，或如实写成「源里未提及」；",
        "· 不要再发起新的检索，不要重写与这些条目无关的内容。",
        "",
        "（这是最后一轮：改完直接给最终答复。若仍对不上，程序会在交付物上标注"
        "哪些条目没有依据。）",
    ]
    left = _TOTAL_CHARS
    for i, r in enumerate(srcs, 1):
        if left <= 0:
            break
        body = r["text"][: min(_PER_SRC_CHARS, left)]
        left -= len(body)
        lines.append("")
        lines.append(f"===== 源 {i} · {r['url']} =====")
        lines.append(body)
    return "\n".join(lines)


__all__ = ["plan", "run", "sources", "pool", "urls", "failures", "render_prompt",
           "slug_of"]
