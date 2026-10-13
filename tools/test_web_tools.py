"""网络工具族的自测：失败降级指路 + 泛查询自动收紧。

运行：python tools/test_web_tools.py
全部断言内建，退出码 0 = 全过。**不联网**——requests.get 全程用替身。

用例取自同一批真实事故（REA 检索三跑）的两个缺口：

  ① 失败后没有一等公民的降级路径。fetch_url 被代理拒之后，模型手里明明有
     fetch_json 却没用，转而去猜了个 browser（白烧两轮）——而「GitHub 仓库打
     api.github.com/repos/o/r」这条正解就印在它刚读过的工具说明里。
     问题不在能力，在**失败那一刻没人把这条和眼前的错误连起来**。

  ② 搜索词不设防。三跑的第一枪都是泛查询，返回的全是 GitHub 首页与教程站
     （白烧一轮）；第二枪加了 `site:github.com "..."` 才对。

两件事都不该靠模型临场想起来。所以：失败指路由程序按失败类型与 URL 形态写出来；
泛查询由程序自动补一枪精确的，把结果拼在同一次返回里。
"""
from __future__ import annotations

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


def main() -> None:
    test_failure_hint()
    test_fetch_url_wires_hint()
    test_query_judgement()
    test_tighten_query()
    test_search_orchestration()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
