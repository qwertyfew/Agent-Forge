"""P32-a 回归测试：PTC 直调自动转译——不再拒绝返工。

运行：python tools/test_p32_autowrap.py
全部断言内建，退出码 0 = 全过。

背景（berry 任务实证）：12B 模型看到「可编入脚本清单」里的工具名仍会凭
训练先验直调，旧硬闸「拒绝+教育」只保证正确、保证不了省轮次——第一轮
web_search 直调被拒，白烧一整轮。P32-a 改为自动转译：原地改写成等价
ops_batch 脚本照常执行，第一轮就拿真结果；历史里留下的就是脚本形态。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                        # noqa: E402
from agentforge.loop import Agent                           # noqa: E402
from agentforge.permissions import Guard                    # noqa: E402
from agentforge.tools import build_registry                 # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


class _TC:
    def __init__(self, name, args=None):
        self.name = name
        self.arguments = args or {}


class _Res:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.thinking = None
        self.tokens_per_second = 0.0
        self.eval_count = 0
        self.prompt_eval_count = 0


class _StubHub:
    def __init__(self, script):
        self.script = list(script)
        self.rounds: list[list[dict]] = []

    def chat_stream(self, spec, messages, tools=None, stop_check=None, **kw):
        self.rounds.append([dict(m) for m in messages])
        yield "result", self.script[len(self.rounds) - 1]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_p32_"))
    (tmp / "sales.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    check("PTC 常驻默认开启（前提）", bool(cfg.ptc_resident))
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))

    # ---------- ① 直调 read_file → 自动转译执行，第一轮拿到真结果 ----------
    ag.hub = _StubHub([
        _Res(tool_calls=[_TC("read_file", {"path": "sales.csv"})]),
        _Res(content="完成。"),
    ])
    evs = list(ag.run([], "读个文件", auto_approve=True))
    tr = next((e for e in evs if e.get("type") == "tool_result"), None) or {}
    out = tr.get("output") or ""
    check("P32 直调不再被拒：第一轮就拿到文件内容",
          tr.get("name") == "ops_batch" and "a,b" in out and "已被禁用" not in out,
          repr(out)[:200])
    note = next((e for e in evs if e.get("type") == "notify"), None) or {}
    check("P32 自动转译有通知", "自动转 ops_batch" in (note.get("text") or ""),
          str(note)[:150])

    # ---------- ② 历史/下一轮消息里留下的是脚本形态 ----------
    ag.hub.rounds = []
    ag.hub = _StubHub([
        _Res(tool_calls=[_TC("read_file", {"path": "sales.csv"})]),
        _Res(tool_calls=[_TC("list_dir", {"path": "."})]),
        _Res(content="完成。"),
    ])
    evs = list(ag.run([], "再来一轮", auto_approve=True))
    # 改写后的 assistant(tool_calls) 出现在第二轮发给模型的历史里
    acalls = []
    for rnd in ag.hub.rounds:
        m = next((m for m in rnd if m.get("role") == "assistant"
                  and m.get("tool_calls")), None)
        if m:
            acalls = m["tool_calls"]
            break
    check("P32 历史里留下的就是 ops_batch 脚本形态",
          bool(acalls) and acalls[0]["function"]["name"] == "ops_batch"
          and "call('read_file'" in str(acalls[0]["function"]["arguments"]),
          repr(acalls)[:250])

    # ---------- ③ 直调未知工具 → 脚本内报未知工具（不 crash） ----------
    ag.hub = _StubHub([
        _Res(tool_calls=[_TC("no_such_tool_xyz", {"x": 1})]),
        _Res(content="完成。"),
    ])
    evs = list(ag.run([], "幻觉工具", auto_approve=True))
    tr = next((e for e in evs if e.get("type") == "tool_result"), None) or {}
    out = tr.get("output") or ""
    check("P32 幻觉工具名安全落地（脚本内报未知工具）",
          "未知工具 no_such_tool_xyz" in out, repr(out)[:200])

    # ---------- ④ 累计 3 次起通知升级 ----------
    ag.hub = _StubHub([
        _Res(tool_calls=[_TC("list_dir", {"path": "."})]),
        _Res(tool_calls=[_TC("list_dir", {"path": "."})]),
        _Res(tool_calls=[_TC("list_dir", {"path": "."})]),
        _Res(content="完成。"),
    ])
    evs = list(ag.run([], "连着直调三次", auto_approve=True))
    notes = [e for e in evs if e.get("type") == "notify" and "自动转" in (e.get("text") or "")]
    check("P32 第 3 次直调通知升级为硬提醒",
          len(notes) >= 1 and any("省轮次" in (e.get("text") or "") for e in notes),
          repr([n.get("text") for n in notes])[:200])

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
