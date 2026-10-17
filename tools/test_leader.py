#!/usr/bin/env python3
"""Leader 双环（第 47 步）核心逻辑自测：不联网、不调模型，纯数据结构校验。

覆盖：
  1. LeaderContext 固定槽位：任务原文/失败原因不被精简触碰；note 滚动替换
  2. 失败原因累积保留最近 K 条（旧教训不被新失败冲掉，但也不无限膨胀）
  3. 评估输入：本批全量（回复不截断）+ 固定槽位在前（字节稳定可吃缓存）
  4. parse_batch：prompt 丢弃/kind 推断/模型解析（resolver 语义）
  5. parse_eval：字段缺失容忍、done 无 answer 的降级
"""

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.leader import (  # noqa: E402
    LEADER_PLAN_SYSTEM, LEADER_EVAL_SYSTEM, LeaderContext, StepResult,
    parse_batch, parse_eval, REASONS_KEEP,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


def test_context_slots() -> None:
    print("[1] LeaderContext 固定槽位")
    ctx = LeaderContext("把 README 翻译成英文并写入 docs/en.md")
    ctx.add_round()
    ctx.set_note("第1批结论：README 共 120 行")
    ctx.add_reasons(["交付文件路径写错了"])
    # 槽位在 plan/eval 消息里都固定在最前（原文第一块），且原文逐字保留
    plan_text = "\n\n".join(m["content"] for m in ctx.plan_messages())
    check("原文槽在首位且逐字保留", plan_text.startswith("【任务原文】\n把 README 翻译成英文并写入 docs/en.md"))
    check("原因槽在原文之后", "【未完成原因" in plan_text and "交付文件路径写错了" in plan_text)
    check("摘要槽在场", "第1批结论" in plan_text)
    # note 滚动替换不影响原文与原因
    ctx.set_note("第2批结论：已写出 docs/en.md")
    plan_text2 = "\n\n".join(m["content"] for m in ctx.plan_messages())
    check("note 滚动替换后旧摘要消失", "第1批结论" not in plan_text2 and "第2批结论" in plan_text2)
    check("原文槽不受 note 替换影响", plan_text2.startswith("【任务原文】\n把 README 翻译成英文"))
    check("原因槽不受 note 替换影响", "交付文件路径写错了" in plan_text2)
    # 原因累积裁剪到最近 K 条
    for i in range(REASONS_KEEP + 3):
        ctx.add_reasons([f"原因{i}"])
    check("原因保留最近 K 条不膨胀", len(ctx.reasons) == REASONS_KEEP,
          f"len={len(ctx.reasons)}")
    check("保留的是最近的原因", ctx.reasons[-1] == f"原因{REASONS_KEEP + 2}")


def test_eval_messages_full_batch() -> None:
    print("[2] 评估输入：本批全量进 Leader")
    ctx = LeaderContext("任务原文XYZ")
    ctx.add_round()
    long_reply = "很长的分析结论" * 500          # 3500 字，绝不能被截断
    batch = [StepResult(title="分析", kind="analysis", prompt="读 README 并分析",
                        reply=long_reply, files=["out/a.md"]),
             StepResult(title="写坏的一步", kind="coding", prompt="改 main.py",
                        failed=True, error="模型调用失败")]
    text = "\n\n".join(m["content"] for m in ctx.eval_messages(batch))
    check("长回复全文在场（不截断）", long_reply in text)
    check("失败步骤带失败标记", "执行失败" in text and "模型调用失败" in text)
    check("产出文件列给 Leader", "out/a.md" in text)
    check("批次号可见", "第 1 批" in text)
    # 头部字节稳定：同一上下文两次取 eval 文本，除本批外前缀应一致
    ctx2 = LeaderContext("任务原文XYZ")
    ctx2.add_round()
    text_a = "\n\n".join(m["content"] for m in ctx2.eval_messages([batch[0]]))
    ctx2.add_round()
    ctx2.set_note("摘要")
    text_b = "\n\n".join(m["content"] for m in ctx2.eval_messages([batch[0]]))
    check("原文槽前缀字节稳定（缓存友好）",
          text_a.startswith("【任务原文】\n任务原文XYZ")
          and text_b.startswith("【任务原文】\n任务原文XYZ"))


def test_parse_batch() -> None:
    print("[3] parse_batch：解析与指派")
    entries = [
        {"id": "1", "key": "ds", "name": "DeepSeek V4-Pro",
         "scores": {"coding": 91, "reasoning": 89, "writing": 84, "chinese": 86, "math": 92},
         "total": 90, "window": 262144},
        {"id": "2", "key": "qw", "name": "qwen3.5:4b",
         "scores": {"coding": 66, "reasoning": 72, "writing": 70, "chinese": 78, "math": 74},
         "total": 72, "window": 65536},
    ]

    def resolver(specs, ents):        # 模拟 Dispatcher._resolve 的最小语义
        for i, t in enumerate(specs, 1):
            t.index = i
            dim = {"coding": "coding", "writing": "writing", "analysis": "reasoning"}.get(t.kind, "reasoning")
            best = max(ents, key=lambda e: e["scores"].get(dim, 0))
            t.model_key, t.model_name = best["key"], best["name"]

    obj = {"batch": [
        {"title": "读文件", "kind": "inspect", "prompt": "列出 docs/ 下所有 .md 文件"},
        {"title": "写报告", "kind": "writing", "prompt": "把结论写进 report.md", "model": "qwen3.5:4b"},
        {"title": "空步骤应丢弃", "kind": "coding", "prompt": "   "},
        {"title": "非法kind推断", "kind": "nonsense", "prompt": "编写 parse.py 解析日志"},
    ], "rationale": "先看现场再写"}
    specs, rationale = parse_batch(obj, entries, resolver=resolver)
    check("空 prompt 条目被丢弃", len(specs) == 3, f"len={len(specs)}")
    check("rationale 透传", rationale == "先看现场再写")
    check("inspect 步骤拿到维度最强（reasoning=DeepSeek）", specs[0].model_key == "ds")
    check("writing 步骤拿 writing 最强（DeepSeek 84>70）", specs[1].model_key == "ds")
    check("非法 kind 按文本推断为 coding", specs[2].kind == "coding", specs[2].kind)
    check("index 连续编号", [t.index for t in specs] == [1, 2, 3])
    check("goal 即 Leader 的小步骤提示词", specs[0].goal == "列出 docs/ 下所有 .md 文件")
    # 彻底不可解析 → 空批（调用方走原文直做兜底）
    bad, _ = parse_batch("不是JSON", entries)
    check("非 dict 输入返回空批", bad == [])
    empty, _ = parse_batch({"batch": []}, entries)
    check("空批返回空", empty == [])


def test_parse_eval() -> None:
    print("[4] parse_eval：字段缺失容忍")
    ok = parse_eval({"done": True, "answer": "已完成，见 report.md",
                     "note": "全部完成", "reasons": [], "batch": None})
    check("完成态透传", ok["done"] and ok["answer"] == "已完成，见 report.md")
    bad = parse_eval({"done": True, "answer": ""})
    check("done 但无 answer 降级为未完成", not bad["done"] and bad["reasons"])
    minimal = parse_eval({"done": False, "batch": [{"title": "x", "prompt": "y"}]})
    check("未完成态字段缺省安全", not minimal["done"] and minimal["answer"] == "")
    check("非 dict 返回空", parse_eval(None) == {})


def test_prompts_constraints() -> None:
    print("[5] 提示词硬约束在位")
    check("规划提示含六类判定表", all(k in LEADER_PLAN_SYSTEM for k in
          ("coding", "inspect", "writing", "math", "analysis", "general")))
    check("规划提示含「一类且仅一类」自检标准", "一类且仅一类" in LEADER_PLAN_SYSTEM)
    check("规划提示写明「自包含操作信息」边界", "自包含操作信息" in LEADER_PLAN_SYSTEM)
    check("规划提示写明「不要包含背景叙事」边界", "不要包含背景叙事" in LEADER_PLAN_SYSTEM)
    check("同批独立性规则在场", "相互独立" in LEADER_PLAN_SYSTEM)
    check("评估提示要求逐项核对原文", "逐项核对" in LEADER_EVAL_SYSTEM)
    check("评估提示要求 note 自包含（架构用它替换全部历史）", "替换掉之前所有轮次" in LEADER_EVAL_SYSTEM)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    test_context_slots()
    test_eval_messages_full_batch()
    test_parse_batch()
    test_parse_eval()
    test_prompts_constraints()
    print(f"\n通过 {PASS} / {PASS + FAIL}")
    sys.exit(1 if FAIL else 0)
