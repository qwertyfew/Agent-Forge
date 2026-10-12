"""收尾验证闸门的自测：verify.py 的提取/执行/核查 + orchestrator._verify_gate 的四条路径。

运行：python tools/test_verify_gate.py
全部断言内建，退出码 0 = 全过。这是纯程序测试，不需要模型在线。
"""
from __future__ import annotations

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
            assert "系统侧验证结果" in prompt
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

    print("\n全部断言通过。")


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    except AssertionError:
        print("\n存在失败断言。")
        sys.exit(1)
    print(f"耗时 {time.time() - t0:.1f}s")
