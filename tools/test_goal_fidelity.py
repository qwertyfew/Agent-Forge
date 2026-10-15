"""goal 保真核查自测：dispatcher.deliverable_tokens / missing_deliverables
+ Dispatcher._enforce_goal_fidelity 的补写与台账。

运行：python tools/test_goal_fidelity.py
全部断言内建，退出码 0 = 全过。纯程序测试，不需要模型在线、不需要联网。

用例取自审计文档 ② 的典型缩水：用户要求「覆盖写入 outline.md」，调度器拆成
「分析素材并给结论」——执行者照缩水 goal 干活，交付物凭空消失。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import gaps                               # noqa: E402
from agentforge.dispatcher import (                       # noqa: E402
    Dispatcher, TaskSpec, deliverable_tokens, missing_deliverables,
    _fidelity_retry_text, _sentence_of,
)


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------- token 提取

def test_deliverable_tokens() -> None:
    # 审计现场：文件名 + 引号内容
    toks = deliverable_tokens("通读素材并覆盖写入 outline.md，格式务必参照「结论先行」。")
    check("文件名被提取（outline.md）", "outline.md" in toks, repr(toks))
    check("引号内容被提取（结论先行）", "结论先行" in toks, repr(toks))
    # 带目录前缀
    toks = deliverable_tokens("把结果写进 results/report.md。")
    check("带目录前缀的路径被提取",
          any("report.md" in t for t in toks), repr(toks))
    # Windows 反斜杠路径
    toks = deliverable_tokens(r"把对比结果写入 docs\对比.md")
    check("反斜杠路径被提取", any("对比.md" in t for t in toks), repr(toks))
    # 多个文件
    toks = deliverable_tokens("改 config.yaml 和 web/index.html。")
    check("多个文件都提取", "config.yaml" in toks and "web/index.html" in toks, repr(toks))
    # 宁漏勿误：动词短语不认（「写个报告」），没有点名就不出手
    check("动词短语不算点名", deliverable_tokens("帮我写个报告，分析一下。") == [])
    # 无交付物 / 空安全
    check("纯对话无 token", deliverable_tokens("你好，今天天气如何？") == [])
    check("空文本安全", deliverable_tokens("") == [])
    check("None 安全", deliverable_tokens(None) == [])
    # 上限
    names = " ".join(f"f{i}.txt" for i in range(20))
    check("token 数受限", len(deliverable_tokens(f"写入 {names}。")) <= 12)


def test_missing_deliverables() -> None:
    # 缩水现场：goal 丢了交付物
    shrunk = [TaskSpec(id="t1", goal="通读素材并分析素材，给出结构建议。")]
    miss = missing_deliverables("通读素材并覆盖写入 outline.md。", shrunk)
    check("缩水 goal → outline.md 判缺失", miss == ["outline.md"], repr(miss))
    # 保真 goal → 放行
    kept = [TaskSpec(id="t1", goal="通读素材，把大纲覆盖写入 outline.md。")]
    check("保真 goal → 放行",
          missing_deliverables("通读素材并覆盖写入 outline.md。", kept) == [])
    # 反斜杠/正斜杠等价
    kept_bs = [TaskSpec(id="t1", goal=r"把对比结果写入 docs\对比.md")]
    check("路径分隔符归一（\\ = /）",
          missing_deliverables("把对比结果写入 docs/对比.md。", kept_bs) == [])
    # 空任务表安全
    check("空任务表安全", missing_deliverables("写 outline.md", []) == [])


def test_retry_text_and_sentence() -> None:
    txt = _fidelity_retry_text("通读素材并覆盖写入 outline.md。", ["outline.md"])
    check("重拆指令点名丢失项", "outline.md" in txt and "必须重拆" in txt)
    check("重拆指令要求原样保留", "原样" in txt)
    sent = _sentence_of("先读素材。然后覆盖写入 outline.md 并检查。", "outline.md")
    check("句子定位正确", "outline.md" in sent and "先读素材" not in sent, repr(sent))


class FakeCfg:
    def __init__(self, root: str):
        self._root = root
        self.goal_fidelity_gate = True

    def state(self) -> str:
        return self._root


def test_enforce_goal_fidelity() -> None:
    """allow_retry=False 路径：仍缺 → 程序化补进最相关 goal + 记台账。"""
    with tempfile.TemporaryDirectory() as td:
        gaps_path = Path(td) / "gaps.json"
        d = Dispatcher.__new__(Dispatcher)   # 不走 __init__（不需要 LLM）
        d.cfg = FakeCfg(td)

        plan = type(d._llm_plan.__globals__["DispatchPlan"])  # noqa: F841  仅确认可引用
        from agentforge.dispatcher import DispatchPlan
        tasks = [
            TaskSpec(id="t1", goal="通读素材并分析素材，给出结构建议。"),
            TaskSpec(id="t2", goal="根据素材写一份会议通知草稿。"),
        ]
        p = DispatchPlan(tasks=tasks, rationale="", source="rules", dispatcher_name="t")
        out = d._enforce_goal_fidelity("通读素材并覆盖写入 outline.md。", p,
                                       entries=[], allow_retry=False)
        joined = "\n".join(t.goal for t in out.tasks)
        check("仍缺 → 交付物被补进某个 goal", "outline.md" in joined, repr(joined))
        check("补写带范围保全标记", "【系统侧范围保全】" in joined)
        check("补进了相关性最高的任务（t1，素材任务）",
              "outline.md" in out.tasks[0].goal, out.tasks[0].goal)
        check("台账记了 goal_fidelity",
              gaps_path.exists() and "goal_fidelity" in gaps_path.read_text("utf-8"),
              gaps_path.read_text("utf-8") if gaps_path.exists() else "（无台账）")

        # 保真 goal → 原样返回、不记台账
        gaps_path.unlink(missing_ok=True)
        tasks2 = [TaskSpec(id="t1", goal="通读素材，把大纲覆盖写入 outline.md。")]
        p2 = DispatchPlan(tasks=tasks2, rationale="", source="rules", dispatcher_name="t")
        out2 = d._enforce_goal_fidelity("通读素材并覆盖写入 outline.md。", p2,
                                        entries=[], allow_retry=False)
        check("保真 goal → goal 不被改动",
              out2.tasks[0].goal == tasks2[0].goal)
        check("保真 goal → 不记台账", not gaps_path.exists())


def main() -> None:
    test_deliverable_tokens()
    test_missing_deliverables()
    test_retry_text_and_sentence()
    test_enforce_goal_fidelity()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
