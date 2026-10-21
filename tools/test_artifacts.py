"""「任务产出文件」的归因契约（agentforge/artifact.py）。

运行：python tools/test_artifacts.py
全部断言内建，退出码 0 = 全过。纯文件系统测试，不需要模型、不需要联网。

锁的是这一类坑：**把别人的改动算在本任务头上**。

事故现场（台账实证）：用户开新窗口问了一个纯问答的问题（「在你仍在输出文字时，
会进行很多『思考过程』，这些思考过程是什么」），一次工具都没调（`tools: 0`），
可它的产出文件里却有一条 `docs/evolution.md`（`via: "scan"`）。原因是收尾那段
`diff_snapshot` 只按 mtime 相减——任务跑着的那 20 秒里恰好有别的编辑器（另一个
agent）在写这个文件，于是界面变成「这个任务返回了一份它自己没生成的文档」。
用户的第一反应是「它凭什么凭空返回一个文档」，这是对的：那条记录本来就不该存在。

判据：**本任务没动过手，就没有产出。** 划清「工具亲口写的（确证）」与
「索引相减推出来的（推测）」这两类，前者才叫产出，后者只能说「这段时间它变了」。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import artifact                                    # noqa: E402
from agentforge.tools.base import EXEC, SAFE, WRITE               # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# 假注册表：真值取自 tools.base 的等级常量，免得测试自己编一套口径
RISK = {"list_dir": SAFE, "read_file": SAFE, "find_files": SAFE,
        "write_file": WRITE, "edit_file": WRITE, "run_command": EXEC}


def risk_of(name: str):
    return RISK.get(name)          # 未知工具返回 None


# ---------------------------------------------------------------- 动手判定

def test_may_have_written() -> None:
    check("一次工具都没调（纯问答）→ 不可能有产出", artifact.may_have_written([], risk_of) is False)
    check("None 当空处理", artifact.may_have_written(None, risk_of) is False)
    check("只调了只读工具 → 不可能写盘",
          artifact.may_have_written(["list_dir", "read_file", "find_files"], risk_of) is False)
    check("调过写类工具 → 扫",
          artifact.may_have_written(["list_dir", "write_file"], risk_of) is True)
    check("调过命令类工具 → 扫（脚本副作用只能靠扫兜）",
          artifact.may_have_written(["run_command"], risk_of) is True)
    check("未知工具保守当「可能写」——宁可多扫一遍",
          artifact.may_have_written(["some_plugin_tool"], risk_of) is True)
    check("名字为空串不误判为「写过」",
          artifact.may_have_written(["", "list_dir"], risk_of) is False)

    # 这个字面量是刻意重复的（artifact 要零依赖），必须有一条断言盯着它和真值同值
    check("artifact.READONLY_RISK 与 tools.base.SAFE 同值",
          artifact.READONLY_RISK == SAFE,
          f"{artifact.READONLY_RISK!r} vs {SAFE!r}")


# ---------------------------------------------------------------- 确证 vs 推测

def test_certain_flag() -> None:
    rec = artifact.classify_tool_write("write_file", {"path": "a.md"}, "已新建 a.md")
    check("工具亲口写的记 certain=True（确证）", rec and rec.get("certain") is True, repr(rec))
    rec2 = artifact.classify_tool_write("write_file", {"path": "a.md"}, "[错误] 写不进去")
    check("工具报错时不记产出", rec2 is None, repr(rec2))

    before = {artifact.norm("x"): ("x", 1.0, 10)}
    after = {artifact.norm("x"): ("x", 2.0, 10), artifact.norm("y"): ("y", 2.0, 5)}
    diff = artifact.diff_snapshot(before, after)
    check("扫描相减出来的记 certain=False（推测）",
          diff and all(d.get("certain") is False for d in diff), repr(diff))
    kinds = sorted(d["action"] for d in diff)
    check("扫描能分出 created / modified", kinds == ["created", "modified"], str(kinds))

    merged = artifact.merge([
        {"path": "x", "action": "modified", "via": "scan", "tool": "", "certain": False},
        {"path": "x", "action": "created", "via": "tool", "tool": "write_file", "certain": True},
    ])
    check("同一路径只留一条，且以工具记录为准",
          len(merged) == 1 and merged[0]["via"] == "tool" and merged[0]["action"] == "created",
          repr(merged))


def test_describe_passes_certain() -> None:
    root = Path(tempfile.mkdtemp(prefix="af_art_"))
    try:
        p = root / "a.md"
        p.write_text("x", encoding="utf-8")
        out = artifact.describe([
            {"path": str(p), "action": "created", "via": "tool", "tool": "write_file", "certain": True},
        ], root)
        check("describe 透出 certain=True", out and out[0].get("certain") is True, repr(out))

        # 老台账里没有这个字段：按 via 反推（扫描出来的历史记录不能默认当确证）
        old_scan = artifact.describe(
            [{"path": str(p), "action": "modified", "via": "scan", "tool": ""}], root)
        check("老台账缺字段时，via=scan 反推成 certain=False",
              old_scan and old_scan[0].get("certain") is False, repr(old_scan))
        old_tool = artifact.describe(
            [{"path": str(p), "action": "modified", "via": "tool", "tool": "edit_file"}], root)
        check("老台账缺字段时，via=tool 反推成 certain=True",
              old_tool and old_tool[0].get("certain") is True, repr(old_tool))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- 事故现场端到端

def test_pure_chat_gets_no_credit() -> None:
    """复现事故：纯问答任务 + 任务期间有别的程序改了工作区文件。"""
    root = Path(tempfile.mkdtemp(prefix="af_art_"))
    try:
        docs = root / "docs"
        docs.mkdir()
        victim = docs / "evolution.md"
        victim.write_text("v1", encoding="utf-8")

        before = artifact.snapshot(root)

        # 别的进程（另一个编辑器 / 另一个 agent）在任务跑着的时候改了它。
        # 用 utime 把 mtime 明确推后，不依赖写入速度与时间精度。
        victim.write_text("v2 —— 被别人改的", encoding="utf-8")
        os.utime(victim, (before[artifact.norm(victim)][1] + 30,) * 2)

        after = artifact.snapshot(root)
        raw = artifact.diff_snapshot(before, after)
        check("先确认扫描确实看见了这次改动（否则这个测试是空的）",
              any(artifact.norm(r["path"]) == artifact.norm(victim) for r in raw), repr(raw))

        # ① 纯问答：touched 空 → 不扫 → 产出为空
        file_log: list[dict] = []
        scanned = raw if artifact.may_have_written([], risk_of) else []
        merged = artifact.merge([*file_log, *scanned])
        check("事故现场：一次工具都没调的任务，产出文件为空（不背别人的锅）",
              merged == [], repr(merged))

        # ② 只调了只读工具：同样不扫
        scanned2 = raw if artifact.may_have_written(["list_dir", "read_file"], risk_of) else []
        check("只读了目录/文件的任务，同样不背这个锅",
              artifact.merge([*file_log, *scanned2]) == [])

        # ③ 真跑过命令：扫，并且如实标成「推测」
        scanned3 = raw if artifact.may_have_written(["run_command"], risk_of) else []
        merged3 = artifact.merge([*file_log, *scanned3])
        hit = [r for r in merged3 if artifact.norm(r["path"]) == artifact.norm(victim)]
        check("跑过命令的任务照旧能兜住副作用", len(hit) == 1, repr(merged3))
        check("但那条带着 certain=False，界面能区分「推测」",
              hit and hit[0].get("certain") is False, repr(hit))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_intermediate_classification() -> None:
    """清空弹窗的分类口径：依赖缓存/编译产物归「中间」，真实交付物归「最终」。

    这一条是把「整理文件夹时必删」与「任务成果必须留」两套直觉对齐：
    __pycache__、node_modules、.pyc 这类东西名字里没有 tmp，却是最该清的；
    而克隆下来的仓库源码（README.md / tests/）绝不能因为「扫描到的」就被清掉。
    """
    junk = [
        "p/__pycache__/m.cpython-313.pyc", "p/node_modules/react/index.js",
        "p/.venv/Lib/site-packages/x.py", "p/pkg/x.class", "p/obj/main.obj",
        "p/.pytest_cache/v/cache/lastfailed", "p/.DS_Store", "p/Thumbs.db",
        "p/build/out.o", "p/tmp/a.txt", "p/草稿_v2.md", "p/run.log",
    ]
    keep = [
        "p/report.md", "p/src/main.py", "p/output/deck.pptx",
        "p/learn-claude-code/README.md", "p/learn-claude-code/tests/test_x.py",
        "p/.git/config",
    ]
    for rel in junk:
        check(f"中间文件：{rel}", artifact.is_intermediate({"path": rel}))
    for rel in keep:
        check(f"最终产出：{rel}", not artifact.is_intermediate({"path": rel}))


def main() -> None:
    test_may_have_written()
    test_certain_flag()
    test_describe_passes_certain()
    test_pure_chat_gets_no_credit()
    test_intermediate_classification()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
