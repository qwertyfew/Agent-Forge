"""骨架先行（write_skeleton / fill_section）的自测。

运行：python tools/test_skeleton.py
全部断言内建，退出码 0 = 全过。

卡四件事：
1. 结构判定纯粹看内容：只有标题 + 空正文 = 未填完；有正文 = 填过；
   代码块里的 `#` 不算标题；单个空小节不误伤长文档。
2. fill 的语义：replace 替换本节、append 续写本节、标题容错匹配、找不到不写。
3. 失败成本只有一节：后一节被核查拒，前一节已填的内容原样保留。
4. 闸门认这个状态：骨架未填完在产物核查里是 status=skeleton（不是「本轮已产出」），
   填完之后才算通过；未交付硬闸会把它判成「未交付」而不是「写错地方」。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import skeleton                          # noqa: E402
from agentforge import verify as vg                      # noqa: E402
from agentforge.config import Config                     # noqa: E402
from agentforge.evidence import EvidenceLedger           # noqa: E402
from agentforge.permissions import Guard                 # noqa: E402
from agentforge.tools import build_registry              # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


OUTLINE = """# 差距清单

## 1. 结论
一句话说清这份清单要回答什么。

## 2. 压缩
这里写压缩链路的现状与差距。

## 3. 权限
这里写权限链路的现状与差距。
"""

# 纯结构（不带一行意图）：用来测「纯内容判定」这一层
BARE = "# 差距清单\n\n## 1. 结论\n\n## 2. 压缩\n\n## 3. 权限\n"


def setup(tmp: Path):
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.state_dir = tmp / ".agentforge"
    (tmp / "agentforge").mkdir(parents=True, exist_ok=True)
    guard = Guard(cfg, approver=None)
    guard.ledger = EvidenceLedger()
    # 先真读过一个文件：这样证据门槛放行，引用核查才会真正跑起来
    # （下面要测的是「引用错了 → 只丢这一节」，不是「没读过 → 拒写」）
    ctx = tmp / "agentforge" / "context.py"
    ctx.write_text("class Compressor:\n    pass\n", encoding="utf-8")
    guard.ledger.mark_file(ctx, lines=2, read=True)
    registry = build_registry(guard, cfg)
    return cfg, guard, registry


def main() -> int:
    # ---------- 1. 纯文本判定 ----------
    check("只有标题的骨架 → 未填完", skeleton.unfinished("# 报告\n\n## 一\n\n## 二\n"))
    check("标题 + 正文 → 已填完",
          not skeleton.unfinished("# 报告\n\n## 一\n内容在这里。\n\n## 二\n也有内容。\n"))
    check("占位符不算正文",
          skeleton.unfinished("# 报告\n\n## 一\nTODO\n\n## 二\n待补充\n"), )
    check("单条摘要占位也不算",
          skeleton.unfinished("## 一\n## 二\n## 三\n（待填）\n"))
    deep = ("# 长报告\n\n## 一\n" + "正文" * 200 + "\n\n## 二\n"
            + "正文" * 50 + "\n\n## 三\n\n## 四\n" + "正文" * 50 + "\n")
    check("长文档里单个空小节不误判", not skeleton.unfinished(deep),
          skeleton.status_text(deep))
    fence = "# 报告\n\n## 一\n```bash\n# 这是注释不是标题\n```\n\n## 二\n正文\n"
    check("代码块里的 # 不算标题",
          [s.heading for s in skeleton.sections(fence)] == ["报告", "一", "二"],
          str([s.heading for s in skeleton.sections(fence)]))
    check("列表符号不构成正文", skeleton.unfinished("## 一\n- \n- \n\n## 二\n- \n"))

    # ---------- 2. fill 语义 ----------
    filled, err = skeleton.fill(BARE, "1. 结论", "这是结论段。\n\n- 要点一")
    check("按标题精确填充", err is None and "这是结论段。" in filled, str(err))
    check("其他小节不受影响", "## 2. 压缩" in filled)
    check("填完的小节变成有正文",
          "1. 结论" not in skeleton.pending(filled))
    check("没填的小节仍在待填清单", "2. 压缩" in skeleton.pending(filled),
          str(skeleton.pending(filled)))

    filled2, err2 = skeleton.fill(filled, "结论", "追加的一段。", mode="append")
    check("标题容错：省略序号也能命中", err2 is None and "追加的一段。" in filled2, str(err2))
    check("append 保留原文", "这是结论段。" in filled2)
    check("append 不会插到下一节", filled2.index("追加的一段。") < filled2.index("## 2. 压缩"))

    filled3, err3 = skeleton.fill(BARE, "3. 权限", "新内容", mode="replace")
    check("replace 只动这一节", "新内容" in filled3 and "## 2. 压缩" in filled3, filled3)
    _, err4 = skeleton.fill(BARE, "不存在的节", "x")
    check("找不到小节 → 报错且列出可用", err4 and "可用小节" in err4, str(err4))
    _, err5 = skeleton.fill(BARE, "1. 结论", "   ")
    check("空内容 → 报错", err5 is not None, str(err5))
    check("标题带 # 也能命中", skeleton.fill(BARE, "## 2. 压缩", "x")[1] is None)

    # ---------- 2.5 表格逐行（fill_row；rerun34 现场回归） ----------
    TABLE = ("# 差距清单\n\n"
             "| 对比点 | 对方做法 | 现状 | 建议 |\n| --- | --- | --- | --- |\n"
             "| 权限与沙箱 | （待填） | （待填） | （待填） |\n"
             "| 会话恢复 | （待填） | （待填） | （待填） |\n")
    row, err = skeleton.fill_row(TABLE, "权限与沙箱", "证据A | 现状B | 建议C")
    check("表格行按其余格填充，首列保留",
          err is None and "| 权限与沙箱 | 证据A | 现状B | 建议C |" in row, str(err))
    # rerun35 现场：模型给「其余格」也包上首尾 `|`——按格数消歧，应按其余格
    # 解释、首列保留（rerun34 的坏行就是被这个形状诱发，现在直接填对）。
    row2, err2 = skeleton.fill_row(TABLE, "权限与沙箱", "| 证据A | 现状B | 建议C |")
    check("带首尾管道的其余格 → 正确填充且首列保留",
          err2 is None and "| 权限与沙箱 | 证据A | 现状B | 建议C |" in row2, str(err2))
    full_ok = "| 权限与沙箱 | 证据A | 现状B | 建议C |"
    row3, err3 = skeleton.fill_row(TABLE, "权限与沙箱", full_ok)
    check("整行带全首列 → 接受", err3 is None and full_ok in row3, str(err3))
    row4, err4 = skeleton.fill_row(TABLE, "权限与沙箱",
                                   full_ok.replace("权限与沙箱", "别的名字"))
    check("整行改掉首列 → 拒收（行首列不许删改）",
          err4 is not None and "首列" in err4 and row4 == TABLE, str(err4))
    row5, err5 = skeleton.fill_row(TABLE, "权限与沙箱", "| A | B | C | D |")
    check("格数对不上（多一列的管道形式）→ 拒收且文件不动",
          err5 is not None and row5 == TABLE, str(err5))

    # ---------- 3. 骨架合法性 ----------
    check("单节骨架被拒", bool(skeleton.outline_error("# 报告\n\n正文一句话\n")),
          str(skeleton.outline_error("# 报告\n\n正文一句话\n")))
    check("超长骨架被拒", bool(skeleton.outline_error(
        "# 报告\n\n## 一\n" + "内容" * 1500 + "\n\n## 二\n一句话\n")))
    fat = "# 报告\n\n## 一\n" + "很长的正文" * 60 + "\n\n## 二\n一句话\n"
    check("某节正文过长被拒（那不是骨架）", bool(skeleton.outline_error(fat)))
    check("正常骨架通过", skeleton.outline_error(OUTLINE) is None,
          str(skeleton.outline_error(OUTLINE)))
    check("围栏里的 # 不算节数", bool(skeleton.outline_error(
        "# 报告\n\n```\n## 假标题\n## 又一个\n```\n")))

    # ---------- 4. 工具层：骨架 + 分段填充 ----------
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg, guard, registry = setup(tmp)
        ws = registry.get("write_skeleton")
        fs = registry.get("fill_section")
        check("两个工具都已注册", ws is not None and fs is not None)

        bad = ws.run(path="evals/gap.md", outline="# 只有标题\n")
        check("单节骨架在工具层被拒", bad.startswith("[错误]"), bad[:120])

        out = ws.run(path="evals/gap.md", outline=OUTLINE)
        target = tmp / "evals" / "gap.md"
        check("骨架已落盘", target.is_file(), out[:200])
        check("回执列出小节", "1. 结论" in out and "3. 权限" in out, out[:300])
        check("回执点明骨架不算交付", "不算交付" in out, out[:300])
        pr0 = skeleton.progress(tmp, target)
        check("骨架状态：0/3 已填（一行意图不算填过）", pr0["filled"] == 0,
              str(pr0))

        r1 = fs.run(path="evals/gap.md", heading="1. 结论", content="结论写在这里。")
        check("填第一节成功", "已填充" in r1, r1[:200])
        check("回执带进度", "1/3" in r1, r1[:200])
        body = target.read_text("utf-8")
        check("文件里出现第一节正文", "结论写在这里。" in body)
        pr1 = skeleton.progress(tmp, target)
        check("其余小节仍算待填", pr1["pending"] == ["2. 压缩", "3. 权限"], str(pr1))
        check("此时仍是「骨架未填完」", skeleton.unfinished_file(tmp, target))

        # 第二节被引用核查拒：只丢这一节
        badbody = ("见 `agentforge/context.py:190`、`agentforge/permissions.py:36`、"
                   "`agentforge/loop.py:363` 三处，路径都不存在。")
        r2 = fs.run(path="evals/gap.md", heading="2. 压缩", content=badbody)
        check("引用核查拒收本节", r2.startswith("[错误]"), r2[:200])
        after = target.read_text("utf-8")
        check("被拒后第一节仍在", "结论写在这里。" in after)
        check("被拒后第二节仍算待填", "2. 压缩" in skeleton.progress(tmp, target)["pending"],
              str(skeleton.progress(tmp, target)))
        check("被拒内容进草稿", any((tmp / ".drafts").glob("*gap*")),
              str(list((tmp / ".drafts").glob("*"))))

        r3 = fs.run(path="evals/gap.md", heading="2. 压缩", content="压缩这一段正文。")
        check("同一节重填成功", "已填充" in r3, r3[:200])
        long_body = "第一节的开头。" + "\n\n" + ("细节段落。" * 60)
        r4 = fs.run(path="evals/gap.md", heading="2. 压缩", content=long_body, mode="append")
        check("append 续写同一节", "已填充" in r4, r4[:200])
        merged = target.read_text("utf-8")
        check("append 后旧正文还在", "压缩这一段正文。" in merged and "第一节的开头。" in merged)
        check("append 不落到下一节", merged.index("第一节的开头。") < merged.index("## 3. 权限"))

        miss = fs.run(path="evals/gap.md", heading="9. 不存在", content="x")
        check("填不存在的小节 → 报错不改文件", miss.startswith("[错误]") and
              target.read_text("utf-8") == merged, miss[:120])

        # 表格骨架：fill_section 未找到小节时回退按行填（rerun32/34 的现场）
        tbl = ("# 差距清单\n\n"
               "| 对比点 | 对方做法 | 现状 | 建议 |\n| --- | --- | --- | --- |\n"
               "| 权限与沙箱 | （待填） | （待填） | （待填） |\n")
        registry.get("write_file").run(path="evals/table.md", content=tbl)
        check("表格骨架已落盘", (tmp / "evals" / "table.md").is_file())
        tr = fs.run(path="evals/table.md", heading="权限与沙箱",
                    content="证据A | 现状B | 建议C")
        check("fill_section 表格行回退成功", "已填充表格行" in tr, tr[:200])
        tbody = (tmp / "evals" / "table.md").read_text("utf-8")
        check("表格行内容与首列都在",
              "| 权限与沙箱 | 证据A | 现状B | 建议C |" in tbody, tbody)
        tr2 = fs.run(path="evals/table.md", heading="权限与沙箱",
                     content="| 证据A2 | 现状B2 | 建议C2 |")
        check("表格行带首尾管道 → 工具层正确填充", "已填充表格行" in tr2, tr2[:200])
        check("填充后首列保留、内容落位",
              "| 权限与沙箱 | 证据A2 | 现状B2 | 建议C2 |" in
              (tmp / "evals" / "table.md").read_text("utf-8"))
        tr3 = fs.run(path="evals/table.md", heading="权限与沙箱", content="只有一格")
        check("行回退的具体报错要透传（不能被「未找到小节」盖掉）",
              tr3.startswith("[错误]") and "单元格" in tr3 and "可用小节" not in tr3,
              tr3[:200])

        fs.run(path="evals/gap.md", heading="3. 权限", content="权限这一段正文。")
        done = target.read_text("utf-8")
        prd = skeleton.progress(tmp, target)
        check("三节填完 → 不再是未完成", not skeleton.unfinished_file(tmp, target), str(prd))
        check("骨架进度 3/3", "3/3" in skeleton.progress_text(tmp, target),
              skeleton.progress_text(tmp, target))

        # ---------- 5. 闸门认这个状态 ----------
        ws.run(path="evals/half.md", outline=OUTLINE)
        skel_only = tmp / "evals" / "half.md"
        now = time.time()
        check("半成品确实是骨架状态", skeleton.unfinished_file(tmp, skel_only))
        issues = vg.find_artifact_issues(
            [("evals/half.md", True)], tmp, produced=True,
            task_start_ts=now - 30)          # mtime 比任务开始新——旧判定会放过
        check("骨架未填 → 产物核查判 skeleton",
              len(issues) == 1 and issues[0]["status"] == "skeleton", str(issues))
        check("骨架状态有可读说明", "骨架" in vg.issue_line(issues[0]),
              vg.issue_line(issues[0]))
        check("骨架未填 → artifact_fixed 为假",
              vg.artifact_fixed(issues[0], {"evals/half.md": 0.0}, tmp) is False)

        vg_txt = vg.delivery_failed_block(["evals/half.md"], skeleton_only=True)
        check("未交付声明点明「只写了骨架」", "只写了骨架" in vg_txt, vg_txt[:160])
        check("未交付声明不再说「没有任何写盘动作」",
              "没有任何写盘动作" not in vg_txt, vg_txt[:160])

        skel_only.write_text(done, encoding="utf-8")
        issues2 = vg.find_artifact_issues(
            [("evals/half.md", True)], tmp, produced=True,
            task_start_ts=now - 30)
        check("填完之后不再有问题", not issues2, str(issues2))
        check("填完之后 artifact_fixed 为真",
              vg.artifact_fixed({"path": "evals/half.md", "status": "skeleton"},
                                {"evals/half.md": 0.0}, tmp) is True)

        # 修正轮 prompt 要教它填而不是重写
        check("修正轮提示提到 fill_section",
              "fill_section" in vg.ARTIFACT_CORRECTIVE_PROMPT)

    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
