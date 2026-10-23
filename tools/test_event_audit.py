"""event_audit 的口径回归：返工怎么数，不许含糊。

这套断言防的不是「代码写错」，是「口径漂移」——把一次错误数成两次（返工量翻倍），
或者把整写覆盖数成零（看不见最大的那笔无用功），都会让人得出相反的结论。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import event_audit as ea  # noqa: E402

OK_BLOCK = ("\n\n【系统核对·写盘结果】以下由 harness 实测得出，不是脚本里的 print：\n"
            "  - write_file → {p}: 已写入磁盘\n"
            "  ✓ 以上文件已确认存在于磁盘。\n")


def _tool(s, out, inner=None, name="ops_batch", args=None):
    return {"s": s, "k": "tool", "name": name, "args": args or {}, "out": out,
            **({"inner": inner} if inner else {})}


def _run(process, *, steps=1, turns=None, verdict="pass"):
    case = {"id": "t", "verdict": verdict, "layer": "check", "duration": 1.0,
            "steps": steps, "process": process}
    if turns is not None:
        case["turns"] = turns
    return ea.audit_run(case, seed_names=set())


def test_clean_run():
    r = _run([_tool(1, "[ops_batch] 1 次工具调用（read_file×1）", ["read_file"]),
              _tool(2, "[ops_batch] 1 次工具调用（write_file×1）" + OK_BLOCK.format(p="a.md"),
                    ["write_file"]),
              {"s": 3, "k": "verify"}], steps=3)
    assert r["clean"] is True, r
    assert r["calls"] == 2
    assert r["error_n"] == 0 and r["redundant_n"] == 0


def test_param_err_beats_write_fail():
    """同一个错误的两句描述：只算一次返工，归到根因（签名错）。"""
    out = ("[ops_batch] 1 次工具调用（edit_file×1）\n"
           "[错误] 工具 edit_file 参数不对：got an unexpected keyword argument 'content'\n"
           "[参数] edit_file 接受：path（必填）、old_text（必填）、new_text（必填）\n"
           "\n【系统核对·写盘结果】\n  - edit_file → a.py: 写入失败\n")
    r = _run([_tool(1, out, ["edit_file"])], steps=1)
    assert r["error_n"] == 1, r["waste"]
    assert r["kinds"] == ["param_err"], r["kinds"]


def test_noop_script_first():
    out = ("[ops_batch] 0 次工具调用 —— 脚本执行了，但里面没有任何一次 tools.call(...)\n"
           "[脚本异常] NameError: name 'x' is not defined\n")
    r = _run([_tool(1, out, ["write_file"])], steps=1)
    assert r["kinds"] == ["noop_script"], r["kinds"]


def test_edit_miss_counts_once():
    out = ("[ops_batch] 1 次工具调用（edit_file×1）\n"
           "[错误] 找到 3 处匹配，存在歧义，未做任何改动。\n"
           "\n【系统核对·写盘结果】\n  - edit_file → a.py: 写入失败\n")
    r = _run([_tool(1, out, ["edit_file"])], steps=1)
    assert r["error_n"] == 1 and r["kinds"] == ["edit_miss"], r["waste"]


def test_discover():
    r = _run([_tool(1, "tools: github_search", name="find_tools",
                    args={"query": "github_search"})], steps=1)
    assert r["kinds"] == ["discover"], r["kinds"]


def test_rewrite_counts_superseded_writes():
    """整写三次同一路径：两次白写，但不算错误。"""
    proc = [_tool(1, "[ops_batch] 1 次工具调用（read_file×1）", ["read_file"])]
    for i in range(3):
        proc.append(_tool(2 + i, "[ops_batch] 1 次工具调用（write_file×1）"
                          + OK_BLOCK.format(p="reports/outline.md"), ["write_file"]))
    r = _run(proc, steps=4)
    assert r["error_n"] == 0, r["waste"]
    assert r["redundant_n"] == 2, r["waste"]
    assert r["clean"] is False


def test_failed_write_then_success_is_not_rewrite():
    """写失败后重写是同一次落盘，不能既算错误又算冗余。"""
    fail = ("[ops_batch] 1 次工具调用（write_file×1）\n"
            "\n【系统核对·写盘结果】\n  - write_file → a.md: 写入失败\n")
    r = _run([_tool(1, fail, ["write_file"]),
              _tool(2, "[ops_batch] 1 次工具调用（write_file×1）"
                    + OK_BLOCK.format(p="a.md"), ["write_file"])], steps=2)
    assert r["error_n"] == 1, r["waste"]
    assert r["redundant_n"] == 0, r["waste"]


def test_no_write_marker_no_rewrite():
    """脚本里写了 write_file，但系统核对里没有「已写入磁盘」——不算落盘。"""
    r = _run([_tool(1, "[ops_batch] 1 次工具调用（write_file×1）\nSuccessfully wrote a.md",
                    ["write_file"]),
              _tool(2, "[ops_batch] 1 次工具调用（write_file×1）\nSuccessfully wrote a.md",
                    ["write_file"])], steps=2)
    assert r["redundant_n"] == 0, r["waste"]


def test_turns_prefers_explicit_field():
    """多泳道并行时 steps 只是最后一条泳道的计数器，回合数要看 turns。"""
    r = _run([_tool(1, "x", ["read_file"])], steps=3, turns=7)
    assert r["turns"] == 7, r["turns"]
    r2 = _run([_tool(1, "x", ["read_file"])], steps=3)
    assert r2["turns"] == 3, r2["turns"]


def test_non_monotonic_steps_do_not_inflate():
    """步号回退（多泳道混流）不该凭空多出返工。"""
    proc = [_tool(1, "x", ["read_file"]), _tool(3, "y", ["list_dir"]),
            _tool(2, "z", ["read_file"])]
    r = _run(proc, steps=3)
    assert r["waste_n"] == 0, r["waste"]
    assert r["calls"] == 3


def test_fill_section_is_not_rewrite():
    """增量填充同一路径 6 次是 fill_section 的正确用法，不能算重复整写。"""
    proc = [_tool(1, "[ops_batch] 1 次工具调用（write_file×1）"
                  + OK_BLOCK.format(p="reports/outline.md"), ["write_file"])]
    for i in range(6):
        proc.append(_tool(2 + i, "[ops_batch] 1 次工具调用（fill_section×1）"
                          "\n\n【系统核对·写盘结果】\n"
                          "  - fill_section → reports/outline.md: 已写入磁盘\n",
                          ["fill_section"]))
    r = _run(proc, steps=7)
    assert r["redundant_n"] == 0, r["waste"]
    assert r["clean"] is True, r["waste"]


def test_write_file_then_fill_section_counts_one_rewrite():
    """整写两次 + 分节填充若干：只有整写的那次被覆盖。"""
    proc = [_tool(1, "[ops_batch] 1 次工具调用（write_file×1）"
                  + OK_BLOCK.format(p="a.md"), ["write_file"]),
            _tool(2, "[ops_batch] 1 次工具调用（write_file×1）"
                  + OK_BLOCK.format(p="a.md"), ["write_file"]),
            _tool(3, "[ops_batch] 1 次工具调用（fill_section×1）"
                  "\n\n【系统核对·写盘结果】\n  - fill_section → a.md: 已写入磁盘\n",
                  ["fill_section"])]
    r = _run(proc, steps=3)
    assert r["redundant_n"] == 1, r["waste"]


def test_markdown_has_two_tiers():
    runs = [_run([_tool(1, "[ops_batch] 0 次工具调用 —— 脚本执行了", ["write_file"])],
                 steps=1)]
    md = ea._md(runs, [Path("x.json")])
    assert "错误类" in md and "冗余类" in md, md[:400]
    assert "零返工" in md


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"event_audit：{len(fns)} 项全部通过")
