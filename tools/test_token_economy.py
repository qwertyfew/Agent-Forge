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
from agentforge.context import (Compressor, measure,           # noqa: E402
                                truncate_tool_results)
from agentforge.loop import tidy_tool_args                     # noqa: E402
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


def _tool_msg_list(start: int, end: int) -> list[dict]:
    return [_tool_msg(i) for i in range(start, end + 1)]


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

    # ---------- 一手来源必须熬过压缩（rerun61）----------
    # 现场：README 抓到了，下一轮压缩把 6 万字摘成 6 千，命令全丢，模型只好答
    # 「没拿到 README」，闸门也只能判「命令无据」——东西是它扔的。
    readme = ("[实测 · API] https://api.github.com/repos/a/b/readme\n"
              "[来源] 经 GitHub 官方 API 取到的 README 原文\n# @a/b\n"
              + ("这里是很长的说明文字。" * 300)
              + "\n## Installation\nnpm install -g @a/b\nmarp --pptx deck.md\n")
    ev = [{"role": "user", "content": "任务"},
          {"role": "tool", "content": readme, "tool_call_id": "x1"},
          *_tool_msg_list(2, 12)]
    res4 = comp.compact(ev, "auto", force=False)
    kept = [m for m in res4.messages if m.get("role") == "tool"][0]["content"]
    check("抓到的正文被压时标成「保留源」", "保留源" in kept, kept[:80])
    check("源里的安装命令没被压掉", "npm install -g @a/b" in kept, kept[-200:])
    check("源里的参数行也没被压掉", "marp --pptx deck.md" in kept, kept[-200:])
    check("仍然显著变短（不是原样照抄）", len(kept) < len(readme), f"{len(kept)}/{len(readme)}")

    # ---------- 反复压缩不许「越压越长」（rerun62 现场）----------
    # 日志里 30 次 `truncate 3872→3878`：每轮都给已压缩的消息再盖一次
    # `[已压缩] `（正好 6 字），压了 30 轮一个字没省，还多花 30 对快照。
    cur = ev
    lens = []
    for _ in range(4):
        cur = truncate_tool_results(cur, keep_recent=1)
        lens.append(len([m for m in cur if m.get("role") == "tool"][0]["content"]))
    check("反复压缩长度单调不增", lens == sorted(lens, reverse=True), str(lens))
    check("第二轮起不再重复盖前缀（不会越压越长）", len(set(lens[1:])) == 1, str(lens))
    again = [m for m in cur if m.get("role") == "tool"][0]["content"]
    check("证据命令行熬过四轮压缩",
          "npm install -g @a/b" in again and "marp --pptx deck.md" in again, again[-160:])
    check("没东西可压时返回原列表（不刷压缩事件）",
          truncate_tool_results(cur, keep_recent=1) is cur)

    # ---------- 摘要压缩必须「累积」，且不许吃掉任务原文（rerun62）----------
    # 现场：`60640→6087` 之后下一轮又 `63703`——compaction 只改了本轮请求的副本，
    # ctx 一直是原始历史，于是每轮都从 6 万字重摘一遍，摘要白做、快照翻倍。
    cfg3 = Config.load(str(ROOT / "config.yaml"))
    cfg3.workspace_root = str(tmp)
    cfg3.keep_recent_tool_results = 1
    comp3 = Compressor(cfg3, client=None)
    calls: list[int] = []

    def _fake_summary(chunk, key):
        calls.append(1)
        return "摘要：双方讨论了安装方式。" * 6
    comp3._summarize = _fake_summary

    task = "任务：找一个 Markdown 转 pptx 的命令行项目。必须给出安装命令，并核对 npm 包名。"
    big = ("[实测 · API] https://api.github.com/repos/a/b/readme\n[来源] README 原文\n"
           + "说明。" * 1200 + "\nnpm install -g @a/b\nmarp deck.md --pptx\n")
    chain = [{"role": "user", "content": task},
             {"role": "assistant", "content": "",
              "tool_calls": [{"function": {"name": "ops_batch", "arguments": "{}"}}]},
             {"role": "tool", "content": big, "tool_call_id": "c1"},
             {"role": "assistant", "content": "先看 README。"},
             {"role": "user", "content": "继续，把命令给我。"},
             {"role": "tool", "content": "最近结果", "tool_call_id": "c2"}]
    r1 = comp3.compact(chain, "auto", force=True, allow_summarize=True)
    head1 = str(r1.messages[0]["content"])
    check("摘要压缩生效", r1.strategy == "summarize" and r1.after < r1.before,
          f"{r1.strategy} {r1.before}->{r1.after}")
    check("任务原文被钉在摘要里（验收条件不能摘丢）", task in head1, head1[:200])
    check("老化在前、摘要在后时命令仍被捞回",
          "npm install -g @a/b" in head1 and "marp deck.md --pptx" in head1, head1[-200:])
    r2 = comp3.compact(r1.messages, "auto", force=True, allow_summarize=True)
    head2 = str(r2.messages[0]["content"])
    check("再压一轮命令不丢（继承上一代钉住的）",
          "npm install -g @a/b" in head2 and task in head2, head2[-200:])
    check("已是最简摘要时不再重复调用摘要模型", len(calls) == 1, f"calls={len(calls)}")
    check("压不动就不谎报压缩", r2.strategy != "summarize" or r2.after < r2.before,
          f"{r2.strategy} {r2.before}->{r2.after}")

    # ---------- 退化工具参数不许原样进历史（rerun62）----------
    # 现场：一次 ops_batch 的参数 50228 字，其中 50k 是 `<|channel>thought` 连吐；
    # 它此后每轮重发，直接把上下文撑爆（紧跟着 6 次摘要压缩）。
    degenerate = "<|channel>thought\n" * 3000
    tidied = tidy_tool_args({"script": degenerate})
    check("退化参数被折叠到可忽略", len(tidied["script"]) < 500, f"{len(tidied['script'])}")
    check("控制标记被剥掉", "<|channel" not in tidied["script"], tidied["script"][:60])
    normal = {"script": "for i in range(3):\n    print(call('read_file', path='a.py'))\n"}
    check("正常脚本一个字都不改", tidy_tool_args(normal)["script"] == normal["script"])
    check("短参数不参与清洗", tidy_tool_args({"query": "marp cli"})["query"] == "marp cli")

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
