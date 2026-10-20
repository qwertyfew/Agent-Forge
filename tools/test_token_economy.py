"""token 经济 T2/T3 的自测：编排触发反转 + 工具结果无条件老化。

运行：python tools/test_token_economy.py
全部断言内建，退出码 0 = 全过。纯程序测试，不需要模型在线。

背景（docs/changes/token-economy-20261019.md）：
- T2：极简单循环是默认路径——「多动作/长文本」不再触发多 agent 拆解，
  只有用户明确要并行才拆（rerun14 仪式开销：11 员工固定开销 + 6 轮 leader 判定）。
- T3：工具结果老化不等预算——rerun14 实测每轮 prompt 8k→35k，大头是老工具
  结果在后续每轮反复重发；60k 字符阈值才动手太晚。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                          # noqa: E402
from agentforge.context import Compressor, measure            # noqa: E402
from agentforge.orchestrator import (                         # noqa: E402
    _orchestration_wanted, _wants_parallel,
)


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _tool_msg(i: int) -> dict:
    body = f"工具结果 {i}：" + ("x" * 600) + f"（第 {i} 条尾部）"
    return {"role": "tool", "tool_call_id": f"call_{i}", "name": "read_file",
            "content": body}


def main() -> None:
    # ---------- T2：编排触发反转 ----------
    check("明确并行诉求 → 拆", _wants_parallel("并行分析这两个仓库的差距"))
    check("泳道也算并行诉求", _wants_parallel("开两条泳道同时跑"))
    check("编号多动作 ≠ 并行", not _wants_parallel(
        "1. 逆向分析两个仓库\n2. 与 AgentForge 逐项对比\n3. 产出清单"))
    check("长任务文本 ≠ 并行", not _wants_parallel(
        "任务：" + "很长的一段任务描述，" * 40))
    cfg_auto = Config.load(str(ROOT / "config.yaml"))
    check("默认编排模式是 auto", getattr(cfg_auto, "orchestration", "") == "auto",
          repr(getattr(cfg_auto, "orchestration", None)))
    check("auto 档：多动作不拆", not _orchestration_wanted(
        "1. 分析 A\n2. 分析 B\n3. 写报告", cfg_auto))
    check("auto 档：并行诉求才拆", _orchestration_wanted(
        "并行分析 A 和 B", cfg_auto))
    cfg_always = Config.load(str(ROOT / "config.yaml"))
    cfg_always.orchestration = "always"
    check("always 档：恢复旧行为", _orchestration_wanted("1. 分析 A\n2. 分析 B", cfg_always))
    cfg_never = Config.load(str(ROOT / "config.yaml"))
    cfg_never.orchestration = "never"
    check("never 档：并行诉求也不拆", not _orchestration_wanted("并行跑", cfg_never))

    # ---------- T3：工具结果无条件老化 ----------
    tmp = Path(tempfile.mkdtemp(prefix="af_t3_"))
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = str(tmp)
    comp = Compressor(cfg, client=None)
    check("keep_recent 默认 8", int(getattr(cfg, "compact_keep_recent", 0)) == 8)

    msgs = [{"role": "user", "content": "任务"}]
    msgs += [_tool_msg(i) for i in range(1, 13)]
    msgs.append({"role": "assistant", "content": "结论"})
    before = measure(msgs)
    res = comp.compact(msgs, "auto", force=False)
    check("预算内也触发老化", res.changed and res.strategy == "truncate",
          f"strategy={res.strategy}")
    check("老化确实变短", res.after < res.before, f"{res.before}→{res.after}")
    tools_kept = [m for m in res.messages if m.get("role") == "tool"]
    full_kept = [m for m in tools_kept if not str(m.get("content", "")).startswith("[已压缩]")]
    check("只保留最近 8 条完整工具结果（其余压成一行）",
          len(full_kept) == 8 and len(tools_kept) == 12,
          f"full={len(full_kept)} total={len(tools_kept)}")
    check("最老的被压成一行",
          "[已压缩]" in tools_kept[0]["content"] and len(tools_kept[0]["content"]) < 400)
    check("最近的结果原样保留", tools_kept[-1]["content"] == _tool_msg(12)["content"])
    check("tool_call_id 配对保留（接口硬要求）",
          all(m.get("tool_call_id") for m in tools_kept))
    check("原列表不被修改", measure(msgs) == before)
    check("assistant/user 不受老化影响",
          any(m.get("role") == "assistant" and m["content"] == "结论" for m in res.messages))

    # 少于 keep_recent 不动
    few = [{"role": "user", "content": "t"}, _tool_msg(1), _tool_msg(2)]
    res2 = comp.compact(few, "auto", force=False)
    check("少量工具结果不老化", not res2.changed, res2.strategy)

    # ---------- 老化 + 超预算 L2 仍兼容 ----------
    cfg2 = Config.load(str(ROOT / "config.yaml"))
    cfg2.workspace_root = str(tmp)
    cfg2.compact_threshold = 100        # 极小阈值：老化后仍超 → 走 L2
    comp2 = Compressor(cfg2, client=None)
    res3 = comp2.compact(msgs, "auto", force=False, allow_summarize=False)
    check("超预算时退化到仅截断（client=None 也不崩）",
          res3.changed and len(res3.messages) > 0, res3.strategy)

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
