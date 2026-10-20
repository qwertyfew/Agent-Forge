"""写前策略（证据门槛 + 草稿暂存）的自测。

运行：python tools/test_prewrite.py
全部断言内建，退出码 0 = 全过。

背景：内容核查原先全在 write_file 内部——模型把整篇生成完之后才被打回，
代价是整篇重写（rerun23 连拒 4 次，每次三千字从头来）。本测试卡三件事：
1. 引用密集型内容 + 本任务零 read_file → 写之前就拒，并给出必读清单；
2. 拒绝时内容存进 .drafts/ 草稿，回传「改哪几行」，第二次明确禁止重写全文；
3. 正常场景不能误伤（读过一次就放行、引用少的普通文件不设门槛）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                    # noqa: E402
from agentforge.evidence import EvidenceLedger           # noqa: E402
from agentforge.permissions import Guard                 # noqa: E402
from agentforge.tools import build_registry              # noqa: E402
from agentforge import writepolicy                       # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


DENSE = (
    "# 差距清单\n\n"
    "| 维度 | 现状 |\n| :-- | :-- |\n"
    "| 压缩 | `agentforge/context.py:190` 有 Compressor |\n"
    "| 权限 | `agentforge/permissions.py:36` 有 Guard |\n"
    "| 循环 | `agentforge/loop.py:363` 有 Agent |\n"
    "| 会话 | `agentforge/tasksession.py:99` 有 TaskSession |\n"
)


def setup(tmp: Path):
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.state_dir = tmp / ".agentforge"
    cfg.prewrite_gate = True
    (tmp / "agentforge").mkdir(parents=True, exist_ok=True)
    for name in ("context.py", "permissions.py", "loop.py", "tasksession.py"):
        body = "class X:\n    pass\n" + ("# filler\n" * 500)
        (tmp / "agentforge" / name).write_text(body, encoding="utf-8")
    guard = Guard(cfg, approver=None)
    guard.ledger = EvidenceLedger()
    registry = build_registry(guard, cfg)
    return cfg, guard, registry


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, guard, registry = setup(tmp)
        wf = registry.get("write_file")
        check("write_file 工具存在", wf is not None)

        # ---------- 1. 零证据 + 引用密集 → 写前拒 ----------
        out = wf.run(path="evals/gap.md", content=DENSE)
        check("零读时拒收", out.startswith("[错误] 写前检查未通过"), out[:120])
        check("给出必读清单", "agentforge/context.py" in out, out[:300])
        check("文件确实没写入", not (tmp / "evals/gap.md").exists())
        check("解释拒收理由（引用只能来自读过的代码）", "引用只能从印象里编" in out, out[:300])

        # ---------- 2. 读过一次即放行 ----------
        guard.ledger.mark_file(tmp / "agentforge" / "context.py", lines=502, read=True)
        out2 = wf.run(path="evals/gap.md", content=DENSE)
        check("有过取证就放行", out2.startswith("已"), out2[:150])
        check("放行后文件真的写出来", (tmp / "evals/gap.md").exists())

        # ---------- 3. 引用少的普通文件不受门槛影响 ----------
        cfg2, guard2, registry2 = setup(tmp / "b")
        wf2 = registry2.get("write_file")
        plain = "# 笔记\n\n随便写点东西，没有引用。\n"
        out3 = wf2.run(path="notes/plain.md", content=plain)
        check("普通文件不设门槛", out3.startswith("已"), out3[:150])

        # ---------- 4. 拒绝时存草稿 + 定位行号 ----------
        cfg3, guard3, registry3 = setup(tmp / "c")
        wf3 = registry3.get("write_file")
        guard3.ledger.mark_file(tmp / "c" / "agentforge" / "context.py", lines=502, read=True)
        bad = DENSE.replace("agentforge/context.py:190", "agentforge/context.py:9999")
        out4 = wf3.run(path="evals/gap2.md", content=bad)
        check("行号越界被拒", out4.startswith("[错误] 引用核查未通过"), out4[:150])
        check("提示内容已暂存草稿", "已暂存为草稿" in out4, out4[:400])
        draft = tmp / "c" / ".drafts" / "evals__gap2.md"
        check("草稿文件真的落盘", draft.is_file(), str(draft))
        if draft.is_file():
            text = draft.read_text(encoding="utf-8")
            check("草稿内容与提交内容一致", text == bad, f"{len(text)} vs {len(bad)}")
            lineno = [i for i, ln in enumerate(bad.splitlines(), 1)
                      if "agentforge/context.py:9999" in ln][0]
            check("定位到草稿行号", f"草稿第 {lineno} 行" in out4, out4[:400])
        check("给出局部修正指令", "edit_file" in out4 and "不要整篇重写" in out4, out4[:400])

        # ---------- 5. 同一路径第二次被拒 → 禁止重写全文 ----------
        out5 = wf3.run(path="evals/gap2.md", content=bad)
        check("第二次被拒会升级措辞", "第 2 次被拒" in out5 and "禁止重写全文" in out5, out5[-200:])

        # ---------- 6. 语义闸门拒绝也走草稿（同一出口） ----------
        cfg4, guard4, registry4 = setup(tmp / "d")
        claim = ("# 结论\n\n| 维度 | 现状 |\n| :-- | :-- |\n"
                 "| 压缩 | `agentforge/context.py`：本仓库缺乏上下文压缩逻辑。 |\n")
        out6 = writepolicy.reject_message(
            "evals/claim.md", claim,
            [("上下文压缩", "断言称缺乏，实际 agentforge/context.py:190 有 class Compressor")],
            "claims", cfg4)
        check("语义拒绝走同一出口", out6.startswith("[错误] 语义核查未通过"), out6[:150])
        check("语义拒绝也存草稿", (tmp / "d" / ".drafts" / "evals__claim.md").is_file(), out6[:300])
        check("语义拒绝也定位行号", "草稿第 5 行" in out6, out6[:300])

        # ---------- 7. 开关关闭时不拦 ----------
        cfg5, guard5, registry5 = setup(tmp / "e")
        cfg5.prewrite_gate = False
        wf5 = registry5.get("write_file")
        out7 = wf5.run(path="evals/off.md", content=DENSE)
        check("prewrite_gate=False 时不设门槛", out7.startswith("已"), out7[:150])

        # ---------- 8. 草稿查询（供脚本执行前复用） ----------
        check("draft_ready 能查到草稿",
              writepolicy.draft_ready("evals/gap2.md", cfg3) == ".drafts/evals__gap2.md",
              writepolicy.draft_ready("evals/gap2.md", cfg3))
        check("无草稿时返回空", writepolicy.draft_ready("evals/none.md", cfg3) == "")

    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
