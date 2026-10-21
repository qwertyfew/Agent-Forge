"""会话事件日志（journal）与压缩可重放性的自测。

运行：python tools/test_compact_journal.py
全部断言内建，退出码 0 = 全过。

背景：压缩此前只改内存 messages（context.Compressor），日志里没有任何痕迹。
一旦触发压缩，「模型当轮看到的是原文还是摘要」就无从查证——评测归因只能猜。
本测试卡住三件事：
1. 压缩必须写出 compact 事件 + 前后快照；
2. journal.replay() 折叠出的上下文必须与模型实际收到的一致（可重放）；
3. 内部回灌的 user 消息要标成 source=system，别与用户原话混为一谈。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                    # noqa: E402
from agentforge.context import measure                  # noqa: E402
from agentforge.journal import Journal, replay          # noqa: E402
from agentforge.loop import Agent, _msg_source          # noqa: E402
from agentforge.permissions import Guard                # noqa: E402
from agentforge.tools import build_registry             # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


class _Hub:
    """压缩只走截断策略时不需要真模型；这里只保证 hub 存在。"""

    def chat_stream(self, *a, **kw):  # pragma: no cover
        raise AssertionError("本测试不应调用模型")


def build_agent(tmp: Path, *, threshold: int = 2000) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.state_dir = tmp / ".agentforge"
    cfg.compact_auto = True
    cfg.compact_threshold = threshold
    cfg.compact_keep_chars = 400
    cfg.keep_recent_tool_results = 1
    cfg.compact_keep_recent = 1
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    return Agent(cfg, client=None, registry=registry, guard=guard,
                 router=None, hub=_Hub())


def big_history(n: int = 8) -> list[dict]:
    msgs = [{"role": "system", "content": "SYS-提示"}]
    msgs.append({"role": "user", "content": "分析仓库并写报告"})
    for i in range(n):
        msgs.append({"role": "assistant", "content": f"读第 {i} 个文件",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": json.dumps({"path": f"f{i}.py"})}}]})
        msgs.append({"role": "tool", "content": f"文件 {i} 内容 " + "x" * 900})
    return msgs


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ag = build_agent(tmp)
        ag.journal = Journal.open(ag.cfg, "t-compact")
        check("journal 打开成功", ag.journal is not None and ag.journal.path.parent.is_dir())

        before = big_history()
        ag._journal_step = 3
        # 新签名：system 与历史分开传（system 每轮重贴，压缩只压历史），
        # 返回的也只是压缩后的历史——这样压缩结果才能累积，不必每轮重摘。
        system_msgs, history = before[:1], before[1:]
        after, ev = ag._compact(system_msgs, history)

        check("超预算触发压缩", ev is not None and ev["type"] == "compact", str(ev))
        check("压缩确实变小", ev["after"] < ev["before"], f'{ev["before"]}→{ev["after"]}')
        check("返回的上下文确实比原文短",
              measure(after) < measure(history), f"{measure(history)}→{measure(after)} 字符")

        events = [json.loads(x) for x in ag.journal.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        kinds = [e["type"] for e in events]
        check("事件流含 system 事件", "system" in kinds, str(kinds))
        check("事件流含 compact 事件", "compact" in kinds, str(kinds))
        cmp_ev = [e for e in events if e["type"] == "compact"][-1]
        check("compact 事件带前后快照路径",
              bool(cmp_ev.get("before_snapshot")) and bool(cmp_ev.get("after_snapshot")),
              str(cmp_ev))

        before_snap = json.loads(Path(cmp_ev["before_snapshot"]).read_text(encoding="utf-8"))
        after_snap = json.loads(Path(cmp_ev["after_snapshot"]).read_text(encoding="utf-8"))
        check("压缩前快照是完整原文",
              len(before_snap["messages"]) == len(before) - 1, str(len(before_snap["messages"])) + "/系统提示单独记")
        check("压缩后快照等于模型实际收到的历史",
              after_snap["messages"] == after, "快照与返回的历史不一致")

        rp = replay(ag.journal.path)
        check("重放取到最后生效的系统提示", rp["system"] == "SYS-提示", rp["system"][:40])
        check("重放包含压缩后的上下文（而非原文）",
              rp["messages"] == after, f'replay={len(rp["messages"])} 实际={len(after)}')
        check("重放记录了压缩次数", rp["compacts"] == 1, str(rp["compacts"]))

        # 压缩之后新增的消息，重放里必须接在快照后面
        ag.journal.message({"role": "assistant", "content": "写完了"})
        rp2 = replay(ag.journal.path)
        check("压缩后的新消息接在快照之后",
              rp2["messages"][-1]["content"] == "写完了"
              and len(rp2["messages"]) == len(after) + 1,
              str(len(rp2["messages"])))

        # ---------- 内部回灌的归属 ----------
        check("用户原话标 user",
              _msg_source({"role": "user", "content": "把报告写到 evals/x.md"}) == "user")
        check("停止拦截标 system",
              _msg_source({"role": "user", "content": "【停止拦截·交付物未落盘】..."}) == "system")
        check("中途补充标 system",
              _msg_source({"role": "user", "content": "（用户中途补充，立即采纳并调整方向）x"}) == "system")
        check("assistant 不带来源",
              _msg_source({"role": "assistant", "content": "好的"}) == "")

        # ---------- 关闭开关时不该产生任何文件 ----------
        cfg2 = Config.load(str(ROOT / "config.yaml"))
        cfg2.workspace_root = tmp / "off"
        cfg2.state_dir = tmp / "off" / ".agentforge"
        cfg2.journal = False
        j = Journal.open(cfg2, "t-off")
        check("journal=False 时不建日志", j is None or not (Path(j.dir) / "events.jsonl").exists())

    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
