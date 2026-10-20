"""网络工具族的自测：失败降级指路 + 泛查询自动收紧 + 按名字找 GitHub 项目。

运行：python tools/test_web_tools.py
全部断言内建，退出码 0 = 全过。**不联网**——requests.get 全程用替身。

用例取自同一批真实事故（REA 检索三跑 + 四跑）的三个缺口：

  ① 失败后没有一等公民的降级路径。fetch_url 被代理拒之后，模型手里明明有
     fetch_json 却没用，转而去猜了个 browser（白烧两轮）——而「GitHub 仓库打
     api.github.com/repos/o/r」这条正解就印在它刚读过的工具说明里。
     问题不在能力，在**失败那一刻没人把这条和眼前的错误连起来**。

  ② 搜索词不设防。三跑的第一枪都是泛查询，返回的全是 GitHub 首页与教程站
     （白烧一轮）；第二枪加了 `site:github.com "..."` 才对。

  ③ 找对了入口、找错了对象。四跑问「git 上叫 REA 的项目」，模型连搜四枪，
     最后抓的是 `api.github.com/repos/bbylw/rea-cn`——一个 80 star 的中文镜像站，
     而正主 `morluto/rea` 有 19 万 star。搜索结果里压根没有正主，模型只能锚定
     它看到的第一个 GitHub 链接。**「有哪些同名候选、各自多少 star」搜索摘要
     给不出来**，得由程序去查官方搜索接口，把候选摆到台面上。

三件事都不该靠模型临场想起来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests as R                                            # noqa: E402
from agentforge.tools import web as W                           # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------------------
# 替身：一个不联网的 requests.get
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, text: str, status: int = 200, ctype: str = "text/html"):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise R.HTTPError(f"{self.status_code}", response=SimpleNamespace(
                status_code=self.status_code))

    def json(self):
        import json as _j
        return _j.loads(self.text)


def bing_html(items) -> str:
    """把 (url, title, snippet) 列表拼成 bing 搜索结果页的形状。"""
    body = "".join(
        f'<li class="b_algo"><h2><a href="{u}">{t}</a></h2><p>{s}</p></li>'
        for u, t, s in items
    )
    return "<html><body>" + body + "</body></html>"


def make_tools():
    cfg = SimpleNamespace(max_tool_output_chars=20000, workspace_root=".")
    guard = SimpleNamespace(resolve=lambda p: p)
    return {t.name: t for t in W.build_web_tools(guard, cfg)}


def with_fake_get(table):
    """把 requests.get 换成 table(query) → FakeResp，并记录每次查询串。

    返回 (calls, restore)：calls 是发出的查询串列表，restore 用来收尾还原。
    """
    calls: list[str] = []

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        q = str((params or {}).get("q") or url)
        calls.append(q)
        return table(q)

    orig = W.requests.get
    W.requests.get = fake_get
    return calls, (lambda: setattr(W.requests, "get", orig))


# ---------------------------------------------------------------------------
# 缺口 ①：失败时的降级指路
# ---------------------------------------------------------------------------
def test_failure_hint() -> None:
    check("GitHub 仓库页 → 官方 API 地址",
          W.api_equivalent("https://github.com/lm-sys/REA")[0]
          == "https://api.github.com/repos/lm-sys/REA")
    check("npm 包页 → registry 地址",
          W.api_equivalent("https://www.npmjs.com/package/react")[0]
          == "https://registry.npmjs.org/react")
    check("PyPI 包页 → JSON 接口地址",
          W.api_equivalent("https://pypi.org/project/requests/")[0]
          == "https://pypi.org/pypi/requests/json")
    check("认不出的站点不乱编地址", W.api_equivalent("https://example.com/a/b") == ("", ""))
    check("仓库页可带查询串与锚点（不该误判成 repo 名的一部分）",
          W.api_equivalent("https://github.com/psf/requests?tab=readme")[0]
          == "https://api.github.com/repos/psf/requests")

    # 网络层失败（REA 那次的实际情形：代理拒绝）
    hint = W.fetch_failure_hint("https://github.com/lm-sys/REA", R.ConnectionError("proxy refused"))
    check("网络层失败：点名这是网络层", "网络层失败" in hint)
    check("网络层失败：给出可直接照抄的 fetch_json 调用",
          'fetch_json(url="https://api.github.com/repos/lm-sys/REA")' in hint, hint)
    check("网络层失败：说明为什么接口更稳（不经页面渲染）", "渲染" in hint)
    check("browser_snapshot 降为最后退路（不再当第一建议）",
          hint.index("fetch_json") < hint.index("browser_snapshot"))

    err404 = R.HTTPError("404", response=SimpleNamespace(status_code=404))
    hint404 = W.fetch_failure_hint("https://example.com/x", err404)
    check("4xx：先建议确认地址", "确认正确地址" in hint404, hint404)
    check("4xx：不乱说是网络层", "网络层失败" not in hint404)

    err500 = R.HTTPError("500", response=SimpleNamespace(status_code=500))
    check("5xx：说明是站点自身故障", "站点自身故障" in W.fetch_failure_hint("https://e.com/x", err500))


def test_fetch_url_wires_hint() -> None:
    """fetch_url 真的把指路拼进输出（不是只有个没人调的函数）。"""
    tools = make_tools()

    def boom(*a, **k):
        raise R.exceptions.ProxyError("blocked by proxy")

    orig = W.requests.get
    W.requests.get = boom
    try:
        out = tools["fetch_url"].run(url="https://github.com/lm-sys/REA")
    finally:
        W.requests.get = orig
    check("fetch_url 失败输出带 [下一步]", "[下一步]" in out, out)
    check("fetch_url 失败输出指向 fetch_json 与具体地址",
          "fetch_json" in out and "api.github.com/repos/lm-sys/REA" in out, out)


# ---------------------------------------------------------------------------
# 缺口 ②：泛查询判定与收紧
# ---------------------------------------------------------------------------
def test_query_judgement() -> None:
    check("裸词短查询算泛（REA 项目）", W.is_generic_query("REA 项目"))
    check("带 site: 不算泛", not W.is_generic_query('site:github.com "REA"'))
    check("带引号不算泛", not W.is_generic_query('"Reverse Engineer Anything"'))
    check("带年份不算泛", not W.is_generic_query("2026 年 AI 编程工具盘点"))
    check("带版本号不算泛", not W.is_generic_query("Python 3.13 新特性"))
    check("空查询不算泛（不该凭空补一枪）", not W.is_generic_query(""))

    check("结果全是门户首页 → 判定没打中",
          W.results_look_generic([("https://github.com/", "GitHub", ""),
                                  ("https://zhihu.com/", "知乎", "")]))
    check("结果里有具体条目 → 判定打中了",
          not W.results_look_generic([("https://github.com/lm-sys/REA", "REA", ""),
                                      ("https://github.com/lm-sys/REA/issues", "Issues", "")]))
    check("没有结果也算没打中（空表要能安全处理）", W.results_look_generic([]))


def test_tighten_query() -> None:
    q, why = W.tighten_query("看看 openai/whisper 这个仓库")
    check("owner/repo 形态 → 站点限定 + 引号",
          q == 'site:github.com "openai/whisper"', q)
    check("并说明依据", "仓库" in why, why)

    q2, _ = W.tighten_query("git 上那个 REA 项目")
    check("噪声词 git 不会被当成要找的东西（全大写缩写优先）",
          q2 == 'site:github.com "REA"', q2)

    q3, _ = W.tighten_query("帮我找找那个很出名的开源项目")
    check("没有英文词就退到中文精确匹配", q3.startswith('"') and q3.endswith('"'), q3)
    check("空查询也给出一个合法查询串", W.tighten_query("")[0] == '""')


# ---------------------------------------------------------------------------
# 编排：泛查询 → 自动补一枪，结果拼在同一次返回里
# ---------------------------------------------------------------------------
def test_search_orchestration() -> None:
    tools = make_tools()
    search = tools["web_search"]

    def generic_table(q: str):
        if "site:github.com" in q:
            return FakeResp(bing_html([
                ("https://github.com/lm-sys/REA", "lm-sys/REA: Reverse Engineer Anything",
                 "官方仓库，把逆向工程流程交给 agent")]))
        return FakeResp(bing_html([
            ("https://github.com/", "GitHub", "全球最大的代码托管平台"),
            ("https://zhihu.com/", "知乎", "有问题就会有答案")]))

    calls, restore = with_fake_get(generic_table)
    try:
        out = search.run(query="git 上那个 REA 项目")
    finally:
        restore()

    check("泛查询会自动补一枪精确的", "系统侧自动收紧" in out, out[:300])
    check("补的那一枪带站点限定与精确词", 'site:github.com "REA"' in out, out[:300])
    check("收紧后的结果排在前面（先看到有用的）",
          out.index("系统侧自动收紧") < out.index("原始查询"), out[:300])
    check("原始结果也没有被吞掉", "GitHub" in out)
    check("两次请求都真的发出去了", len(calls) >= 2, repr(calls))
    check("注明「以后这么写能省一轮」（教一次，不是每次兜）", "省一轮" in out, out[:400])
    check("仍然提醒必须抓一手来源核实", "fetch_json" in out and "不可直接写进回复" in out)

    # 一把命中就不补：查询泛但结果打到了具体条目
    def hit_table(q: str):
        return FakeResp(bing_html([
            ("https://github.com/lm-sys/REA", "lm-sys/REA", "官方仓库")]))

    calls2, restore2 = with_fake_get(hit_table)
    try:
        out2 = search.run(query="REA 项目")
    finally:
        restore2()
    check("结果打中了就不补搜", "系统侧自动收紧" not in out2, out2[:200])
    check("这种情况只请求一次", len(calls2) == 1, repr(calls2))

    # 一次都没有结果：给出可执行的下一步，而不是一句干巴巴的失败
    def empty_table(q: str):
        return FakeResp("<html><body></body></html>")

    _, restore3 = with_fake_get(empty_table)
    try:
        out3 = search.run(query="REA 项目")
    finally:
        restore3()
    check("没结果时给可执行的下一步（带 site: 的写法）",
          "[下一步]" in out3 and "site:github.com" in out3, out3[:300])


# ---------------------------------------------------------------------------
# 缺口 ③：按名字找 GitHub 项目（REA 检索四跑）
# ---------------------------------------------------------------------------
_GH_ITEMS = [
    {"full_name": "react/react", "html_url": "https://github.com/react/react",
     "stargazers_count": 251004, "forks_count": 0, "language": "JavaScript",
     "license": {"spdx_id": "MIT"}, "pushed_at": "2026-10-01T00:00:00Z",
     "description": "The library for web and native user interfaces."},
    {"full_name": "morluto/rea", "html_url": "https://github.com/morluto/rea",
     "stargazers_count": 193790, "forks_count": 47212, "language": "TypeScript",
     "license": {"spdx_id": "MIT"}, "pushed_at": "2026-10-13T14:59:23Z",
     "description": "Reverse engineer anything with agents."},
]


class GhResp(FakeResp):
    """GitHub 搜索接口的响应（JSON）。"""

    def __init__(self, payload, status: int = 200):
        super().__init__(json.dumps(payload), status=status, ctype="application/json")


def with_router(router):
    """按 URL 分派的 requests.get 替身（区别于只认 q 的 with_fake_get）。

    返回 (calls, restore)，calls 是 [(url, params), ...]。
    """
    calls: list[tuple[str, dict]] = []

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        calls.append((url, dict(params or {})))
        return router(url, params or {})

    orig = W.requests.get
    W.requests.get = fake_get
    return calls, (lambda: setattr(W.requests, "get", orig))


def test_github_search_api() -> None:
    calls, restore = with_router(
        lambda u, p: GhResp({"total_count": 6255050, "items": _GH_ITEMS}))
    try:
        got = W.github_repo_search("REA", sort="stars", limit=5)
    finally:
        restore()

    url, params = calls[0]
    check("打的是 GitHub 官方搜索接口",
          url == "https://api.github.com/search/repositories", url)
    check("带上查询词", params.get("q") == "REA", repr(params))
    check("默认按 star 倒序",
          params.get("sort") == "stars" and params.get("order") == "desc", repr(params))
    check("取几条由 limit 决定（不把整页拉回来）", params.get("per_page") == 5, repr(params))

    check("解析出候选仓库名",
          [i["full_name"] for i in got["items"]] == ["react/react", "morluto/rea"],
          repr(got["items"]))
    r = got["items"][1]
    check("解析出 star / fork", (r["stars"], r["forks"]) == (193790, 47212), repr(r))
    check("解析出语言与协议", (r["language"], r["license"]) == ("TypeScript", "MIT"), repr(r))
    check("最近更新只留日期", r["pushed_at"] == "2026-10-13", r["pushed_at"])
    check("带出可点的仓库地址", r["url"] == "https://github.com/morluto/rea", r["url"])
    check("回传命中总数", got["total"] == 6255050, str(got["total"]))
    check("成功时 error 为空", got["error"] == "", got["error"])

    # 空查询不该白打一次接口
    calls2, restore2 = with_router(lambda u, p: GhResp({"total_count": 0, "items": []}))
    try:
        empty = W.github_repo_search("   ")
    finally:
        restore2()
    check("空查询直接返回错误、不打接口",
          empty["items"] == [] and bool(empty["error"]) and not calls2, repr(empty))

    # 限流（未鉴权每小时 60 次）：要给出能照办的下一步，而不是抛栈
    _, restore3 = with_router(
        lambda u, p: GhResp({"message": "API rate limit exceeded"}, status=403))
    try:
        bad = W.github_repo_search("REA")
    finally:
        restore3()
    check("限流（403）不抛异常、转成 error", bad["items"] == [] and bool(bad["error"]), repr(bad))
    check("限流时说明怎么提高额度", "GITHUB_TOKEN" in bad["error"], bad["error"])

    # 网络层异常同样吞掉（工具不能因为一次抖动抛栈）
    def boom(url, params):
        raise R.ConnectionError("proxy refused")

    _, restore4 = with_router(boom)
    try:
        down = W.github_repo_search("REA")
    finally:
        restore4()
    check("网络层失败也转成 error",
          down["items"] == [] and "ConnectionError" in down["error"], down["error"])


def test_github_render() -> None:
    found = {"total": 6255050, "error": "",
             "items": [{"full_name": "morluto/rea", "url": "https://github.com/morluto/rea",
                        "stars": 193790, "forks": 47212, "language": "TypeScript",
                        "license": "MIT", "pushed_at": "2026-10-13",
                        "description": "Reverse engineer anything with agents."}]}
    text = W.render_repo_rows(found, "REA", "stars")
    check("渲染头标明这是官方接口实测（可直接引用）",
          text.startswith("[实测 · API]"), text[:60])
    check("带上 star 与 fork", "star=193790" in text and "fork=47212" in text, text)
    check("带上主语言与最近更新", "lang=TypeScript" in text and "2026-10-13" in text, text)
    check("带上仓库地址（下一步 fetch_json 直接用）",
          "https://github.com/morluto/rea" in text, text)
    check("带上描述（判断哪个是正主要用）", "Reverse engineer anything" in text, text)

    miss = W.render_repo_rows({"items": [], "error": "接口拒绝（403）"}, "REA")
    check("没有结果时也不崩、说清原因",
          "没拿到结果" in miss and "403" in miss, miss)

    # NOASSERTION 是「有 LICENSE 文件但识别不出协议」的占位值，不能当协议报出去
    na = W.render_repo_rows(
        {"total": 1, "items": [dict(found["items"][0], license="NOASSERTION")]}, "REA")
    check("NOASSERTION 不当作协议渲染", "NOASSERTION" not in na, na)


def test_repo_keyword() -> None:
    check("事故原句：挑出全大写缩写 REA",
          W.repo_keyword("帮我查一下 git 上叫 REA 的项目") == "REA")
    check("口语版同样挑得出", W.repo_keyword("git 上那个 REA 项目") == "REA")
    check("英文/站点限定版也认", W.repo_keyword('site:github.com "REA" repository') == "REA")
    check("owner/repo 形态原样返回",
          W.repo_keyword("看看 openai/whisper 这个仓库") == "openai/whisper")
    check("没有 git/仓库 信号的查询不触发（不该拖去打 GitHub 接口）",
          W.repo_keyword("今天天气怎么样") == "" and W.repo_keyword("python 项目推荐") == "")
    check("只有泛词（项目 / 仓库）时不给关键词",
          W.repo_keyword("帮我找找那个很出名的开源项目") == ""
          and W.repo_keyword("git 上的仓库") == "")
    check("空查询安全", W.repo_keyword("") == "" and W.repo_keyword(None) == "")
    # rerun56：按「最长词」挑词会选中 presentation，把真正的项目名 Marp 丢掉，
    # ★ 同名兜底于是拿 presentation 去查，返回一堆 Swift/Java 的 Presentation 项目。
    check("专有名词优先于领域泛词（Marp 不被 presentation 盖过）",
          W.repo_keyword("Marp markdown presentation tool github repository") == "Marp",
          W.repo_keyword("Marp markdown presentation tool github repository"))
    check("同类：Slidev 不被 slides 盖过",
          W.repo_keyword("slidev markdown slides github") == "slidev")
    check("领域泛词不与普通词争（vue components github → vue）",
          W.repo_keyword("vue components github") == "vue")
    check("功能词不当关键词（to 不抢）",
          "to" != W.tighten_query("markdown to pptx cli open source")[0].split('"')[-2])


def test_github_search_tool() -> None:
    tools = make_tools()
    gh = tools["github_search"]
    check("github_search 是只读工具（自动执行，走不到审批）", gh.risk == "safe", gh.risk)
    props = gh.parameters["properties"]
    check("参数含 query / sort / limit", {"query", "sort", "limit"} <= set(props),
          str(list(props)))
    check("sort 的取值受枚举约束",
          props["sort"]["enum"] == ["stars", "forks", "updated", "best-match"],
          str(props["sort"]))
    check("query 必填", gh.parameters.get("required") == ["query"],
          str(gh.parameters.get("required")))
    check("描述里点明这是官方接口实测、并说明已知仓库全名时改用 fetch_json",
          "官方接口实测" in gh.description and "fetch_json" in gh.description,
          gh.description[:80])

    calls, restore = with_router(
        lambda u, p: GhResp({"total_count": 1, "items": _GH_ITEMS[1:]}))
    try:
        out = gh.run(query="REA", limit=3)
    finally:
        restore()
    check("工具真的打接口并渲染出候选",
          "morluto/rea" in out and "star=193790" in out, out[:300])
    check("limit 透传到接口", calls[0][1].get("per_page") == 3, repr(calls[0][1]))


def test_repo_backstop() -> None:
    """找某个仓库时，web_search 自动补官方接口（★同名 + star 候选 + npm）。"""
    tools = make_tools()
    search = tools["web_search"]

    def router(url, params):
        q = str(params.get("q") or "")
        if "api.github.com/search/repositories" in url:
            # 名字同名检索（精确词 / in:name）→ 给 ★同名的正主
            if "in:name" in q or '"' in q:
                return GhResp({"total_count": 2, "items": [
                    {"full_name": "Akins20/ui-critic", "name": "ui-critic",
                     "html_url": "https://github.com/Akins20/ui-critic",
                     "stargazers_count": 0, "forks_count": 0, "language": "TypeScript",
                     "license": {"spdx_id": "MIT"}, "pushed_at": "2026-09-10T00:00:00Z",
                     "description": "A second pair of eyes for a UI: screenshots in, ranked Gemini critique out."}]})
            # star 排序的常规候选
            return GhResp({"total_count": 6255050, "items": _GH_ITEMS})
        if "registry.npmjs.org" in url:
            return FakeResp(json.dumps({"total": 1, "objects": [
                {"package": {"name": "ui-critic", "version": "1.2.0",
                             "description": "Deterministic design lint for frontend",
                             "date": "2026-05-01T00:00:00Z",
                             "links": {"npm": "https://www.npmjs.com/package/ui-critic",
                                       "repository": "https://github.com/alex410000/ui-critic"}}}]}),
                            ctype="application/json")
        return FakeResp(bing_html([
            ("https://github.com/bbylw/rea-cn", "bbylw/rea-cn: REA：逆向一切",
             "通过一个 MCP 服务，逆向分析二进制文件"),
            ("https://rea.run/zh/", "REA 中英社区指南", "社区文档")]))

    calls, restore = with_router(router)
    try:
        out = search.run(query="帮我查一下 git 上叫 REA 的项目")
    finally:
        restore()

    gh_calls = [(u, p) for u, p in calls if "api.github.com" in u]
    npm_calls = [(u, p) for u, p in calls if "registry.npmjs.org" in u]
    check("识别到「在找仓库」后打了 GitHub 官方接口", len(gh_calls) >= 1, repr(calls))
    check("star 候选那枪按 star 排序（问「哪个更有名」）",
          any(p.get("sort") == "stars" for _, p in gh_calls), repr(calls))
    check("查询词是挑出来的项目名（不是整句提问）",
          any(p.get("q") == "REA" for _, p in gh_calls), repr(calls))
    check("也打了 npm registry（项目可能是包）", len(npm_calls) == 1, repr(calls))
    check("权威候选排在最前面（先看到的是一手数据）",
          out.index("GitHub 官方搜索接口") < out.index("[搜索摘要"), out[:200])
    check("正主 morluto/rea 出现在输出里", "morluto/rea" in out, out[:600])
    check("候选带 star 数（不用去摘要里捡数字）", "star=193790" in out, out[:600])
    check("npm 结果也在（包名/版本/源码仓库）",
          "ui-critic" in out and "alex410000/ui-critic" in out, out[:1200])
    check("原始网页结果没有被吞掉", "bbylw/rea-cn" in out, out[:1200])
    check("尾部点明「哪个是正主以同名候选为准」", "别拿搜索摘要里的转述判断" in out, out[-600:])
    check("仍然要求抓一手来源核实", "fetch_json" in out)

    # 不是仓库检索 → 不该多打这些兜底
    calls2, restore2 = with_router(router)
    try:
        search.run(query="今天天气怎么样")
    finally:
        restore2()
    check("普通查询不触发 GitHub/npm 接口",
          not any("api.github.com" in u or "registry.npmjs.org" in u for u, _ in calls2),
          repr(calls2))

    # 兜底接口挂了也不能把 web_search 拖死
    def broken(url, params):
        if "api.github.com" in url or "registry.npmjs.org" in url:
            raise R.ConnectionError("down")
        return FakeResp(bing_html([("https://example.com/a", "结果", "摘要")]))

    _, restore3 = with_router(broken)
    try:
        out3 = search.run(query="帮我查一下 git 上叫 REA 的项目")
    finally:
        restore3()
    check("兜底接口失败时静默降级（不塞错误、也不抛）",
          "[搜索摘要" in out3 and "ConnectionError" not in out3, out3[:300])


def test_exact_name_lookup() -> None:
    """★名字同名检索：不按 star 排，0 star 的同名正主也能浮出来。"""
    calls, restore = with_router(
        lambda u, p: GhResp({"total_count": 1, "items": [{
            "full_name": "Akins20/ui-critic", "name": "ui-critic",
            "html_url": "https://github.com/Akins20/ui-critic",
            "stargazers_count": 0, "forks_count": 0, "language": "TypeScript",
            "license": {"spdx_id": "MIT"}, "pushed_at": "2026-09-10T00:00:00Z",
            "description": "A second pair of eyes for a UI."}]}))
    try:
        got = W.exact_repo_lookup("UI-Critic")
    finally:
        restore()
    check("名字归一化后认得出同名（UI-Critic == ui-critic）",
          bool(got["items"]) and got["items"][0]["key"] == "uicritic", repr(got))
    check("查询用的是精确词 + in:name（不靠 star 排序）",
          any("in:name" in str(p.get("q")) or '"UI-Critic"' in str(p.get("q"))
              for _, p in calls), repr(calls))
    check("★同名条目带完整名与地址（下一步 fetch_json 直接用）",
          got["items"][0]["full_name"] == "Akins20/ui-critic" and
          "github.com/Akins20/ui-critic" in got["items"][0]["url"], repr(got["items"][0]))
    # render_repo_rows 给同名候选打 ★
    rendered = W.render_repo_rows({"items": got["items"], "total": 1, "error": ""},
                                 "UI-Critic", "best-match")
    check("★同名候选标出来了（0 star 的正主靠它浮出来）",
          "★名字同名" in rendered and "Akins20/ui-critic" in rendered, rendered[:300])

    # 没有同名时不报错，空列表
    calls2, restore2 = with_router(
        lambda u, p: GhResp({"total_count": 0, "items": []}))
    try:
        miss = W.exact_repo_lookup("ui-critic")
    finally:
        restore2()
    check("没同名时返回空列表（不报错）", miss["items"] == [] and miss["error"] == "", repr(miss))


def test_npm_search() -> None:
    """npm 官方 registry 检索：项目可能是包不是 GitHub 热门仓库。"""
    calls, restore = with_router(
        lambda u, p: FakeResp(json.dumps({
            "total": 1,
            "objects": [{"package": {
                "name": "ui-critic", "version": "1.2.0",
                "description": "Deterministic design lint for frontend",
                "date": "2026-05-01T00:00:00Z",
                "links": {"npm": "https://www.npmjs.com/package/ui-critic",
                          "repository": "https://github.com/alex410000/ui-critic"}}}]}),
            ctype="application/json"))
    try:
        got = W.npm_package_search("ui-critic", limit=5)
    finally:
        restore()
    check("打的是 npm 官方 registry 搜索接口",
          calls and "registry.npmjs.org/-/v1/search" in calls[0][0], repr(calls))
    check("解析出包名与版本", got["items"][0]["name"] == "ui-critic"
          and got["items"][0]["version"] == "1.2.0", repr(got["items"]))
    check("带出源码仓库地址（GitHub 正主可能藏在这里）",
          "alex410000/ui-critic" in got["items"][0]["repo"], repr(got["items"][0]))
    check("渲染出 npm 包名与仓库链接",
          "ui-critic" in W.render_npm_rows(got, "ui-critic")
          and "alex410000/ui-critic" in W.render_npm_rows(got, "ui-critic"),
          W.render_npm_rows(got, "ui-critic"))

    # 空查询不打接口
    calls2, restore2 = with_router(lambda u, p: FakeResp("{}"))
    try:
        empty = W.npm_package_search("   ")
    finally:
        restore2()
    check("空查询直接返回错误、不打接口",
          empty["items"] == [] and bool(empty["error"]) and not calls2, repr(empty))


def test_npm_search_tool() -> None:
    """npm_search 是独立工具，不只是 web_search 的内部兜底。"""
    tools = make_tools()
    assert "npm_search" in tools, "npm_search 工具未注册"
    npm = tools["npm_search"]
    check("npm_search 是只读工具", npm.risk == "safe", npm.risk)
    props = npm.parameters["properties"]
    check("参数含 query / limit", {"query", "limit"} <= set(props), str(list(props)))
    check("query 必填", npm.parameters.get("required") == ["query"],
          str(npm.parameters.get("required")))
    check("描述里点明是 npm registry 实测、并指引已知包名时 fetch_json",
          "npm" in npm.description and "fetch_json" in npm.description,
          npm.description[:80])

    calls, restore = with_router(
        lambda u, p: FakeResp(json.dumps({
            "total": 1,
            "objects": [{"package": {"name": "ui-critic", "version": "1.2.0",
                                     "description": "lint tool",
                                     "links": {"npm": "https://www.npmjs.com/package/ui-critic"}}}]}),
            ctype="application/json"))
    try:
        out = npm.run(query="ui-critic")
    finally:
        restore()
    check("工具真的打接口并渲染出包",
          "ui-critic" in out and "lint tool" in out, out[:300])


def main() -> None:
    test_failure_hint()
    test_fetch_url_wires_hint()
    test_query_judgement()
    test_tighten_query()
    test_search_orchestration()
    test_github_search_api()
    test_github_render()
    test_repo_keyword()
    test_github_search_tool()
    test_repo_backstop()
    test_exact_name_lookup()
    test_npm_search()
    test_npm_search_tool()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
