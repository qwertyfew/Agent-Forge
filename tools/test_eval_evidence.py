"""评测层证据测试：PTC 下「到底调没调工具」怎么算。

为什么单开一套：`tool_called` / `tool_must_not` 的判定全靠这里的解析。PTC 常驻后
顶层只看得见 `ops_batch`，真实工具名藏在脚本里——解析漏一种写法，本来成功的任务
就会被记成「全程未调用」（rerun64 的 f5 就是这样被误判的）。评测脚本和被测系统
一样需要证据，它的解析必须有自己的红点。

运行：./.venv/Scripts/python.exe tools/test_eval_evidence.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import bench_tasks as B  # noqa: E402


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + "  " + name
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"测试未通过: {name}")


def main() -> None:
    # ---- 脚本写法：两种合法通道都要认 ----
    _check("① call(\"x\") 形式",
           B._inner_calls("ops_batch", {"script": 'call("fetch_json", url="u")'}) == ["fetch_json"])
    _check("② tools.call(\"x\") 形式",
           B._inner_calls("ops_batch", {"script": 'tools.call("github_search", query="q")'})
           == ["github_search"])
    _check("③ from tools import x + x() 形式",
           B._inner_calls("ops_batch", {"script":
                          'from tools import github_search\nr = github_search(query="REA")'})
           == ["github_search"])
    _check("④ from tools import a, b",
           B._inner_calls("ops_batch", {"script":
                          'from tools import read_file, write_file\nprint(read_file("a"))'})
           == ["read_file", "write_file"])
    _check("⑤ import tools + tools.x()",
           B._inner_calls("ops_batch", {"script":
                          'import tools\ntools.write_file(path="a", content="b")'})
           == ["write_file"])
    _check("⑥ 非 ops_batch 不解析",
           B._inner_calls("read_file", {"script": 'call("x")'}) == [])
    _check("⑦ 注释里的调用不算（无 call/import 关键字时）",
           B._inner_calls("ops_batch", {"script": "# 打算用 github_search\nprint(1)"}) == [])

    # ---- 工具层自己的小结（执行后统计，最权威）----
    _check("⑧ 小结里的多工具",
           B._summary_calls("[ops_batch] 2 次工具调用（edit_file×1、run_command×1）")
           == ["edit_file", "run_command"])
    _check("⑨ 小结里的单工具",
           B._summary_calls("[ops_batch] 1 次工具调用（github_search×1）\n正文")
           == ["github_search"])
    _check("⑩ 零调用小结不编造工具",
           B._summary_calls("[ops_batch] 0 次工具调用 —— 脚本执行了，但里面没有任何一次") == [])

    # ---- 合成口径 ----
    proc = [
        {"k": "tool", "name": "ops_batch",
         "args": {"script": 'from tools import github_search\ngithub_search(query="REA")'},
         "out": "[ops_batch] 1 次工具调用（github_search×1）"},
        {"k": "tool", "name": "ask_user", "args": {}, "out": "用户答复"},
    ]
    used = B._used_tools(proc)
    _check("⑪ used_tools 含内层调用",
           {"github_search", "ops_batch", "ask_user"} <= used, str(sorted(used)))
    _check("⑫ 旧记录（没有 inner 字段）也能补算",
           "github_search" in B._used_tools(
               [{"k": "tool", "name": "ops_batch",
                 "args": {"script": 'call("github_search", query="x")'}, "out": ""}]))
    _check("⑬ 纯问答任务 tools 为空",
           B._used_tools([]) == set())

    # ---- 用 rerun64 的真实记录回归：f5 曾被误判 ----
    import json
    real = ROOT / "evals" / "results" / "rerun64.json"
    if real.exists():
        d = json.loads(real.read_text(encoding="utf-8"))
        f5 = next((c for c in d["cases"] if c["id"] == "f5-git-search-rea"), None)
        if f5:
            got = B._used_tools(f5.get("process") or [])
            _check("⑭ 真实记录 f5 现在能读出 github_search",
                   "github_search" in got, str(sorted(got)))
        q1 = next((c for c in d["cases"] if c["id"] == "q1-idempotent-no-tools"), None)
        if q1:
            _check("⑮ 真实记录 q1 仍未动用任何工具",
                   B._used_tools(q1.get("process") or []) == set(),
                   str(sorted(B._used_tools(q1.get("process") or []))))

    # ---- P2：引用可追溯判定 ----
    ev = ("[实测 · API] GitHub 官方搜索接口 · 查询「REA」\n"
          "  1. morluto/rea  https://github.com/morluto/rea\n"
          "  2. im95able/Rea  https://github.com/im95able/Rea\n")
    proc = [{"s": 1, "k": "tool", "name": "ops_batch", "out": ev}]
    _check("⑯ 名字都来自工具输出 → 通过",
           B.citation_issue("all", "推荐 morluto/rea，备选 im95able/Rea。", proc) == "")
    _check("⑰ 编造的仓库名 → 判红",
           "找不到" in B.citation_issue("all", "推荐 awesome/rea-cli。", proc))
    # 历史现场：rerun65 的答复一个仓库名都没给（只有自造的 "REA (Research ...)"
    # 释义）→ all 模式放过、any 模式要判红（两种模式解决两种错）
    rerun65_like = "请问您是指哪一类 REA？1. REA (Research and Evaluation Architecture)"
    _check("⑱ all 模式对「一个名字都没给」不判（避免误伤诚实空结果）",
           B.citation_issue("all", rerun65_like, proc) == "")
    _check("⑲ any 模式要求至少点名一条（判据才有信息量）",
           "一个都没点名" in B.citation_issue("any", rerun65_like, proc))
    empty_proc = [{"s": 1, "k": "tool", "name": "ops_batch",
                   "out": "[ops_batch] 1 次工具调用（github_search×1）"}]
    _check("⑳ 检索确实为空 + 如实说不存在 → any 也不判",
           B.citation_issue("any", "没有检索到该仓库，无法确认。", empty_proc) == "")
    _check("㉑ 未开启该判据 → 恒过",
           B.citation_issue("", "推荐 awesome/fake-repo。", proc) == "")

    print("\n全部通过")


if __name__ == "__main__":
    main()
