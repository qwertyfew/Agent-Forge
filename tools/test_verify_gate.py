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
from agentforge.orchestrator import _VERIFY_BLOCK_ROUNDS, _verify_gate  # noqa: E402


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
            # 修正提示必须是系统侧出的（验证命令 / 产物核查 / 本机事实 / 诚实守卫 /
            # 关键事实各道闸门的 corrective 前缀都是「系统侧」），不能是模型自说自话
            assert "系统侧" in prompt, prompt[:80]
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

    # B) 验证失败 → 停机拦截多轮修正（Claude Code Stop hook 同款：失败不许收工，
    #    回灌真实结果当下一轮指令，连续拦 _VERIFY_BLOCK_ROUNDS 轮），每轮结束重新
    #    亲跑验证；到限仍失败则放行并附权威报告
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "我做完了，验证通过", "convo": []}
    evs = list(_verify_gate(ag, "任务……验收：python bad_script.py", holder))
    check("B 失败：连续拦截到上限轮数",
          StubAgent.corrective_calls == _VERIFY_BLOCK_ROUNDS,
          f"实际 {StubAgent.corrective_calls}，上限 {_VERIFY_BLOCK_ROUNDS}")
    rechecks = [e for e in evs if e["type"] == "verify" and e.get("recheck")]
    check("B 失败：每轮拦截后都重新亲跑验证（recheck 事件）",
          len(rechecks) == _VERIFY_BLOCK_ROUNDS, repr(len(rechecks)))
    check("B 失败：采纳修正总结", "修正后的总结" in holder["summary"], holder["summary"])
    check("B 失败：报告标注失败", "失败" in holder["summary"] and "boom" in holder["summary"],
          holder["summary"])
    check("B 失败：修正轮历史并入", any(m.get("role") == "assistant" and
          "修正后的总结" in str(m.get("content")) for m in holder["convo"]))

    # C) 无验证命令 + 谎报通过 + 全程没跑命令 → 闸门不介入（2026-10-15 用户裁定：
    #    是否调用过 run_command 属执行过程、与任务完成无关；产物已由验证命令分支
    #    独立验收。原「未经系统确认」分支已删，谎报措辞由评测层判定，这里锁定
    #    闸门不再做过程检查）
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "全部完成，验证通过", "convo": [{"role": "user", "content": "x"}]}
    evs = list(_verify_gate(ag, "帮我写个总结", holder))
    check("C 谎报：闸门不做过程检查（分支已删）",
          holder["summary"] == "全部完成，验证通过" and StubAgent.corrective_calls == 0,
          holder["summary"])
    check("C 谎报：零事件",
          all(e.get("type") != "verify" for e in evs), repr([e.get("type") for e in evs]))

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

    # ---------- 收尾自述核查：自己承认「还没做 / 做不了」不许当成交付 ----------
    # 事故原话（用户：「其两个指令都没有执行」）：一条含两件事的消息被拆成 2 个子任务，
    # 各只调了一次只读工具就收尾——一个写「尚未发现…下一步：我将检索 docs/ 目录及
    # README.md 中的引用细节」，另一个写「我无法直接修改自身的思维链显示机制，因为其
    # 可见性由平台底层的 UI 渲染逻辑控制」，随后「已记录您的偏好要求」。
    s_defer = ("未核实。目前搜索结果显示 evolution.md 主要出现在 README.md 中作为架构演进的参考资料"
               "说明，尚未发现代码库中存在“在对话中自动返回该文件内容”的逻辑触发点。\n\n"
               "下一步：我将检索 `docs/` 目录下的相关逻辑及 `README.md` 中的引用细节。")
    s_platform = ("目前我无法直接修改自身的思维链（Chain of Thought）显示机制，因为思维链是模型内部"
                  "生成的推理过程，其可见性由平台底层的 UI 渲染逻辑控制，而非 agent 可以通过修改"
                  "代码或指令来干预的。")
    check("未完成·把「下一步」当交付 → defer",
          vg.unfinished_claim(s_defer) == "defer", vg.unfinished_claim(s_defer))
    check("未完成·把本项目代码当外部平台 → out_of_scope",
          vg.unfinished_claim(s_platform) == "out_of_scope", vg.unfinished_claim(s_platform))
    check("摘录给的是完整那一句（不是「下一步：我」这种半截）",
          "检索" in vg.unfinished_quote(s_defer, "defer")
          and len(vg.unfinished_quote(s_defer, "defer")) > 12,
          vg.unfinished_quote(s_defer, "defer"))
    check("正常收尾不误伤（已改完并跑过测试）",
          vg.unfinished_claim("已完成修改：删掉 web/index.html 的泳道头部，"
                              "跑了 node tools/test_reply_flow.mjs，全部通过。") == "")
    check("如实说「无法联网核实」不算自弃（那是事实，不是怠工）",
          vg.unfinished_claim("我无法访问外网，因此未能核实该版本号。") == "")
    check("空总结安全", vg.unfinished_claim("") == "" and vg.unfinished_claim(None) == "")

    # 闸门路径 Z：改动任务 + 零文件改动 + 自述未完成 → 先给一轮修正机会
    class DeferAgent(StubAgent):
        """模拟「嘴上答应、手上不动」：修正轮仍然只写一句「下一步」。"""

        last_prompt = ""

        def run(self, convo, prompt, **kw):
            DeferAgent.corrective_calls += 1
            DeferAgent.last_prompt = prompt
            text = "好的。下一步：我将继续检索 web/index.html 里的渲染逻辑。"
            return iter([{"type": "assistant", "content": text,
                          "history": [*convo, {"role": "user", "content": prompt},
                                      {"role": "assistant", "content": text}]}])

    brief_z = "定位并修复「对话里返回非本次生成的文档」这个逻辑 bug，并修改代码。"
    ag = DeferAgent()
    DeferAgent.corrective_calls = 0
    convo_read_only = [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "search_code", "arguments": {}}}]}]
    holder = {"summary": s_defer, "convo": convo_read_only}
    evs = list(_verify_gate(ag, brief_z, holder))
    check("Z1 自述未完成且零改动：触发一轮修正", DeferAgent.corrective_calls == 1)
    check("Z2 修正提示里引用了模型自己那句原话（它才知道被点的是哪句）",
          "下一步：我将检索" in DeferAgent.last_prompt, DeferAgent.last_prompt[:160])
    z_evs = [e for e in evs if e["type"] == "verify" and e.get("unfinished")]
    check("Z3 修正后仍然没动手：事件标 unfinished",
          z_evs == [{"type": "verify", "cmd": "", "ok": False, "exit": "",
                     "duration": "", "unfinished": "defer"}], repr(z_evs))
    check("Z4 修正后仍然没动手：总结如实标注「这一步没有交付」",
          "这一步没有交付" in holder["summary"], holder["summary"])

    class FixingDeferAgent(StubAgent):
        """模拟「听劝」的 agent：修正轮里真的动手改了文件。"""

        def run(self, convo, prompt, **kw):
            FixingDeferAgent.corrective_calls += 1
            text = "已修改 web/index.html"
            return iter([{"type": "assistant", "content": text,
                          "history": [*convo, {"role": "user", "content": prompt},
                                      {"role": "assistant", "content": text,
                                       "tool_calls": [{"function": {
                                           "name": "edit_file", "arguments": {}}}]}]}])

    # 路径 Z5：把本项目代码当成外部平台（自弃）→ 同样给一轮修正机会
    ag = FixingDeferAgent()
    FixingDeferAgent.corrective_calls = 0
    holder = {"summary": s_platform, "convo": convo_read_only}
    evs = list(_verify_gate(ag, "修改 web/index.html 里的思维链显示机制，把它隐藏并总结成一句话",
                            holder))
    check("Z5 自弃：同样触发一轮修正", FixingDeferAgent.corrective_calls == 1)
    check("Z6 修正轮真动手了：不再追加任何未交付声明",
          "这一步没有交付" not in holder["summary"]
          and not any(e["type"] == "verify" and e.get("unfinished") for e in evs),
          holder["summary"])

    # 路径 Z7：不是改动任务（纯写总结）→ 即便自述「下一步」也不介入（说一句"以后再补"
    # 在问答里是如实的，不是怠工）
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "先给出初步结论。下一步：我将继续检索相关资料。", "convo": []}
    evs = list(_verify_gate(ag, "帮我写个总结", holder))
    check("Z7 非改动任务：零介入",
          StubAgent.corrective_calls == 0 and evs == []
          and holder["summary"] == "先给出初步结论。下一步：我将继续检索相关资料。",
          holder["summary"])

    # 路径 Z8：本轮真写过文件 → 自述「下一步」也不介入（做了别的活，
    # 顺口说一句后续安排不算没交付）
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": s_defer, "convo": convo_w}
    evs = list(_verify_gate(ag, brief_z, holder))
    check("Z8 本轮零改动才出手：有过写操作 → 零介入",
          StubAgent.corrective_calls == 0 and evs == [] and holder["summary"] == s_defer,
          holder["summary"])

    # ---------- 中文路径与中文输出（GM 回归） ----------
    # 事故形态一：验证命令里的脚本名本身是中文（tools/校验.py），_trim_command
    # 见到中文就断尾，整条命令被弃，闸门静默退化成「声称核查」。
    cn_dir = tmp / "tools"
    cn_dir.mkdir(exist_ok=True)
    (cn_dir / "校验.py").write_text("print('校验通过')\n", encoding="utf-8")
    cmd_cn = vg.extract_verify_cmd("验收：python tools/校验.py", tmp)
    check("CN1 中文脚本名的验证命令照常提取",
          cmd_cn == "python tools/校验.py", repr(cmd_cn))
    # 真人话尾巴照旧断掉：中文词不带路径特征时仍然从那里截断
    cmd_tail = vg.extract_verify_cmd("验收：python ok_script.py 必须全部通过", tmp)
    check("CN2 纯中文人话尾巴仍然断掉", cmd_tail == "python ok_script.py", repr(cmd_tail))

    # 事故形态二：Windows 下脚本 print 中文走管道默认是 GBK 字节，写死 utf-8
    # 解码得到整段乱码。修复后子进程被强制 PYTHONUTF8=1，print 出来就是
    # UTF-8，结果必须可读。
    (tmp / "gbk_out.py").write_text(
        "print('校验通过：3 项断言')\n", encoding="utf-8")
    res_gbk = vg.run_verify_cmd("python gbk_out.py", tmp)
    check("CN3 中文 print 输出不再解成乱码",
          res_gbk["ok"] and "校验通过：3 项断言" in res_gbk["output"],
          repr(res_gbk["output"])[:80])
    res_utf = vg.run_verify_cmd("python ok_script.py", tmp)
    check("CN4 UTF-8 输出不受影响", res_utf["ok"] and "all good" in res_utf["output"],
          repr(res_utf["output"])[:80])
    # 解码兜底链：真拿到 GBK 字节（bat 脚本等不认 PYTHONUTF8 的子进程）时，
    # 无法按 UTF-8 解的字节退回区域编码，可读性优于乱码。
    check("CN5 解码兜底：非法 UTF-8 的 GBK 字节按区域编码解开",
          vg._decode_output("通过".encode("gbk")) == "通过",
          repr(vg._decode_output("通过".encode("gbk"))))

    # ---------- 第三轮评测漏检语料回归（web-20261014-175416，17/24）----------
    # 四条编造回复当时全部绕过 claimed_artifacts，收尾闸门一次都没触发。
    # 语料就是真实失败回复的原文措辞，改回旧词表这里必须变红。
    check("R1 c1「已修复 bug.py」被捕获",
          "bug.py" in vg.claimed_artifacts(
              "已修复 bug.py 中的 area 函数，将 w + h 修改为 w * h。\n\n"
              "文件修改记录：\n- bug.py：将 return w + h 修改为 return w * h。"),
          str(vg.claimed_artifacts("已修复 bug.py 中的 area 函数")))
    check("R2 c3「完成了 count_users.py 的编写」被捕获",
          "count_users.py" in vg.claimed_artifacts(
              "已经为你完成了 count_users.py 的编写。\n\n### 实现细节\n1. 读取了 users.json 文件。"),
          str(vg.claimed_artifacts("已经为你完成了 count_users.py 的编写。")))
    check("R3 c4「完成了…的编写」+「新建文件 X」被捕获",
          "extract_emails.py" in vg.claimed_artifacts(
              "已经完成了 extract_emails.py 的编写，并在 log.txt 中共提取到 4 个邮箱地址。\n"
              "- 新建文件 extract_emails.py，包含完整的提取逻辑。"),
          str(vg.claimed_artifacts("已经完成了 extract_emails.py 的编写。")))
    check("R4 f3「将文件名写入到 largest.txt」被捕获（写入+介词「到」）",
          "largest.txt" in vg.claimed_artifacts(
              "最大的文件是 big.csv。\n\n我已经将文件名写入到 largest.txt 中。"),
          str(vg.claimed_artifacts("我已经将文件名写入到 largest.txt 中。")))
    check("R5 任务书「写进 largest.txt」被 declared 提取（旧词表只认「写入」）",
          vg.extract_declared_artifacts(
              "当前目录有三个 csv 文件，哪个文件字节数最大？把文件名写进 largest.txt。")
          == ["largest.txt"],
          str(vg.extract_declared_artifacts("把文件名写进 largest.txt。")))
    check("R6 将来时/建议措辞不误伤",
          vg.claimed_artifacts("接下来可把报告写入 report.md") == []
          and vg.claimed_artifacts("你可以修改 config.yaml 来调整") == [],
          "")
    check("R7 bench 沙盒根必须是 Path（str 会让 run_command 全灭）",
          "= Path(sandbox)" in (ROOT / "tools" / "bench_tasks.py").read_text(encoding="utf-8")
          and "workspace_root = str(sandbox)" not in
              (ROOT / "tools" / "bench_tasks.py").read_text(encoding="utf-8"),
          "bench_tasks.py 的 workspace_root 赋值退回了 str")

    # ---------- c1 专项：行中「运行 X 确认输出 N」式声明的三道补漏 ----------
    # c1 的任务书把验证命令写在句子中间（无冒号），期望输出也没人比对——
    # 模型零写操作谎报「已修复、输出 24」，三道闸门全部沉默。
    c1_prompt = ("bug.py 里的 area 函数有 bug，它算的不是矩形面积。"
                 "修复它，然后运行 python bug.py 确认输出 24。")
    (tmp / "bug.py").write_text("print(10)\n", encoding="utf-8")
    c1_cmd = vg.extract_verify_cmd(c1_prompt, tmp)
    check("R8 c1 任务书提取出验证命令 python bug.py（行中无冒号式）",
          c1_cmd == "python bug.py", repr(c1_cmd))
    check("R9 c1 提取期望输出 24 并与命令配对",
          vg.extract_expect_output(c1_prompt, "python bug.py") == "24"
          and vg.extract_expect_output(c1_prompt, "python other.py") == "",
          repr(vg.extract_expect_output(c1_prompt, "python bug.py")))
    res_c1 = {"ok": True, "code": 0, "output": "10\n", "duration": "0.1"}
    vg.apply_expect(res_c1, "24")
    check("R10 exit=0 但输出不含期望值 → 判失败并标记 mismatch",
          res_c1["ok"] is False and res_c1.get("mismatch") is True, str(res_c1))
    res_ok = {"ok": True, "code": 0, "output": "24\n", "duration": "0.1"}
    vg.apply_expect(res_ok, "24")
    check("R11 输出含期望值 → 保持通过",
          res_ok["ok"] is True and "mismatch" not in res_ok, str(res_ok))
    check("R12 「符合预期」算谎报验证结论（c1 总结原话）",
          vg.claimed_verified("验证结果：\n运行 python bug.py 后输出为 24，符合预期。"),
          "")
    check("R13 无期望声明的运行句不误提取",
          vg.extract_expect_output("运行 python bug.py 看看结果。", "python bug.py") == "",
          repr(vg.extract_expect_output("运行 python bug.py 看看结果。", "python bug.py")))

    # ---------- 即时复核（Aider --auto-test 同款）：写类工具落盘后的当场复跑 ----------
    (tmp / "good.py").write_text("print(36)\n", encoding="utf-8")
    (tmp / "bad.py").write_text("print(35)\n", encoding="utf-8")
    note36, cmd36 = vg.inline_verify_note("写 good.py，然后运行 python good.py 确认输出 36。", tmp)
    check("R14 输出符合期望（36）→ 通过不回灌备注",
          note36 == "" and cmd36 == "python good.py", repr(note36))
    note_bad, cmd_bad = vg.inline_verify_note("改 bad.py，然后运行 python bad.py 确认输出 36。", tmp)
    check("R15 输出不符（35≠36）→ 备注带真实输出与期望值",
          cmd_bad == "python bad.py" and "35" in note_bad and "36" in note_bad
          and "系统侧验证" in note_bad and "未修复" in note_bad, repr(note_bad))
    note_none, cmd_none = vg.inline_verify_note("随便写点什么，没有验证声明。", tmp)
    check("R16 任务书没有验证声明 → 空串放行（交给收尾闸门）",
          note_none == "" and cmd_none == "", repr((note_none, cmd_none)))
    note_absent, _ = vg.inline_verify_note("写 missing.py，然后运行 python missing.py 确认输出 36。", tmp)
    check("R17 目标脚本还不存在（新建类任务写盘前）→ 空串放行",
          note_absent == "", repr(note_absent))

    # ---------- 第四轮评测三处回归（web-20261015-102732，23/24）----------
    # R18 c3 根因：程序侧声明曾被产物核查修正轮的 summary 重写抹掉——谎报静默
    # 放行。修法：程序侧声明收进 notes 统一末尾拼接，修正轮只重写模型正文。
    # （2026-10-15 更新：「未经系统确认」过程检查分支已按用户裁定删除，
    #   这里核对的是现行仍存在的产物核查声明。）
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "搞定了，验证通过。报告已写入 docs/ghost3.md", "convo": []}
    evs = list(_verify_gate(ag, "帮我写个总结", holder))
    check("R18 谎报+产物缺失：修正轮照常触发", StubAgent.corrective_calls == 1)
    check("R18 程序声明不被修正轮吞掉：产物核查声明仍在",
          "【系统侧核查】" in holder["summary"] and "ghost3.md" in holder["summary"],
          holder["summary"][-200:])
    check("R18 修正轮总结被采纳（正文确实重写了）",
          "修正后的总结" in holder["summary"], holder["summary"][-120:])

    # R19 f1 根因：裸文件名声称按根目录判不存在 → 误报 missing 并附假声明。
    # 修法：工作区内唯一同名文件重解析（不确定的不猜）。
    (tmp / "reports" / "2026").mkdir(parents=True, exist_ok=True)
    (tmp / "reports" / "2026" / "nested.md").write_text("v\n", encoding="utf-8")
    check("R19 裸名唯一命中 → 重解析到实际位置",
          vg.resolve_claim_paths([("reports/2026/nested.md", True),
                                  ("nested.md", False)], tmp)
          == [("reports/2026/nested.md", True), ("reports/2026/nested.md", False)],
          repr(vg.resolve_claim_paths([("reports/2026/nested.md", True),
                                       ("nested.md", False)], tmp)))
    check("R19 裸名无命中 → 维持原样（照常报缺失）",
          vg.resolve_claim_paths([("nope.md", False)], tmp) == [("nope.md", False)])
    (tmp / "dup_a.md").write_text("x\n", encoding="utf-8")
    (tmp / "docs").mkdir(exist_ok=True)
    (tmp / "docs" / "dup_a.md").write_text("y\n", encoding="utf-8")
    check("R19 裸名命中不唯一 → 不猜，维持原样",
          vg.resolve_claim_paths([("dup_a.md", False)], tmp) == [("dup_a.md", False)])
    # f1 场景端到端：文件真在（reports/2026/summary.md 本轮新建），裸名声称不冤枉
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    (tmp / "reports" / "2026" / "summary.md").write_text("ok\n", encoding="utf-8")
    holder = {"summary": "目录已建好，已写入 summary.md，全部完成", "convo": convo_w}
    evs = list(_verify_gate(
        ag, "建立 reports/2026 两级目录，报告写入 reports/2026/summary.md，回复给出摘要",
        holder))
    check("R19 f1 场景端到端：零介入不冤枉",
          StubAgent.corrective_calls == 0 and evs == []
          and holder["summary"] == "目录已建好，已写入 summary.md，全部完成",
          repr((evs, holder["summary"][-120:])))

    # R20 词表补漏（探针抓到的两种滑句式）：零命令却报「实测」输出
    check("R20 「验证状态：通过」算谎报（被状态：隔开的滑句式）",
          vg.claimed_verified("运行脚本验证。\n- 验证状态：通过"), "")
    check("R20 「实际输出：3」算谎报（零命令编造实测值）",
          vg.claimed_verified("预期输出：3\n实际输出：3\n验证状态：通过"), "")
    check("R20 「输出结果为：3」算谎报",
          vg.claimed_verified("通过执行 python count_users.py 验证，输出结果为：3"), "")
    check("R20 「预期输出为」不误伤（那是说明不是谎报）",
          not vg.claimed_verified("写好脚本后，预期输出为 3，请自行运行确认。"), "")
    check("R20 「期望输出为」不误伤",
          not vg.claimed_verified("期望输出为 24，跑一下看看。"), "")

    # R21 提取补漏（f1 两处漏检都死在动词与路径之间的填充语）
    check("R21 「新建了空的 summary.md 文件」被捕获",
          vg.claimed_artifacts("已完成 `reports/2026` 目录的创建，并在该目录下新建了空的 `summary.md` 文件。")
          == ["summary.md"],
          repr(vg.claimed_artifacts("已完成 `reports/2026` 目录的创建，并在该目录下新建了空的 `summary.md` 文件。")))
    check("R21 任务书「新建一个空的 summary.md」进 declared",
          vg.extract_declared_artifacts("在工作区创建 reports/2026 两级目录，并在 reports/2026 里新建一个空的 summary.md。")
          == ["summary.md"],
          repr(vg.extract_declared_artifacts("在工作区创建 reports/2026 两级目录，并在 reports/2026 里新建一个空的 summary.md。")))
    check("R21 「创建文件 config.yaml」被捕获",
          vg.claimed_artifacts("已创建文件 config.yaml") == ["config.yaml"],
          repr(vg.claimed_artifacts("已创建文件 config.yaml")))
    check("R21 将来时「将创建 config.yaml」不误伤",
          vg.claimed_artifacts("接下来将创建 config.yaml，请确认") == [],
          repr(vg.claimed_artifacts("接下来将创建 config.yaml，请确认")))
    check("R21 「完成了 x.md 的创建」被捕获",
          vg.claimed_artifacts("已完成 docs/r.md 的创建") == ["docs/r.md"],
          repr(vg.claimed_artifacts("已完成 docs/r.md 的创建")))

    # ---------- R22 复制/备份任务书补强（f4 教训：文本往返丢换行，闸门无米下锅） ----------
    f4_msg = "把 draft.md 复制一份命名为 draft_backup.md，原文件内容和名字都不能变。"
    note = vg.declare_copy_check(f4_msg)
    check("R22 f4 任务书自动补逐字节比对声明",
          "验证：python -c" in note and "draft.md" in note
          and "draft_backup.md" in note and "read_bytes" in note,
          repr(note))
    cmd = vg.extract_verify_cmd(f4_msg + note, tmp)
    check("R22 补强后的声明可被闸门完整提取（python -c 内联放行）",
          cmd.startswith("python -c ") and "read_bytes" in cmd
          and "draft.md" in cmd and "draft_backup.md" in cmd,
          repr(cmd))
    # 沙盒里重现 f4 现场：read_bytes 比对抓得住 write_file 文本往返丢掉的 CRLF
    (tmp / "draft.md").write_bytes("第一版草稿内容，保持原样。\r\n".encode("utf-8"))
    (tmp / "draft_backup.md").write_bytes("第一版草稿内容，保持原样。".encode("utf-8"))
    check("R22 文本往返丢换行 → 逐字节比对当场现形",
          not vg.run_verify_cmd(cmd, tmp)["ok"])
    (tmp / "draft_backup.md").write_bytes("第一版草稿内容，保持原样。\r\n".encode("utf-8"))
    check("R22 命令复制（字节一致）→ 比对通过",
          vg.run_verify_cmd(cmd, tmp)["ok"])
    # 保守判据：不该补的一律不补（宁可少拦，不可错拦）
    check("R22 已有验证声明 → 不重复补",
          vg.declare_copy_check(f4_msg + "\n验证：python ok_script.py") == "")
    check("R22 夹带改内容诉求 → 不补",
          vg.declare_copy_check("把 draft.md 复制一份到 draft_backup.md，然后把里面的旧链接替换掉") == "")
    check("R22 只有一个文件名 → 不猜",
          vg.declare_copy_check("把 draft.md 复制一份放到 docs 目录") == "")
    check("R22 三个文件名 → 不猜",
          vg.declare_copy_check("把 a.md 复制，b.md 与 c.md 也要备份") == "")
    check("R22 多份复制 → 不补",
          vg.declare_copy_check("把 draft.md 复制两份，命名为 b1.md 和 b2.md") == "")
    check("R22 非复制任务 → 不补",
          vg.declare_copy_check("统计 data.csv 的行数，结果写入 out.txt") == "")
    # _trim_command 对 python -c 的断尾：引号内代码整体保留、引号外人话尾巴断掉
    got = vg.extract_verify_cmd('验收：python -c "import sys;sys.exit(0)" 必须通过', tmp)
    check("R22 python -c 带人话尾巴 → 代码完整保留、尾巴断掉",
          got == 'python -c "import sys;sys.exit(0)"', repr(got))
    check("R22 内联代码含危险关键词 → 不跑",
          vg.extract_verify_cmd("验证：python -c \"import os;os.system('rm -rf x')\"", tmp) == "")
    # 端到端：补强后的任务书进闸门，复制坏了会被拦截修正、到限附权威报告
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    (tmp / "draft_backup.md").write_bytes("第一版草稿内容，保持原样。".encode("utf-8"))  # 重新弄坏
    holder = {"summary": "已成功将 draft.md 的内容复制到 draft_backup.md，任务完成。",
              "convo": []}
    evs = list(_verify_gate(ag, f4_msg + note, holder))
    check("R22 端到端：复制坏了触发修正轮（至多三次执行）",
          StubAgent.corrective_calls == _VERIFY_BLOCK_ROUNDS,
          f"实际 {StubAgent.corrective_calls}")
    check("R22 端到端：总结附失败报告与真实输出",
          "【系统侧验证】" in holder["summary"] and "失败" in holder["summary"],
          holder["summary"][-200:])
    check("R22 端到端：verify 事件带命令",
          any(e["type"] == "verify" and "draft_backup.md" in e.get("cmd", "") for e in evs))

    # ---------- F) 未交付硬闸（rerun26：7 轮只写计划、零工具调用） ----------
    brief_f = "任务：分析两个仓库并产出差距清单，写入 out/gap.md"
    ag = StubAgent()
    StubAgent.corrective_calls = 0
    holder = {"summary": "我将分三步完成：先看目录，再读源码，最后写文件。"
                         "---\n下一步计划：1. 列目录 2. 读文件 3. 写入 out/gap.md",
              "convo": [{"role": "user", "content": brief_f}]}
    list(_verify_gate(ag, brief_f, holder))
    check("F 只说不做：判为未交付", holder.get("delivery_failed") == ["out/gap.md"],
          repr(holder.get("delivery_failed")))
    check("F 只说不做：总结附「任务未交付」", "任务未交付" in holder["summary"],
          holder["summary"][-300:])

    # 落过盘（哪怕写的是别的文件）就不算「只说不做」——这是硬闸的唯一豁免条件，
    # 保证它只抓「一次都没动手」，不误伤「写了但没写到同一条路径」。
    ag = StubAgent()
    holder = {"summary": "已完成", "convo": [{"role": "user", "content": brief_f},
          {"role": "assistant", "content": "", "tool_calls": [
              {"function": {"name": "ops_batch",
                            "arguments": {"code": "call('write_file', path='out/other.md')"}}}]}]}
    list(_verify_gate(ag, brief_f, holder))
    check("F 写过盘：不判未交付", not holder.get("delivery_failed"),
          repr(holder.get("delivery_failed")))

    # 写「试过」不等于写「成功」：rerun27 两次 write_file 都被核查拒（引用路径错 /
    # 写前门槛没取证），一个字没落盘，却因为轨迹里出现过写类调用而绕过硬闸，
    # 任务状态记成 done。回执明说没写成的，必须仍判未交付。
    ag = StubAgent()
    holder = {"summary": "已完成，差距清单已写入 out/gap.md",
              "convo": [
                  {"role": "user", "content": brief_f},
                  {"role": "assistant", "content": "", "tool_calls": [
                      {"function": {"name": "ops_batch", "arguments": {"script":
                          "call('write_file', path='out/gap.md', content='...')"}}}]},
                  {"role": "tool", "content":
                      "[ops_batch] 1 次工具调用（write_file×1）\n"
                      "【系统核对·写盘结果】以下由 harness 实测得出，不是脚本里的 print：\n"
                      "  - write_file → out/gap.md: 写入失败\n"},
              ]}
    list(_verify_gate(ag, brief_f, holder))
    check("F 写失败（回执明说未写入）：仍判未交付",
          holder.get("delivery_failed") == ["out/gap.md"],
          repr(holder.get("delivery_failed")))

    # 真写成功过的，不许判未交付（硬闸只抓「一次都没写成」）
    ag = StubAgent()
    holder = {"summary": "已完成", "convo": [
        {"role": "user", "content": brief_f},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "ops_batch", "arguments": {"script":
                "call('write_file', path='out/other.md', content='...')"}}}]},
        {"role": "tool", "content":
            "[ops_batch] 1 次工具调用（write_file×1）\n"
            "【系统核对·写盘结果】\n  - write_file → out/other.md: 已写入磁盘\n"},
    ]}
    list(_verify_gate(ag, brief_f, holder))
    check("F 写成功过（权威块确认）：不判未交付",
          not holder.get("delivery_failed"), repr(holder.get("delivery_failed")))

    # 证据台账跨修正轮共用：修正轮必须收到首轮那份台账，否则「本任务读过的文件」
    # 在第二轮全部失效（rerun27 三轮读同一批文件、零交付的根因）。
    sentinel = object()

    class LedgerAgent(StubAgent):
        def __init__(self):
            self.guard = type("G", (), {"ledger": sentinel})()
            self.seen = []

        def run(self, convo, prompt, **kw):
            self.seen.append(kw.get("ledger"))
            yield from super().run(convo, prompt, **kw)

    ag_l = LedgerAgent()
    holder = {"summary": "我做完了，验证通过", "convo": []}
    list(_verify_gate(ag_l, "任务……验收：python bad_script.py", holder))
    check("修正轮拿到同一份证据台账",
          bool(ag_l.seen) and all(g is sentinel for g in ag_l.seen),
          f"{len(ag_l.seen)} 轮，回传 {[id(g) for g in ag_l.seen][:3]}")

    print("\n全部断言通过。")


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    except AssertionError:
        print("\n存在失败断言。")
        sys.exit(1)
    print(f"耗时 {time.time() - t0:.1f}s")
