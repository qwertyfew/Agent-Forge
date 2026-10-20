"""任务书骨架（契约骨架）与产出清单兜底的自测。

运行：python tools/test_contract_skeleton.py

要证明的四件事：
1. 任务书里「至少覆盖这些…：A、B、C」能被确定性提取成必填项清单（不猜语义）；
2. 必填项在**任务开始**就铺成骨架文件（表格型任务就是表头 + 每项一行待填），
   空着的位置能被收尾核查判出来——缺项在骨架阶段就露；
3. 收尾的覆盖核查按同一份清单判「该写的写了没有」，缺项是 incomplete 问题，
   与引用核查（写出来的对不对）互补；
4. 修正轮也拿得到 file_log，且产出清单有文件级兜底（记录层 evidence.jsonl），
   不再出现「真写了文件、台账 files=0」。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import artifact                              # noqa: E402
from agentforge import skeleton as skel                      # noqa: E402
from agentforge import verify as vg                          # noqa: E402
from agentforge.orchestrator import _verify_gate             # noqa: E402
from agentforge.record import Recorder                       # noqa: E402

# 与真实任务书同构的最小样本（表格 + 必填项列举 + 点名一节）
BRIEF = """# 任务：逆向分析两个仓库，产出逐项差距清单

## 要做什么

产出逐项差距清单，写入 `evals/gap.md`。

## 交付物格式（硬性要求）

| 对比点 | 对方做法（附证据：文件/行号） | 现状（附证据：文件/行号） | 建议 |
| --- | --- | --- | --- |

至少覆盖这些对比点：agent loop 结构、上下文压缩策略、会话恢复（中断续跑）、skill 系统。

