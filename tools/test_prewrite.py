"""写前策略（证据门槛 + 草稿暂存）的自测。

运行：python tools/test_prewrite.py
全部断言内建，退出码 0 = 全过。

背景：内容核查原先全在 write_file 内部——模型把整篇生成完之后才被打回，
代价是整篇重写（rerun23 连拒 4 次，每次三千字从头来）。本测试卡三件事：
1. 引用密集型内容 + 本任务零 read_file → 写之前就拒，并给出必读清单；
2. 拒绝时内容存进 .drafts/ 草稿，回传「改哪几行」，第二次明确禁止重写全文；
3. 正常场景不能误伤（读过一次就放行、引用少的普通文件不设门槛）；
4. 同一路径被拒 ≥2 次后，write_file 整篇重写被程序层拒收、只认草稿当前内容
   （rerun34：叮嘱版禁令被模型无视，连撞三次整篇重写）。
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

        # ---------- 5.5 重写禁令上移到程序层（rerun34 现场回归） ----------
        # rerun34 实录：回执里的「禁止重写全文」被无视，同一路径连撞三次整篇
        # write_file。现在 write_file 本身在 reject_count>=2 且草稿在时被拒收。
        out5b = wf3.run(path="evals/gap2.md", content=bad + "\n\n补一段新的。")
        check("被拒≥2次后整篇重写被程序层拒收",
              out5b.startswith("[错误] 这是同一路径第 2 次被拒"), out5b[:200])
        check("禁令给出两条出路", "edit_file" in out5b and "草稿" in out5b, out5b[:400])
        check("被禁重写没有落盘", "（内容未变" not in out5b)
        # 改草稿 → 原样提交：放行（仍走常规核查）
        for name in ("context.py", "permissions.py", "loop.py", "tasksession.py"):
            guard3.ledger.mark_file(tmp / "c" / "agentforge" / name, lines=502, read=True)
        (tmp / "c" / ".drafts" / "evals__gap2.md").write_text(DENSE, encoding="utf-8")
        out5c = wf3.run(path="evals/gap2.md", content=DENSE)
        check("提交内容=草稿当前内容 → 放行", out5c.startswith("已"), out5c[:200])
        check("成功写盘后拒绝计数清零",
              writepolicy.reject_count("evals/gap2.md") == 0,
              str(writepolicy.reject_count("evals/gap2.md")))

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

        # ---------- 9. 行号硬闸（rerun36 缺口：裸引用逃出核查射程） ----------
        # 19 处裸路径引用 0 拦截 → 模型学会最省力合规。现在引用密集型文档里
        # 裸路径升为硬错误，回执带已读区间（模型读过，补行号是抄不是猜）。
        cfg6, guard6, registry6 = setup(tmp / "f")
        wf6 = registry6.get("write_file")
        for name in ("context.py", "permissions.py", "loop.py", "tasksession.py"):
            guard6.ledger.mark_file(tmp / "f" / "agentforge" / name, lines=502,
                                    read=True, ranges=[(1, 502)])
        out9 = wf6.run(path="evals/lines.md", content=DENSE)
        check("引用密集且全部带可定位行号 → 放行", out9.startswith("已"), out9[:200])
        bare = (DENSE.replace(":190", "").replace(":363", "")
                .replace(":36", "").replace(":99", ""))
        out9b = wf6.run(path="evals/bare.md", content=bare)
        check("引用密集但裸路径 → 拒收",
              out9b.startswith("[错误] 引用核查未通过"), out9b[:200])
        check("拒收回执带已读区间", "你读过的区间：1-502" in out9b, out9b[:500])
        check("教模型改成 path:起-止 形式", "path:起-止" in out9b, out9b[:500])
        check("裸引用没有落盘", not (tmp / "f" / "evals" / "bare.md").exists())
        sparse = "# 笔记\n\n参考 `agentforge/context.py` 的实现。\n"
        out9c = wf6.run(path="notes/sparse.md", content=sparse)
        check("非密集文档裸引用不拦（checked<2）", out9c.startswith("已"), out9c[:150])

        # ---------- 10. 表格证据列闸门（表头即契约；rerun37 现场回归） ----------
        # rerun37：引用写成表格单元格里的裸文本，提取器抓到 0 条、核查链空转。
        # 修法：表头写明「证据/行号」的列逐行验可定位 path:行号，不靠提取器。
        cfg7, guard7, registry7 = setup(tmp / "g")
        wf7 = registry7.get("write_file")
        fs7 = registry7.get("fill_section")
        for name in ("context.py", "permissions.py", "loop.py", "tasksession.py"):
            guard7.ledger.mark_file(tmp / "g" / "agentforge" / name, lines=502,
                                    read=True, ranges=[(1, 502)])
        (tmp / "g" / "agentforge" / "extra.py").write_text("x = 1\n", encoding="utf-8")
        hdr = ("| 对比点 | 对方做法（附证据：仓库/文件/行号） | "
               "AgentForge 现状（附证据：文件/行号） | 建议 |\n| --- | --- | --- | --- |\n")
        good_row = ("| agent loop | `agentforge/loop.py:3-14` 状态机驱动 | "
                    "`agentforge/context.py:3-14` 台账 | 借鉴 |\n")
        out10 = wf7.run(path="evals/tbl.md", content="# 清单\n\n" + hdr + good_row)
        check("证据列带可定位行号 → 放行", out10.startswith("已"), out10[:200])
        bare_row = ("| 工具调用 | agentforge/permissions.py: 基于路径的权限控制 | "
                    "agentforge/toolselect.py: 动态匹配 | 借鉴 |\n")
        out10b = wf7.run(path="evals/tbl2.md", content="# 清单\n\n" + hdr + bare_row)
        check("证据列裸文本引用 → 拒收（rerun37 现场形状）",
              out10b.startswith("[错误] 证据核查未通过"), out10b[:300])
        check("拒收点名证据列并教格式", "对方做法" in out10b and "path:起-止" in out10b,
              out10b[:400])
        check("拒收回执直接带可锚定坐标（省一轮重读）",
              "可直接引用这些已读到的坐标" in out10b and "L1:" in out10b, out10b[:500])
        check("裸文本表没有落盘", not (tmp / "g" / "evals" / "tbl2.md").exists())
        unread_row = ("| 子代理 | `agentforge/extra.py:1-1` 隔离 | "
                      "`agentforge/nothere.py:1-5` 调度 | 借鉴 |\n")
        out10c = wf7.run(path="evals/tbl3.md", content="# 清单\n\n" + hdr + unread_row)
        check("整行无可定位锚点 → 上游引用核查即拒收",
              out10c.startswith("[错误]"), out10c[:400])
        relax_row = ("| 子代理 | agentforge/extra.py 的隔离描述（裸文本无行号） | "
                     "`agentforge/loop.py:3-14` 调度 | 借鉴 |\n")
        out10c2 = wf7.run(path="evals/tbl3b.md", content="# 清单\n\n" + hdr + relax_row)
        check("放宽口径：整行有 1 处可定位锚点即放行（另一列可裸文本）",
              out10c2.startswith("已"), out10c2[:250])
        out10d = wf7.run(path="evals/tbl4.md",
                         content="# 清单\n\n" + hdr +
                         good_row.replace("agentforge/loop.py:3-14",
                                          "agentforge/loop.py:9999")
                                 .replace("agentforge/context.py:3-14",
                                          "agentforge/context.py:9999"))
        check("两处行号都越界 → 拒收",
              "9999" in out10d and "超出" in out10d, out10d[:300])
        skel_tbl = hdr + "| agent loop | （待填） | （待填） | （待填） |\n"
        out10e = wf7.run(path="evals/tbl5.md", content="# 清单\n\n" + skel_tbl)
        check("骨架占位行 → 放行（交给未填判定）", out10e.startswith("已"), out10e[:200])
        plain_tbl = "| 优先级 | 建议项 | 预期收益 |\n| --- | --- | --- |\n| 1 | 压缩 | 省 token |\n"
        out10f = wf7.run(path="evals/tbl6.md", content="# 借鉴\n\n" + plain_tbl)
        check("无证据列表头 → 不约束", out10f.startswith("已"), out10f[:200])
        # fill_section 行级闸门
        wf7.run(path="evals/tbl7.md", content="# 清单\n\n" + hdr +
                "| agent loop | （待填） | （待填） | （待填） |\n")
        r10 = fs7.run(path="evals/tbl7.md", heading="agent loop",
                      content="`agentforge/loop.py:3-14` 状态机 | "
                              "`agentforge/context.py:3-14` 台账 | 借鉴")
        check("fill_section 行证据校验通过 → 已填充", "已填充表格行" in r10, r10[:200])
        r10b = fs7.run(path="evals/tbl7.md", heading="agent loop",
                       content="裸文本引用没有行号 | agentforge/toolselect.py: 动态 | 借鉴")
        check("fill_section 证据列裸文本 → 拒收本行",
              r10b.startswith("[错误] 证据核查未通过") and "对方做法" in r10b, r10b[:300])
        check("被拒行未写入（原内容还在）",
              "`agentforge/loop.py:3-14` 状态机" in
              (tmp / "g" / "evals" / "tbl7.md").read_text(encoding="utf-8"))

        # ---------- 11. shell 落盘拦截 + 收尾证据列核查（rerun39 现场回归） ----------
        # rerun39：模型被闸门拒绝后用 run_command cp 把无锚点草稿盖过写闸，
        # 交付 0 引用还全绿。现在登记交付物对 shell 关闭，收尾核查只看磁盘。
        import json as _json
        from agentforge import skeleton as _sk
        from agentforge import verify as _vg
        tp = tmp / "g" / "evals" / "tbl.md"
        sp = _sk.state_path(tmp / "g", "evals/tbl.md")
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(_json.dumps({"path": str(tp.resolve())}), encoding="utf-8")
        rc = registry7.get("run_command")
        out11 = rc.run(command="cp .drafts/x.md evals/tbl.md")
        check("cp 到登记交付物 → 拦截",
              out11.startswith("[错误] shell 落盘已被拦"), out11[:200])
        out11b = rc.run(command="echo hi > evals/tbl.md")
        check("重定向到登记交付物 → 拦截",
              out11b.startswith("[错误] shell 落盘已被拦"), out11b[:200])
        out11c = rc.run(command="cp evals/tbl.md evals/backup.md")
        check("登记文件作为来源复制 → 放行",
              not out11c.startswith("[错误] shell 落盘已被拦"), out11c[:150])
        (tmp / "g" / "evals" / "tbl8.md").write_text(
            "# 清单\n\n" + hdr + bare_row, encoding="utf-8")
        issues11 = _vg.find_artifact_issues([("evals/tbl8.md", True)], tmp / "g")
        check("收尾核查：无锚点表格 → status=evidence",
              len(issues11) == 1 and issues11[0]["status"] == "evidence",
              str(issues11))
        check("收尾核查声明给修正指引",
              "path:起-止" in _vg.issue_line(issues11[0]), _vg.issue_line(issues11[0]))
        (tmp / "g" / "evals" / "tbl8.md").write_text(
            "# 清单\n\n" + hdr + good_row, encoding="utf-8")
        issues11b = _vg.find_artifact_issues([("evals/tbl8.md", True)], tmp / "g")
        check("收尾核查：证据列有可定位锚点 → 通过", not issues11b, str(issues11b))

        # ---------- 11.5 契约表头不可整篇换掉（rerun42 现场回归） ----------
        sp2 = _sk.state_path(tmp / "g", "evals/tbl9.md")
        sp2.parent.mkdir(parents=True, exist_ok=True)
        sp2.write_text(_json.dumps({
            "path": str((tmp / "g" / "evals" / "tbl9.md").resolve()),
            "header": ["对比点", "对方做法（附证据：仓库/文件/行号）",
                       "AgentForge 现状（附证据：文件/行号）", "建议"],
        }), encoding="utf-8")
        out115a = wf7.run(path="evals/tbl9.md",
                          content="# 自由格式\n\n## 对比分析\n\n随便写的内容。\n")
        check("登记文件整篇换掉表头 → 拒收",
              out115a.startswith("[错误] 契约结构核查未通过"), out115a[:250])
        check("拒收指名丢失的表头列", "对方做法" in out115a, out115a[:300])
        out115b = wf7.run(path="evals/tbl9.md",
                          content="# 清单\n\n" + hdr + good_row)
        check("保留契约表头的重写 → 放行", out115b.startswith("已"), out115b[:200])

        # ---------- 12. 锚点前置：预热注入 + read 回执（坐标在开局/读时就给到） ----------
        from agentforge import warmup as _wu
        (tmp / "g" / "agentforge" / "sample_mod.py").write_text(
            "class Sample:\n"
            "    def run(self):\n"
            "        pass\n", encoding="utf-8")
        block, _wstats = _wu.build_block(cfg7, "分析 sample_mod 的 run 方法")
        check("预热注入的证据候选带锚点坐标",
              "证据候选" in block and "L1: class Sample" in block, block[-500:])
        r12 = registry7.get("read_file").run(path="agentforge/sample_mod.py")
        check("read_file 回执尾部带可锚定行",
              "【可锚定行】" in r12 and "L1: class Sample" in r12, r12[-300:])

    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
