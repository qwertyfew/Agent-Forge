"""一手来源核查的自测：sources.py 的判定 + orchestrator._verify_gate 的接入。

运行：python tools/test_sources_gate.py
全部断言内建，退出码 0 = 全过。这是纯程序测试，不需要模型在线、不需要联网。

用例取自真实事故（REA 检索实测）：agent 只调 web_search，把搜索摘要里的
「star 已达 1w+」写进结论，而 GitHub API 实测是 17 万。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import sources as sg                    # noqa: E402
from agentforge.orchestrator import _verify_gate        # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _call(name: str, args: dict | None = None) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}


def _tool(name: str) -> dict:
    return {"role": "tool", "content": f"{name} 的结果"}


# 真实事故里的总结原文（浓缩）
REA_SUMMARY = (
    "REA（Reverse Engineer Anything）是一个把逆向工程流程交给 agent 的项目。\n"
    "- 活跃度：Star 数已达 1w+，单日涨星超 4600，fork 伴随高热度增长。\n"
    "- 最近更新：2026 年 7 月仍有显著动态。\n"
    "- 技术栈：原生 / JS-Electron / .NET 三端。\n"
)


def test_detection() -> None:
    # ---------- 工具清单口径 ----------
    only_search = [_call("web_search"), _tool("web_search")]
    with_fetch = only_search + [_call("fetch_json"), _tool("fetch_json")]
    with_fetch_url = only_search + [_call("fetch_url"), _tool("fetch_url")]
    with_snapshot = only_search + [_call("browser_snapshot"), _tool("browser_snapshot")]
    with_gh_search = only_search + [_call("github_search"), _tool("github_search")]
    local_only = [_call("read_file"), _tool("read_file")]

    check("ran_search：只用搜索 → True", sg.ran_search(only_search))
    check("ran_search：纯本地任务 → False", not sg.ran_search(local_only))
    check("ran_first_party_fetch：只有搜索 → False", not sg.ran_first_party_fetch(only_search))
    check("ran_first_party_fetch：有 fetch_json → True", sg.ran_first_party_fetch(with_fetch))
    check("ran_first_party_fetch：有 fetch_url → True", sg.ran_first_party_fetch(with_fetch_url))
    check("ran_first_party_fetch：有 browser_snapshot → True",
          sg.ran_first_party_fetch(with_snapshot))
    check("ran_first_party_fetch：有 github_search → True（它打的也是官方接口，不是转述）",
          sg.ran_first_party_fetch(with_gh_search))
    check("ran_first_party_fetch：空历史 → False", not sg.ran_first_party_fetch(None))
    check("tool_calls 里用顶层 name 字段也能认（兼容两种形状）",
          sg.ran_search([{"role": "assistant", "tool_calls": [{"name": "web_search"}]}]))

    # ---------- 事实判据 ----------
    hits = sg.external_claim_hits(REA_SUMMARY)
    check("命中事故原文里的星级数字（Star 数已达 1w+）",
          any("1w" in h for h in hits), repr(hits))
    check("命中中文夹字的涨星数字（涨星超 4600）",
          any("4600" in h for h in hits), repr(hits))
    check("命中事故原文里的年份（最近更新：2026 …）",
          any("2026" in h for h in hits), repr(hits))
    check("纯年月句也能认（2026 年 7 月）",
          bool(sg.external_claim_hits("本项目 2019 年立项，2026 年 7 月仍在迭代。")),
          repr(sg.external_claim_hits("本项目 2019 年立项，2026 年 7 月仍在迭代。")))
    check("命中顺序保序且去重", hits == list(dict.fromkeys(hits)), repr(hits))
    check("命中条数受限", len(hits) <= sg.MAX_HITS)

    check("数字在前指标在后也能认（171067 star）",
          bool(sg.external_claim_hits("实测 171067 star，40865 fork")))
    check("千分位数字能认（fork 已达 40,631）",
          bool(sg.external_claim_hits("仓库 fork 已达 40,631")))
    check("版本号能认（当前版本 v3.2.1）",
          bool(sg.external_claim_hits("当前版本 v3.2.1")))
    check("ISO 日期能认（发布于 2026-10-13）",
          bool(sg.external_claim_hits("发布于 2026-10-13")))
    check("大小写不敏感（STAR 100）", bool(sg.external_claim_hits("STAR 100")))
    check("跨行不凑数（「版本」与下一行的「2026」不拼成一条）",
          sg.external_claim_hits("版本\n\n2026") == [],
          repr(sg.external_claim_hits("版本\n\n2026")))
    check("单句里的版本事实照常认（版本 3）",
          bool(sg.external_claim_hits("版本 3")))

    check("英文指标词不做子串误伤（tissue 不算 issue）",
          not sg.external_claim_hits("this tissue sample 12 pieces"))
    check("无数字的定性描述不误报",
          not sg.external_claim_hits("这个仓库是用 TypeScript 写的，主要做逆向工程。"),
          repr(sg.external_claim_hits("这个仓库是用 TypeScript 写的，主要做逆向工程。")))
    check("纯本地交付总结不误报",
          not sg.external_claim_hits("报告已写入 docs/x.md，新增了 2 处校验。"),
          repr(sg.external_claim_hits("报告已写入 docs/x.md，新增了 2 处校验。")))
    check("空总结安全", sg.external_claim_hits("") == [])

    # ---------- 时效性定性转述：没有数字，也还是转述 ----------
    # A 组原句（实测）：通篇定性转述、一个阿拉伯数字都没有，旧判据静默放行。
    A_SUMMARY = ("REA 项目最近在开发者社区走红，10 月初的热度明显上升，"
                 "主打把逆向工程流程交给 agent。")
    q_rea = [_call("web_search", {"query": "REA 项目"}), _tool("web_search")]

    check("旧判据对纯定性转述确实不命中（这正是要补的缺口）",
          sg.external_claim_hits(A_SUMMARY) == [],
          repr(sg.external_claim_hits(A_SUMMARY)))
    check("补上之后：无数字的时效性转述不再放行",
          sg.needs_external_note(A_SUMMARY, q_rea) != [],
          repr(sg.needs_external_note(A_SUMMARY, q_rea)))
    check("命中片段就是那两个词本身（列出来读得懂）",
          {"最近", "月初"} <= set(sg.timeliness_claim_hits(A_SUMMARY, q_rea)),
          repr(sg.timeliness_claim_hits(A_SUMMARY, q_rea)))
    check("抓了一手来源之后，时效性转述也不标",
          sg.needs_external_note(
              A_SUMMARY, q_rea + [_call("fetch_json"), _tool("fetch_json")]) == [])
    check("没搜索（纯本地任务）时，时效词不误伤",
          sg.needs_external_note(A_SUMMARY, [_call("read_file"), _tool("read_file")]) == [])
    check("搜过但总结谈的是别的东西（与查询无实词交集）→ 不标",
          sg.needs_external_note("我最近改了 loop.py 里的超时处理。", q_rea) == [],
          repr(sg.needs_external_note("我最近改了 loop.py 里的超时处理。", q_rea)))
    check("空总结的时效性判据安全", sg.timeliness_claim_hits("", q_rea) == [])

    # ---------- 搜索词抽取：从工具参数里拿，不看模型自报 ----------
    check("从 arguments（dict）里取 query",
          sg.search_queries(q_rea) == ["REA 项目"], repr(sg.search_queries(q_rea)))
    check("arguments 是 JSON 字符串也能解",
          sg.search_queries([{"role": "assistant", "tool_calls": [
              {"function": {"name": "web_search", "arguments": '{"query": "x y"}'}}]}]) == ["x y"])
    check("非搜索工具的参数不会被当成搜索词",
          sg.search_queries([_call("fetch_url", {"url": "https://a/b"})]) == [])

    # ---------- 三条件同时成立 ----------
    check("搜索 + 无一手来源 + 有数字 → 出手",
          sg.needs_external_note(REA_SUMMARY, only_search) != [])
    check("搜索 + 已抓一手来源 → 不出手",
          sg.needs_external_note(REA_SUMMARY, with_fetch) == [])
    check("搜索 + 无一手来源 + 无数字 → 不出手",
          sg.needs_external_note("这是一个逆向工程 agent 项目。", only_search) == [])
    check("没搜索 + 有数字 → 不出手",
          sg.needs_external_note(REA_SUMMARY, local_only) == [])

    # ---------- 声明块 ----------
    block = sg.external_unverified_block(sg.needs_external_note(REA_SUMMARY, only_search))
    check("声明块带系统侧标记", "【系统侧核查】" in block)
    check("声明块点明缺的是一手来源",
          "fetch_json" in block and "一手来源" in block)
    check("声明块逐条列出裸奔的数字", any("1w" in ln for ln in block.splitlines()))
    check("声明块声明程序完成、不依赖模型自报", "不依赖模型自报" in block)


def test_gate_wiring() -> None:
    """接入 _verify_gate：走一遍真闸门，看答复末尾有没有那段声明。"""
    tmp = Path(tempfile.mkdtemp(prefix="af_sources_"))

    class FakeCfg:
        verify_gate = True
        sources_gate = True
        # 本机事实核查是另一个契约，另有 tools/test_local_facts.py 专测；
        # 这里显式关掉，免得两个闸门在一个文件里互相牵连（判据串味最难查）。
        local_facts_gate = False
        workspace_root = str(tmp)

    class FakeAgent:
        cfg = FakeCfg()

    brief = "检索 GitHub 上的项目 REA，做一份项目介绍"
    only_search = [_call("web_search"), _tool("web_search")]

    # 1) 只用搜索：应追加声明
    holder = {"summary": REA_SUMMARY, "convo": only_search}
    list(_verify_gate(FakeAgent(), brief, holder))
    check("闸门接入：只用搜索 → 答复末尾出现系统侧声明",
          "【系统侧核查】" in holder["summary"], holder["summary"][-200:])

    # 2) 抓过一手来源：不应追加
    holder2 = {"summary": REA_SUMMARY,
               "convo": only_search + [_call("fetch_json"), _tool("fetch_json")]}
    list(_verify_gate(FakeAgent(), brief, holder2))
    check("闸门接入：抓过一手来源 → 不加声明",
          "【系统侧核查】" not in holder2["summary"], holder2["summary"][-200:])
    check("闸门接入：不加声明时正文原样不动",
          holder2["summary"] == REA_SUMMARY)

    # 3) 开关关掉：即便命中也不出手
    class FakeCfgOff(FakeCfg):
        sources_gate = False

    class FakeAgentOff:
        cfg = FakeCfgOff()

    holder3 = {"summary": REA_SUMMARY, "convo": only_search}
    list(_verify_gate(FakeAgentOff(), brief, holder3))
    check("闸门接入：sources_gate=false 时不出手",
          holder3["summary"] == REA_SUMMARY)

    # 4) 纯本地任务（没搜索）：不误伤
    holder4 = {"summary": REA_SUMMARY, "convo": [_call("read_file"), _tool("read_file")]}
    list(_verify_gate(FakeAgent(), brief, holder4))
    check("闸门接入：纯本地任务不误伤", holder4["summary"] == REA_SUMMARY)

    # 5) 定性时效性转述（零阿拉伯数字）：也要走真闸门
    q_rea = [_call("web_search", {"query": "REA 项目"}), _tool("web_search")]
    summary5 = "REA 项目最近在开发者社区走红，10 月初的热度明显上升。"
    holder5 = {"summary": summary5, "convo": q_rea}
    list(_verify_gate(FakeAgent(), brief, holder5))
    check("闸门接入：无数字的时效性转述也出手",
          holder5["summary"] != summary5,
          repr(holder5["summary"][:60]))


def main() -> None:
    test_detection()
    test_gate_wiring()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