清单末尾附一节「按性价比排序的借鉴清单」。
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_cskel_"))
    root = tmp

    # ---------- A) 必填项提取：只认显式列举 ----------
    items = skel.required_items(BRIEF)
    check("A 提取出四条必填项 + 点名一节",
          items == ["agent loop 结构", "上下文压缩策略", "会话恢复（中断续跑）",
                    "skill 系统", "按性价比排序的借鉴清单"], repr(items))
    check("A 表格列名取自已声明的表头",
          skel.table_shape(BRIEF) == ["对比点", "对方做法（附证据：文件/行号）",
                                      "现状（附证据：文件/行号）", "建议"])
    check("A 无人列举的任务书不插手",
          skel.required_items("帮我看看 loop.py 写得怎么样") == [])
    built = skel.outline_from_brief(BRIEF)
    check("A 骨架文本含表头与四个待填行",
          built is not None and built[2] == "table"
          and built[0].count("（待填）") >= 4, repr(built and built[0][:80]))
    check("A 点名的一节单独成节而不是表格行",
          "## 按性价比排序的借鉴清单" in (built[0] if built else ""))

    # ---------- B) 铺骨架：待填位一开始就可见 ----------
    info = skel.seed_from_brief(root, "evals/gap.md", BRIEF)
    check("B 骨架已落盘", info is not None and (root / "evals/gap.md").is_file())
    pr = skel.progress(root, "evals/gap.md")
    check("B 进度：5 个待填位全空", pr["total"] == 5 and pr["filled"] == 0,
          f"{pr}")
    check("B 空骨架不算交付", skel.unfinished_file(root, "evals/gap.md"))

    # 已存在就不动（模型可能已经动手写了）
    before = (root / "evals/gap.md").read_text(encoding="utf-8")
    check("B 已存在的文件不被覆盖",
          skel.seed_from_brief(root, "evals/gap.md", BRIEF) is None
          and (root / "evals/gap.md").read_text(encoding="utf-8") == before)

    # 填两行 → 进度里立刻能看到还缺哪几项
    txt = before.replace("| agent loop 结构 | （待填） | （待填） | （待填） |",
                         "| agent loop 结构 | 对方 while 循环 loop.py:7 | "
                         "本仓多步编排 loop.py:1200 | 不采纳 |")
    txt = txt.replace("| skill 系统 | （待填） | （待填） | （待填） |",
                      "| skill 系统 | 按需加载 SKILL.md:3 | skills.py 注册表 | 可借鉴 |")
    (root / "evals/gap.md").write_text(txt, encoding="utf-8")
    pr = skel.progress(root, "evals/gap.md")
    check("B 填两行后进度是 2/5", pr["filled"] == 2, repr(pr))
    check("B 待填清单点名缺的项",
          "上下文压缩策略" in pr["pending"] and "会话恢复（中断续跑）" in pr["pending"],
          repr(pr["pending"]))
    check("B 仍有待填位 → 判未交付", skel.unfinished_file(root, "evals/gap.md"))

    # ---------- C) 收尾覆盖核查：写全了但少一项也算没交 ----------
    brief2 = "任务：写差距清单，写入 evals/gap2.md\n" + BRIEF.split("## 交付物格式")[1]
    full = ("# 差距清单\n\n"
            "| 对比点 | 对方 | 本仓 | 建议 |\n|---|---|---|---|\n"
            "| agent loop 结构 | a | b | c |\n"
            "| 上下文压缩策略 | a | b | c |\n"
            "| 会话恢复（中断续跑） | a | b | c |\n"
            "| skill 系统 | a | b | c |\n\n"
            "## 按性价比排序的借鉴清单\n\n1. 先做压缩\n")
    p2 = root / "evals/gap2.md"
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text(full, encoding="utf-8")
    req = skel.required_items(brief2)
    check("C 写全时覆盖核查无缺项", skel.coverage_missing(full, req) == [],
          repr(skel.coverage_missing(full, req)))
    part = full.replace("| 会话恢复（中断续跑） | a | b | c |\n", "")
    p2.write_text(part, encoding="utf-8")
    issues = vg.find_artifact_issues(
        [("evals/gap2.md", True)], root, produced=True,
        task_start_ts=time.time() - 30, required=req)
    incomplete = [c for c in issues if c.get("status") == "incomplete"]
    check("C 少一行被判 incomplete",
          len(incomplete) == 1 and incomplete[0]["missing"] == ["会话恢复（中断续跑）"],
          repr(issues))
    line = vg.issue_line(incomplete[0]) if incomplete else ""
    check("C 问题行直接点名缺哪项", "会话恢复" in line, line)
    issues_full = vg.find_artifact_issues(
        [("evals/gap2.md", True)], root, produced=True,
        task_start_ts=time.time() - 30, required=req) if False else None
    p2.write_text(full, encoding="utf-8")
    check("C 补齐后不再报",
          not [c for c in vg.find_artifact_issues(
              [("evals/gap2.md", True)], root, produced=True,
              task_start_ts=time.time() - 30, required=req)
              if c.get("status") == "incomplete"])

    # ---------- D) 修正轮拿得到 file_log；产出清单有文件级兜底 ----------
    class StubCfg:
        workspace_root = str(root)
        verify_gate = True

    seen: list = []

    class StubAgent:
        cfg = StubCfg()

        def run(self, convo, prompt, **kw):
            seen.append(kw.get("file_log"))
            hist = [*convo, {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "已补写"}]
            yield {"type": "assistant", "content": "已补写", "history": hist}

    log: list = []
    ag = StubAgent()
    holder = {"summary": "报告写好了", "convo": []}
    list(_verify_gate(ag, "任务：把报告写入 docs/rep.md", holder, file_log=log))
    check("D 修正轮收到同一个 file_log", seen and all(x is log for x in seen),
          repr([id(x) for x in seen]))

    rec = Recorder(root / "history" / "t1" / "_records", rel_root=root)
    rec.version(root / "docs" / "rep.md", "# 报告\n\n正文\n", old="")
    rows = rec.writes()
    check("D 记录层能读回写盘清单",
          len(rows) == 1 and rows[0]["path"] == "docs/rep.md", repr(rows))
    rec_files = artifact.from_records(rows)
    merged = artifact.merge([{"path": "docs/rep.md", "action": "created",
                              "via": "tool", "tool": "write_file",
                              "certain": True}, *rec_files])
    check("D 合并时工具记录优先、不重复",
          len(merged) == 1 and merged[0]["via"] == "tool", repr(merged))
    merged2 = artifact.merge([*rec_files])
    check("D 没有 file_log 时由记录兜底出现在产出清单",
          len(merged2) == 1 and merged2[0]["path"] == "docs/rep.md", repr(merged2))

    # ---------- E) 表格骨架逐行填：粒度与骨架一致 ----------
    # rerun32 零交付的根因：fill_section 只认 `## 小节`，表格骨架没有逐行入口，
    # 模型只能整篇 write_file，一次引用错误全篇作废。
    tbl = (root / "evals/gap.md").read_text(encoding="utf-8")
    one, err = skel.fill_row(tbl, "上下文压缩策略",
                             "s08 压缩后重放 | context.py 只截断 | 可借鉴")
    check("E 按首列定位行并逐格填满", err is None and "| 上下文压缩策略 | s08 压缩后重放 |"
          " context.py 只截断 | 可借鉴 |" in one, repr(err))
    check("E 填行后待填位少一个", one.count("（待填）") == tbl.count("（待填）") - 3,
          f"{tbl.count('（待填）')} → {one.count('（待填）')}")
    check("E 列数不足时报错且不动原文",
          skel.fill_row(tbl, "上下文压缩策略", "只有一格")[1] is not None
          and skel.fill_row(tbl, "上下文压缩策略", "只有一格")[0] == tbl)
    check("E 列数超出也报错", skel.fill_row(tbl, "上下文压缩策略", "a | b | c | d")[1]
          is not None)
    miss = skel.fill_row(tbl, "不存在的项", "a | b | c")
    check("E 首列不存在时报错并列出可用行",
          miss[0] == tbl and "可用的行" in (miss[1] or ""), repr(miss[1]))
    # 首列带括注时按归一化匹配
    check("E 括注差异也能定位同一行",
          skel.fill_row(tbl, "会话恢复", "a | b | c")[1] is None,
          repr(skel.fill_row(tbl, "会话恢复", "a | b | c")[1]))

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
