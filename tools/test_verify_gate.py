"""收尾验证闸门的自测：verify.py 的提取/执行/核查 + orchestrator._verify_gate 的四条路径。

运行：python tools/test_verify_gate.py
全部断言内建，退出码 0 = 全过。这是纯程序测试，不需要模型在线。
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import verify as vg                     # noqa: E402
from agentforge.orchestrator import _verify_gate        # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_verify_"))
    # 造一个真实验证脚本：通过 / 失败两用
    (tmp / "ok_script.py").write_text("print('all good')\n", encoding="utf-8")
    (tmp / "bad_script.py").write_text("import sys\nprint('boom')\nsys.exit(3)\n",
                                       encoding="utf-8")

    # ---------- extract_verify_cmd ----------
    brief = ("任务：修复 tools/x.py 的调用约定问题\n"
             "步骤：1.改 fs.py 2.修测试脚本\n"
             "验收：python ok_script.py 全过，不许谎报\n")
    cmd = vg.extract_verify_cmd(brief, tmp)
    check("提取声明行命令（去掉人话尾巴）", cmd == "python ok_script.py", repr(cmd))

    brief2 = "改完之后跑一下 `python bad_script.py` 确认\n"
    cmd2 = vg.extract_verify_cmd(brief2, tmp)
    check("兜底扫描反引号命令行", cmd2 == "python bad_script.py", repr(cmd2))

    brief3 = "验收：python 不存在的脚本.py 全过"
    check("脚本不存在时拒绝提取", vg.extract_verify_cmd(brief3, tmp) == "")

    brief4 = "验收：rm -rf ok_script.py"
    check("破坏性命令拒绝提取", vg.extract_verify_cmd(brief4, tmp) == "")

    check("无声明返回空", vg.extract_verify_cmd("写个报告", tmp) == "")

    # ---------- run_verify_cmd ----------
    res = vg.run_verify_cmd("python ok_script.py", tmp)
    check("通过脚本 → ok", res["ok"] and res["code"] == 0, str(res))
    res = vg.run_verify_cmd("python bad_script.py", tmp)
    check("失败脚本 → 退出码 3 且非 ok",
          (not res["ok"]) and res["code"] == 3 and "boom" in res["output"], str(res))

    # ---------- claimed_verified / ran_verification ----------
    check("识别谎报措辞", vg.claimed_verified("修复完成，所有断言验证通过"))
    check("识别英文措辞", vg.claimed_verified("done, all tests passed"))
    check("中性总结不误伤", not vg.claimed_verified("已写好报告，未运行测试"))
    convo_no = [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {}}}]}]
    convo_yes = [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "run_command", "arguments": {}}}]}]
    check("没跑过 run_command", not vg.ran_verification(convo_no))
    check("跑过 run_command", vg.ran_verification(convo_yes))

    # ---------- report_block ----------
    ok_block = vg.report_block("python ok_script.py", res_ok := vg.run_verify_cmd("python ok_script.py", tmp))
    check("通过报告不含输出节选", "通过" in ok_block and "节选" not in ok_block)
    res_bad = vg.run_verify_cmd("python bad_script.py", tmp)
    bad_block = vg.report_block("python bad_script.py", res_bad)
    check("失败报告含输出节选", "失败" in bad_block and "boom" in bad_block)

    # ---------- _verify_gate 四条路径（stub agent，不需要模型） ----------
    class StubCfg:
        workspace_root = str(tmp)
        verify_gate = True

    class StubAgent:
        cfg = StubCfg()
        corrective_calls = 0

        def run(self, convo, prompt, **kw):
            # 模拟真实 agent：把用户消息追加进历史，回一条 assistant
            StubAgent.corrective_calls += 1
            assert ("系统侧验证结果" in prompt) or ("系统侧核查结果" in prompt), prompt[:80]
            hist = [*convo, {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "修正后的总结：验证实际失败"}]
            yield {"type": "assistant", "content": "修正后的总结：验证实际失败",
                   "history": hist}

    # A) 验证通过 → 只附报告，不触发修正轮
    ag = StubAgent()
    holder = {"summary": "做完了，一切正常", "convo": [{"role": "user", "content": "验收：python ok_script.py"}]}
    evs = list(_verify_gate(ag, "任务……验收：python ok_script.py", holder))
    kinds = [e["type"] for e in evs]
    check("A 通过：发 verify 事件", "verify" in kinds, str(kinds))
    check("A 通过：不触发修正轮", StubAgent.corrective_calls == 0)
    check("A 通过：总结附系统报告", "【系统侧验证】" in holder["summary"] and "通过" in holder["summary"],
          holder["summary"])

    # B) 验证失败 → 触发修正轮，采纳修正总结，报告标注失败
    ag = StubAgent()
    holder = {"summary": "我做完了，验证通过", "convo": []}
    evs = list(_verify_gate(ag, "任务……验收：python bad_script.py", holder))
    check("B 失败：触发一轮修正", StubAgent.corrective_calls == 1)
    check("B 失败：采纳修正总结", "修正后的总结" in holder["summary"], holder["summary"])
    check("B 失败：报告标注失败", "失败" in holder["summary"] and "boom" in holder["summary"],
          holder["summary"])
    check("B 失败：修正轮历史并入", any(m.get("role") == "assistant" and
          "修正后的总结" in str(m.get("content")) for m in holder["convo"]))

    # C) 无验证命令 + 谎报通过 + 全程没跑命令 → 追加未验证声明
    ag = StubAgent()
    holder = {"summary": "全部完成，验证通过", "convo": [{"role": "user", "content": "x"}]}
    evs = list(_verify_gate(ag, "帮我写个总结", holder))
    check("C 谎报：附未验证声明", "未经系统确认" in holder["summary"], holder["summary"])

    # D) 无验证命令 + 如实总结 → 不追加任何东西
    ag = StubAgent()
    holder = {"summary": "完成了，未运行验证", "convo": []}
    list(_verify_gate(ag, "帮我写个总结", holder))
    check("D 如实：总结原样", holder["summary"] == "完成了，未运行验证")

    # E) 开关关闭 → 完全不介入
    class OffCfg(StubCfg):
        verify_gate = False

    class OffAgent(StubAgent):
        cfg = OffCfg()

    holder = {"summary": "验证通过", "convo": []}
    evs = list(_verify_gate(OffAgent(), "验收：python ok_script.py", holder))
    check("E 开关关：零事件零改写", evs == [] and holder["summary"] == "验证通过")

    # ---------- 声明产物存在性核查：提取 ----------
    (tmp / "sub").mkdir(exist_ok=True)
    (tmp / "sub" / "out.json").write_text("{}\n", encoding="utf-8")

    brief_a = ("任务：调研并评估项目 X\n"
               "4. 报告写入 docs/REA_EVAL_AGENT.md（含数据表与来源链接），回复给出结论摘要\n"
               "5. 明细产出文件：sub/out.json\n"
               "6. 禁止写入 src/config.yaml\n")
    dec = vg.extract_declared_artifacts(brief_a)
    check("P1 提取动词式+冒号式声明、禁令不误收",
          dec == ["docs/REA_EVAL_AGENT.md", "sub/out.json"], repr(dec))

    check("P2 无声明返回空表", vg.extract_declared_artifacts("写个总结就好") == [])

    # ---------- 声明产物存在性核查：总结声明 ----------
    s1 = "任务完成。报告已写入 docs/r.md，并已生成 out/summary.json。"
    check("P3 完成式声明全抓到",
          vg.claimed_artifacts(s1) == ["docs/r.md", "out/summary.json"],
          repr(vg.claimed_artifacts(s1)))
    s2 = "接下来可写入 x.md；建议将报告写入 docs/x.md；我读取了 config.yaml。"
    check("P4 将来时/转述/读取不误伤", vg.claimed_artifacts(s2) == [],
          repr(vg.claimed_artifacts(s2)))
    check("P5 英文完成式", vg.claimed_artifacts("report written to docs/a.md")
          == ["docs/a.md"], repr(vg.claimed_artifacts("report written to docs/a.md")))

    # ---------- 声明产物存在性核查：exists 判定 ----------
    miss = vg.find_missing_artifacts(
        [("sub/out.json", True),          # 存在 → 不缺
         ("docs/ghost.md", True),         # 缺失 → 报
         ("docs/ghost.md", False),        # 同路径去重
         ("../escape.md", True),          # 逃出工作区 → 不核查
         (str(tmp.parent / "outside.md"), True)],  # 工作区外绝对路径 → 不核查
        tmp)
    check("P6 只报工作区内缺失文件",
          miss == [{"path": "docs/ghost.md", "declared": True}], repr(miss))

    block = vg.artifact_missing_block(miss)
    check("P7 核查声明标注交付属性", "docs/ghost.md" in block and "任务书明确要求" in block)

    # ---------- 闸门路径 F：声明的文件缺失 → 修正轮真写文件 → 复查通过不附声明 ----------
    class FixingAgent(StubAgent):
        """模拟「听劝」的 agent：修正轮里真的把文件写出来。"""

        def run(self, convo, prompt, **kw):
            FixingAgent.corrective_calls += 1
            seg = prompt.split("【你此前的总结】")[0]
            # 问题行格式：- `path`（标签；状态说明），抓反引号里的路径
            for m in re.finditer(r"`([^`]+)`", seg):
                f = Path(StubCfg.workspace_root) / m.group(1).strip()
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("real content\n", encoding="utf-8")
            text = "已补写文件"
            return iter([{"type": "assistant", "content": text,
                          "history": [*convo, {"role": "assistant", "content": text}]}])

    brief_g = "任务：写评估报告，报告写入 docs/real_report.md，回复给出摘要"
    ag = FixingAgent()
    FixingAgent.corrective_calls = 0
    holder = {"summary": "报告已写入 docs/real_report.md", "convo": []}
    evs = list(_verify_gate(ag, brief_g, holder))
    check("F 缺失：触发修正轮", FixingAgent.corrective_calls == 1)
    check("F 缺失：修正后文件真存在",
          (tmp / "docs" / "real_report.md").is_file())
    check("F 缺失：复查通过不附核查声明",
          "【系统侧核查】" not in holder["summary"], holder["summary"])
    check("F 缺失：不发产物 verify 事件",
          not any(e["type"] == "verify" and e.get("artifacts") for e in evs))

    # ---------- 闸门路径 G：修正轮仍不写 → 附系统侧核查声明 ----------
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "搞定了，报告已写入 docs/ghost2.md", "convo": []}
    evs = list(_verify_gate(ag, "任务：产出评估报告，写入 docs/ghost2.md", holder))
    check("G 谎报：仍触发一轮修正", StubAgent.corrective_calls == 1)
    art_evs = [e for e in evs if e["type"] == "verify" and e.get("artifacts")]
    check("G 谎报：发产物 verify 事件", len(art_evs) == 1
          and art_evs[0]["artifacts"] == [{"path": "docs/ghost2.md",
                                           "declared": True, "status": "missing"}],
          repr(art_evs))
    check("G 谎报：总结附系统侧核查声明",
          "【系统侧核查】" in holder["summary"] and "docs/ghost2.md" in holder["summary"]
          and "任务书明确要求的交付物" in holder["summary"], holder["summary"])

    # ---------- 闸门路径 H：仅总结声称（任务书没声明）缺失 → 同样核查 ----------
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "完成了，总结已保存到 solo_notes.md", "convo": []}
    evs = list(_verify_gate(ag, "帮我写个总结", holder))
    check("H 仅声称：同样触发核查并附声明",
          StubAgent.corrective_calls == 1
          and "solo_notes.md" in holder["summary"]
          and "总结声称已写入" in holder["summary"], holder["summary"])

    # ---------- 闸门路径 I：声明的文件真实存在且任务确实写过 → 不介入 ----------
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    convo_w = [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "write_file", "arguments": {}}}]}]
    holder = {"summary": "明细已写入 sub/out.json，全部完成", "convo": convo_w}
    evs = list(_verify_gate(ag, "任务：产出明细，产出文件：sub/out.json", holder))
    check("I 真存在且写过：零介入",
          StubAgent.corrective_calls == 0 and evs == []
          and holder["summary"] == "明细已写入 sub/out.json，全部完成")

    # ---------- markdown 强调符不再挡声明提取（REA 三测盲区 2） ----------
    s3 = "完成。报告已写入：**`docs/REA_EVAL_AGENT.md`**，请查阅。"
    check("Q1 中文加粗+反引号不挡提取",
          vg.claimed_artifacts(s3) == ["docs/REA_EVAL_AGENT.md"],
          repr(vg.claimed_artifacts(s3)))
    check("Q2 英文加粗不挡提取",
          vg.claimed_artifacts("report written to **`docs/b.md`**")
          == ["docs/b.md"], repr(vg.claimed_artifacts("report written to **`docs/b.md`**")))
    brief_md = "任务：写报告，报告写入 **docs/md_bold.md**，回复给摘要"
    check("Q3 任务书声明同样容忍加粗",
          vg.extract_declared_artifacts(brief_md) == ["docs/md_bold.md"],
          repr(vg.extract_declared_artifacts(brief_md)))

    # ---------- 写操作核查（ghost 判定依据） ----------
    convo_edit = [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "edit_file", "arguments": {}}}]}]
    check("R1 识别 write_file", vg.ran_writes(convo_w))
    check("R2 识别 edit_file", vg.ran_writes(convo_edit))
    check("R3 只读历史不算写", not vg.ran_writes(convo_no))
    check("R4 run_command 也算可能产出", vg.produced_possible(convo_yes))
    check("R5 纯只读任务不可能产出", not vg.produced_possible(convo_no))

    # ---------- mtime 起点快照 ----------
    (tmp / "docs").mkdir(exist_ok=True)
    (tmp / "docs" / "snap_a.md").write_text("v1\n", encoding="utf-8")
    snap = vg.snapshot_artifacts("报告写入 docs/snap_a.md，明细写入 docs/snap_b.md", tmp)
    check("S1 快照只记已存在的声明文件",
          list(snap) == ["docs/snap_a.md"], repr(snap))
    check("S2 快照值是正数 mtime", snap["docs/snap_a.md"] > 0)

    # ---------- 三类问题判定 ----------
    (tmp / "docs" / "ghost_exist.md").write_text("old content\n", encoding="utf-8")
    (tmp / "docs" / "fresh.md").write_text("new\n", encoding="utf-8")
    issues = vg.find_artifact_issues(
        [("docs/snap_a.md", True),        # 存在 + 快照 mtime 未变 → stale
         ("docs/nope.md", True),          # 不存在 → missing
         ("docs/fresh.md", False),        # 存在、非声明、produced=True → 无问题
         ("docs/ghost_exist.md", False),  # 存在、声称写入、produced=True → 不冤枉
         ("../escape.md", True),          # 逃出工作区 → 不核查
         (str(tmp.parent / "outside.md"), True)],  # 工作区外 → 不核查
        tmp, snapshot=snap, produced=True)
    got = {(i["path"], i["status"]) for i in issues}
    check("T1 stale/missing 各就各位，有写操作不判 ghost",
          got == {("docs/snap_a.md", "stale"), ("docs/nope.md", "missing")},
          repr(got))

    issues_g = vg.find_artifact_issues([("docs/ghost_exist.md", False)], tmp,
                                       produced=False)
    check("T1b 零写操作才判 ghost",
          issues_g == [{"path": "docs/ghost_exist.md", "declared": False,
                        "status": "ghost"}], repr(issues_g))

    issues2 = vg.find_artifact_issues([("docs/ghost_exist.md", False)], tmp,
                                      produced=None)
    check("T2 produced 未知时跳过 ghost", issues2 == [], repr(issues2))

    time.sleep(0.02)   # 保证 mtime 前进
    (tmp / "docs" / "snap_a.md").write_text("v2\n", encoding="utf-8")
    issues3 = vg.find_artifact_issues([("docs/snap_a.md", True)], tmp,
                                      snapshot=snap, produced=False)
    check("T3 快照后文件变过 → 不算 stale 也不算 ghost", issues3 == [], repr(issues3))

    (tmp / "docs" / "new_born.md").write_text("born this task\n", encoding="utf-8")
    issues4 = vg.find_artifact_issues([("docs/new_born.md", True)], tmp,
                                      snapshot=snap, produced=False)
    check("T4 快照里没有 → 本轮新建 → 不误判", issues4 == [], repr(issues4))

    # ---------- path_mtime / artifact_fixed（修正轮复查） ----------
    pm = vg.path_mtime("docs/snap_a.md", tmp)
    check("U1 path_mtime 取到值", pm is not None and pm > 0)
    check("U2 未修复：mtime 没变", not vg.artifact_fixed(
        {"path": "docs/snap_a.md", "status": "stale"},
        {"docs/snap_a.md": pm}, tmp))
    time.sleep(0.02)
    (tmp / "docs" / "snap_a.md").write_text("v3\n", encoding="utf-8")
    check("U3 已修复：mtime 变了", vg.artifact_fixed(
        {"path": "docs/snap_a.md", "status": "stale"},
        {"docs/snap_a.md": pm}, tmp))
    check("U4 missing 修复=文件出现", vg.artifact_fixed(
        {"path": "docs/now_exists.md", "status": "missing"}, {}, tmp) is False)
    (tmp / "docs" / "now_exists.md").write_text("x\n", encoding="utf-8")
    check("U5 missing 修复后判定通过", vg.artifact_fixed(
        {"path": "docs/now_exists.md", "status": "missing"}, {}, tmp))
    check("U6 文件不存在 → 未修好", not vg.artifact_fixed(
        {"path": "docs/late.md", "status": "ghost"},
        {"docs/late.md": None}, tmp))
    (tmp / "docs" / "late.md").write_text("late\n", encoding="utf-8")
    check("U7 pre 为 None 但现在存在 → 算修好", vg.artifact_fixed(
        {"path": "docs/late.md", "status": "ghost"},
        {"docs/late.md": None}, tmp))

    block_i = vg.artifact_issue_block(
        [{"path": "docs/snap_a.md", "declared": True, "status": "stale"}])
    check("T5 核查声明带状态说明",
          "docs/snap_a.md" in block_i and "本轮从未更新" in block_i
          and "【系统侧核查】" in block_i, block_i)

    # ---------- 闸门路径 V：吃老本（声明文件是上轮旧文件，本轮没碰） ----------
    (tmp / "docs" / "old_report.md").write_text("stale v1\n", encoding="utf-8")
    snap_v = vg.snapshot_artifacts("报告写入 docs/old_report.md", tmp)
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "评估报告已写入 docs/old_report.md", "convo": []}
    evs = list(_verify_gate(ag, "任务：更新评估报告，报告写入 docs/old_report.md",
                            holder, snapshot=snap_v))
    check("V1 吃老本：触发修正轮", StubAgent.corrective_calls == 1)
    v_evs = [e for e in evs if e["type"] == "verify" and e.get("artifacts")]
    check("V2 吃老本：事件标 stale",
          v_evs and v_evs[0]["artifacts"] == [{"path": "docs/old_report.md",
                                               "declared": True, "status": "stale"}],
          repr(v_evs))
    check("V3 吃老本：附系统侧核查声明",
          "【系统侧核查】" in holder["summary"] and "本轮从未更新" in holder["summary"],
          holder["summary"])

    # ---------- 闸门路径 W：加粗声称 + 文件缺失（复现 REA 三测盲区 2） ----------
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "完成。报告已写入：**`docs/bold_claim.md`**，请查阅", "convo": []}
    evs = list(_verify_gate(ag, "帮我写个总结", holder))
    check("W1 加粗声称：提取生效触发核查", StubAgent.corrective_calls == 1)
    check("W2 加粗声称：缺失照常附声明",
          "bold_claim.md" in holder["summary"] and "【系统侧核查】" in holder["summary"],
          holder["summary"])

    # ---------- 闸门路径 X：幽灵声明（文件在但全程零写操作） ----------
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "明细已写入 docs/ghost_exist.md", "convo": []}
    evs = list(_verify_gate(ag, "帮我整理明细", holder))
    check("X1 幽灵声明：触发修正轮", StubAgent.corrective_calls == 1)
    x_evs = [e for e in evs if e["type"] == "verify" and e.get("artifacts")]
    check("X2 幽灵声明：事件标 ghost",
          x_evs and x_evs[0]["artifacts"] == [{"path": "docs/ghost_exist.md",
                                               "declared": False, "status": "ghost"}],
          repr(x_evs))
    check("X3 幽灵声明：核查声明说明缘由",
          "不是本轮写出的" in holder["summary"], holder["summary"])

    # ---------- 闸门路径 Y：真写过/本轮更新过 → 零介入 ----------
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "明细已写入 docs/ghost_exist.md", "convo": convo_w}
    evs = list(_verify_gate(ag, "帮我整理明细", holder))
    check("Y1 任务里真写过 → 不冤枉",
          StubAgent.corrective_calls == 0 and evs == []
          and holder["summary"] == "明细已写入 docs/ghost_exist.md")
    (tmp / "docs" / "touched.md").write_text("v1\n", encoding="utf-8")
    snap_t = vg.snapshot_artifacts("产出文件：docs/touched.md", tmp)
    time.sleep(0.02)
    (tmp / "docs" / "touched.md").write_text("v2\n", encoding="utf-8")
    holder = {"summary": "明细已写入 docs/touched.md", "convo": []}
    evs = list(_verify_gate(ag, "任务：产出明细，产出文件：docs/touched.md",
                            holder, snapshot=snap_t))
    check("Y2 快照后真更新过 → 零介入",
          StubAgent.corrective_calls == 0 and evs == []
          and holder["summary"] == "明细已写入 docs/touched.md")

    print("\n全部断言通过。")


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    except AssertionError:
        print("\n存在失败断言。")
        sys.exit(1)
    print(f"耗时 {time.time() - t0:.1f}s")
