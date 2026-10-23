"""关键事实回显核查自测：verify.required_fact_tokens / missing_required_facts
+ orchestrator._verify_gate 的接入（facts_gate）。

运行：python tools/test_keyfacts.py
全部断言内建，退出码 0 = 全过。纯程序测试，不需要模型在线、不需要联网。

用例取自 w3-short-notice 事故：文件里正确写了「14:00」，回复却是空洞回执，
关键事实没进答复。方案 B 保证汇总层**拿到**文件内容，本闸门保证关键事实
**写进**答复——缺了先给一轮修正，仍缺就附系统侧声明。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import verify as vf                       # noqa: E402
from agentforge.orchestrator import _verify_gate          # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------- token 提取

def test_required_fact_tokens() -> None:
    # w3 现场：要求句 + 时间
    toks = vf.required_fact_tokens(
        "写一份 short.txt，内容是会议改期通知。必须保留会议时间 14:00。")
    check("w3 现场：14:00 被提取", "14:00" in toks, repr(toks))
    # 引号内容
    toks = vf.required_fact_tokens("回复务必包含「结论先行」这一原则的说明。")
    check("引号内容被提取", "结论先行" in toks, repr(toks))
    # 数字（含千分位/百分号）
    toks = vf.required_fact_tokens("确保答复里给出总数 1,024 和占比 37%。")
    check("千分位数字被提取", any("1,024" in t for t in toks), repr(toks))
    check("百分数被提取", any("37%" in t for t in toks), repr(toks))
    # 日期
    toks = vf.required_fact_tokens("注明发布日期 2026-10-15。")
    check("日期被提取", any("2026-10-15" in t for t in toks), repr(toks))
    # 验证命令行的数字不是要回显的事实（整行排除）
    toks = vf.required_fact_tokens(
        "任务完成后必须给出结论。\n验证：python tools/x.py 应输出 42")
    check("验证行里的 42 不算", "42" not in toks, repr(toks))
    # 路径段里的数字是定位符不是事实（R19 f1 回归：reports/2026/summary.md）
    toks = vf.required_fact_tokens(
        "建立 reports/2026 两级目录，报告写入 reports/2026/summary.md，回复给出摘要")
    check("路径里的 2026 不算事实", toks == [], repr(toks))
    # 裸「2026/」不当日期（日期正则要求月/日至少其一）
    toks = vf.required_fact_tokens("必须注明 2026/ 目录下的归档规则。")
    check("裸 2026/ 不当日期", "2026/" not in toks, repr(toks))
    # 反引号代码里的数字不算事实
    toks = vf.required_fact_tokens("务必执行 `python run.py --n 7` 并给出结论。")
    check("代码里的 7 不算事实", "7" not in toks, repr(toks))
    # 没有要求句 → 没有 token（宁漏勿误）
    check("没有要求句 → 空", vf.required_fact_tokens("随便写写，然后跑一下。") == [])
    check("空文本安全", vf.required_fact_tokens("") == [])
    check("None 安全", vf.required_fact_tokens(None) == [])
    # rerun31 实测的三类噪声：标识符编号 / 行首序号 / 数量上限
    toks = vf.required_fact_tokens("【本轮编号：rerun31】\n必须保留结论。")
    check("标识符里的编号（rerun31）不算事实",
          not any(t.strip("[]") == "31" for t in toks), repr(toks))
    toks = vf.required_fact_tokens(
        "1. 读源码。\n2. 与本体逐项对比，产出清单，写入 evals/gap-rerun31.md。")
    check("行首序号（2.）不算事实", "2" not in toks, repr(toks))
    toks = vf.required_fact_tokens(
        "末尾附一节「借鉴清单」：最多 5 条，每条注明预期收益。")
    check("数量上限（最多 5 条）不算事实", "5" not in toks, repr(toks))
    check("同句里的引号内容仍被提取", "借鉴清单" in toks, repr(toks))
    # 上限
    toks = vf.required_fact_tokens(
        "必须包含 1、必须包含 2、必须包含 3、必须包含 4、必须包含 5、"
        "必须包含 6、必须包含 7、必须包含 8、必须包含 9、必须包含 10。")
    check("token 数受限", len(toks) <= vf.MAX_FACT_TOKENS, repr(toks))


def test_missing_required_facts() -> None:
    task = "写一份 short.txt，必须保留会议时间 14:00。"
    check("答复缺 14:00 → 命中",
          vf.missing_required_facts("已生成 short.txt，验证通过。", task) == ["14:00"],
          repr(vf.missing_required_facts("已生成 short.txt，验证通过。", task)))
    check("答复含 14:00 → 放行",
          vf.missing_required_facts("会议改期到 14:00，已写入 short.txt。", task) == [])
    # 全半角归一：答复写成了全角冒号也算
    check("全角冒号归一（14：00 = 14:00）",
          vf.missing_required_facts("会议改期到 14：00。", task) == [])
    # 千分位归一：1,024 与 1024 等价
    task2 = "确保给出文件总数 1,024。"
    check("千分位归一（1024 = 1,024）",
          vf.missing_required_facts("文件总数为 1024。", task2) == [])
    # 中文数字不转：「三个」≠「3」（宁可修正一轮，也不猜）
    check("中文数字不算（三个 ≠ 3）",
          vf.missing_required_facts("一共三个文件。", "必须包含数字 3 的统计。") != [])


def test_artifacts_count_as_present() -> None:
    """P1：产物里有的关键事实，答复没复述也不算缺。

    现场（w5 rerun65 / soak03，复现 5/31 轮）：reports/outline.md 里三个标题一个不少，
    只因答复没复述，就被盖了一句「未能核对到」。写盘类任务的主出口是文件。
    """
    task = "整理会议纪要写到 reports/outline.md，必须保留 14:00 与「星港项目」。"
    reply = "已整理完毕，见 reports/outline.md。"
    art = "## 结论\n- 星港项目 10 月 28 日灰度\n会议时间 14:00\n"
    check("只比答复时会被判缺（旧口径）",
          set(vf.missing_required_facts(reply, task)) == {"14:00", "星港项目"},
          repr(vf.missing_required_facts(reply, task)))
    check("纳入产物后不再判缺（P1）",
          vf.missing_required_facts(reply, task, artifacts=art) == [],
          repr(vf.missing_required_facts(reply, task, artifacts=art)))
    # 两边都没有的事实仍然要判缺：不能因为"有文件"就整体放过
    check("两边都没有的仍判缺",
          vf.missing_required_facts(reply, "必须保留 14:00 与 17:30。", artifacts=art) == ["17:30"],
          repr(vf.missing_required_facts(reply, "必须保留 14:00 与 17:30。", artifacts=art)))
    # 「复述」与「缺失」分开：产物里有、答复没写 → 只提示
    check("产物有答复没有 → 记为未复述（不判缺）",
          vf.unrestated_facts(reply, task, artifacts=art) and
          vf.missing_required_facts(reply, task, artifacts=art) == [],
          repr(vf.unrestated_facts(reply, task, artifacts=art)))
    check("没有产物时不产生未复述提示",
          vf.unrestated_facts(reply, task, artifacts="") == [])
    # artifacts_text：读得到就读，读不到绝不抛（读失败不能变成新的误报）
    tmpd = Path(tempfile.mkdtemp(prefix="af_facts_"))
    (tmpd / "a.md").write_text("星港项目 14:00", encoding="utf-8")
    got = vf.artifacts_text(["a.md", "不存在.md"], tmpd)
    check("artifacts_text 读相对路径", "星港项目" in got, got[:80])
    check("artifacts_text 容忍缺失文件", vf.artifacts_text(["不存在.md"], tmpd) == "")


# ---------------------------------------------------------------- 闸门接入

class FakeCfg:
    verify_gate = True
    sources_gate = True
    local_facts_gate = True
    unfinished_gate = True
    workspace_root = str(Path.cwd())


class FakeAgent:
    """假 agent：run() 只负责模拟「一轮修正」。

    fix=None  → 修正轮什么也没做（答复照旧）
    fix=文本  → 修正轮改写答复（把关键事实写进去）
    """

    cfg = FakeCfg()

    def __init__(self, fix: str | None = None):
        self.fix = fix
        self.prompts: list[str] = []

    def run(self, convo, prompt, **kw):
        self.prompts.append(prompt)
        yield {"type": "assistant", "history": convo,
               "content": self.fix if self.fix is not None else ""}


def test_gate_wiring() -> None:
    task = "用一句话回复我改期结论，必须包含改到几点：14:00。"
    bad = "改期通知已经写好，请查收。"

    # 1) 缺关键事实 → 修正一轮；没补上 → 附系统侧声明 + 播报事件
    agent = FakeAgent(fix=None)
    holder = {"summary": bad, "convo": []}
    events = list(_verify_gate(agent, task, holder))
    check("闸门接入：给了一轮修正机会", len(agent.prompts) == 1, repr(agent.prompts))
    check("闸门接入：修正提示点名缺失项", "14:00" in agent.prompts[0])
    check("闸门接入：没补上 → 答复末尾出现系统侧声明",
          "【系统侧核查】" in holder["summary"], holder["summary"][-200:])
    check("闸门接入：播报了缺失事实事件",
          any(e.get("type") == "verify" and e.get("missing_facts") for e in events),
          repr([e.get("type") for e in events]))

    # 2) 修正轮把事实补上 → 不加声明
    agent2 = FakeAgent(fix="会议改期到 14:00，通知已写好。")
    holder2 = {"summary": bad, "convo": []}
    events2 = list(_verify_gate(agent2, task, holder2))
    check("闸门接入：修正轮补上事实 → 不加声明",
          "【系统侧核查】" not in holder2["summary"], holder2["summary"][-200:])
    check("闸门接入：修正后的答复被采纳",
          holder2["summary"].startswith("会议改期到"), holder2["summary"][:60])
    check("闸门接入：没问题时不播报事件",
          not any(e.get("type") == "verify" for e in events2))

    # 3) 本来就包含 → 不打扰
    agent3 = FakeAgent()
    holder3 = {"summary": "会议改期到 14:00。", "convo": []}
    list(_verify_gate(agent3, task, holder3))
    check("闸门接入：本来就包含 → 不打扰", agent3.prompts == [])
    check("闸门接入：正文原样", holder3["summary"] == "会议改期到 14:00。")

    # 4) 开关关掉 → 一律不出手
    class FakeCfgOff(FakeCfg):
        facts_gate = False

    class FakeAgentOff(FakeAgent):
        cfg = FakeCfgOff()

    agent4 = FakeAgentOff()
    holder4 = {"summary": bad, "convo": []}
    list(_verify_gate(agent4, task, holder4))
    check("闸门接入：facts_gate=false 时不出手",
          holder4["summary"] == bad and agent4.prompts == [])


def main() -> None:
    test_required_fact_tokens()
    test_missing_required_facts()
    test_artifacts_count_as_present()
    test_gate_wiring()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
