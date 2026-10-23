"""PTC 下的工具轨迹解析：闸门「按工具名判定证据」还灵不灵。

背景（rerun65 实测）：PTC 常驻后顶层 `tool_calls` 只有 ops_batch，于是
- `sources.ran_local_inspection` 判成「全程没读过工作区」→ 已经写好的答复被盖
  「未经核实」（**误伤**）；
- `sources.ran_search` 恒为假 → 外部事实核查整条**静默失效**。

这套测试钉住的是「解析层」：只要 PTC 脚本里的真实调用读不出来，上面两道闸门
就会一个乱报、一个不报——而且都不会有任何报错。

运行：./.venv/Scripts/python.exe tools/test_tooltrace.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import sources, tooltrace  # noqa: E402


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + "  " + name
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"测试未通过: {name}")


def _convo(calls: list[tuple[str, str]], results: list[str]) -> list[dict]:
    """按「一次 assistant 带一个工具调用 + 一条 tool 回执」拼轨迹。"""
    out: list[dict] = []
    for (name, script), res in zip(calls, results):
        tc = {"function": {"name": name, "arguments": {"script": script}}}
        out.append({"role": "assistant", "content": "", "tool_calls": [tc]})
        out.append({"role": "tool", "content": res})
    return out


SUMMARY_READ = "[ops_batch] 3 次工具调用（read_file×2、list_dir×1）\n正文略"
SUMMARY_SEARCH = "[ops_batch] 1 次工具调用（github_search×1）\n候选略"
SUMMARY_FETCH = "[ops_batch] 1 次工具调用（fetch_url×1）\n[实测 · 网页] https://github.com/a/b\n正文"
SUMMARY_ZERO = "[ops_batch] 0 次工具调用 —— 脚本执行了，但里面没有任何一次 ..."


def main() -> None:
    # ---- 解析层 ----
    _check("① from tools import 写法能解析",
           tooltrace.names_from_script(
               'from tools import read_file\nprint(read_file("a"))') == ["read_file"])
    _check("② 小结优先（执行后统计）",
           tooltrace.summary_names(SUMMARY_READ) == ["read_file", "list_dir"])
    _check("③ 0 次调用的脚本不再退回源码猜（脚本没执行成）",
           tooltrace.tool_names(_convo(
               [("ops_batch", 'from tools import read_file\nread_file("a")')],
               [SUMMARY_ZERO])) == ["ops_batch"],
           str(tooltrace.tool_names(_convo(
               [("ops_batch", 'from tools import read_file\nread_file("a")')],
               [SUMMARY_ZERO]))))

    # ---- 第一道：本地事实核查（防误伤）----
    convo_read = _convo([("ops_batch", 'from tools import read_file, list_dir\n'
                                     'read_file("notes.md")\nlist_dir(".")')],
                        [SUMMARY_READ])
    _check("④ PTC 读文件 → 算读过工作区",
           sources.ran_local_inspection(convo_read) is True)
    state_q = "看看工作区里现在有哪些文件"
    stale = "工作区里有 notes.md、reports/outline.md 两个文件。"
    _check("⑤ 读过就不该再标「未核实」",
           sources.needs_local_note(stale, convo_read, message=state_q) == [],
           str(sources.needs_local_note(stale, convo_read, message=state_q)))
    _check("⑥ 真没读的时候仍然要标（反向断言）",
           sources.needs_local_note(stale, [], message=state_q) != [],
           str(sources.needs_local_note(stale, [], message=state_q)))

    # ---- 第二道：外部事实核查（防静默失效）----
    # 注意口径：github_search 打的是官方 API，算「一手来源」（FETCH_TOOLS），
    # 所以「检索过没核实」这条只对 web_search 成立。
    summary_web = "[ops_batch] 1 次工具调用（web_search×1）\n搜索摘要略"
    convo_search = _convo([("ops_batch", 'call("web_search", query="REA")')],
                          [summary_web])
    _check("⑦ PTC 检索 → 算检索过", sources.ran_search(convo_search) is True)
    claim = "这个项目有 352399 个 star，最近更新于 2026-10-21。"
    _check("⑧ 只搜过摘要、没抓正文 → 要标「转述未核实」",
           sources.needs_external_note(claim, convo_search) != [],
           str(sources.needs_external_note(claim, convo_search)))
    convo_fetch = _convo([("ops_batch", 'call("web_search", query="REA")'),
                          ("ops_batch", 'call("fetch_url", url="https://github.com/a/b")')],
                         [summary_web, SUMMARY_FETCH])
    _check("⑨ 抓过一手源 → 不再标（反向断言）",
           sources.needs_external_note(claim, convo_fetch) == [],
           str(sources.needs_external_note(claim, convo_fetch)))

    # ---- 顶层直连（非 PTC）路径不能因为改动而失效 ----
    plain = [{"role": "assistant", "content": "",
              "tool_calls": [{"function": {"name": "read_file",
                                           "arguments": {"path": "notes.md"}}}]},
             {"role": "tool", "content": "文件内容"}]
    _check("⑩ 非 PTC 的直连调用照旧可见",
           sources.ran_local_inspection(plain) is True)

    # ---- 与评测侧同一口径 ----
    sys.path.insert(0, str(ROOT / "tools"))
    import bench_tasks as B  # noqa: E402
    _check("⑪ 评测侧与产品侧解析结果一致",
           B._inner_calls("ops_batch", {"script": 'from tools import github_search\n'
                                                   'github_search(query="x")'})
           == tooltrace.names_from_script('from tools import github_search\n'
                                          'github_search(query="x")'))

    print("\n全部通过")


if __name__ == "__main__":
    main()
