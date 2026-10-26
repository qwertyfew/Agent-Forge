"""语义闸门（否定断言 vs 代码事实）的自测。

运行：python tools/test_claims_gate.py
全部断言内建，退出码 0 = 全过。

背景（rerun22 实测）：差距清单 10 条里 4 条写「AgentForge 缺乏 X」，而 X 在
agentforge/ 里明确有实现（context.py 的 Compressor、permissions.py 的 Guard、
taskstore.py）。路径闸门全放行——它只判断文件在不在，判断不了文件里有没有。
本层只做一件事：抓「否定词 + 功能名」，去代码里找定义行；找到即判矛盾并
给出真实 path:line，让模型一次改对，而不是整篇返工。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.core.config import Config                        # noqa: E402
from agentforge.evidence import audit_claims                # noqa: E402
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


def _mk_workspace() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="af_claims_"))
    pkg = tmp / "agentforge"
    (pkg / "tools").mkdir(parents=True, exist_ok=True)
    (pkg / "context.py").write_text(
        "class CompactResult:\n    pass\n\n\nclass Compressor:\n"
        "    \"\"\"按 token 预算压缩历史。\"\"\"\n", encoding="utf-8")
    (pkg / "permissions.py").write_text(
        "class PermissionDenied(Exception):\n    pass\n\n\nclass Guard:\n"
        "    def check_command(self, cmd):\n        return True\n", encoding="utf-8")
    (pkg / "taskstore.py").write_text(
        "class Task:\n    pass\n\n\nclass TaskStore:\n    pass\n", encoding="utf-8")
    (pkg / "tools" / "notes.py").write_text("def notes_tool():\n    pass\n", encoding="utf-8")
    return tmp


def main() -> None:
    tmp = _mk_workspace()

    # ---------- 纯函数：真矛盾要抓，真缺失/对方侧不误伤 ----------
    false_neg = (
        "| 维度 | AgentForge 现状 |\n"
        "| :-- | :-- |\n"
        "| 上下文压缩 | `agentforge/tools/notes.py`：提供笔记存储，"
        "但缺乏基于 token 预算的上下文压缩逻辑。 |\n"
        "| 权限 | `agentforge/tools/notes.py`：缺乏细粒度的权限审批流程。 |\n"
    )
    issues = audit_claims(false_neg, tmp)
    kinds = {i.token for i in issues}
    check("抓到「缺乏上下文压缩」假结论", "上下文压缩" in kinds, str(kinds))
    check("抓到「缺乏权限审批」假结论", "权限审批" in kinds, str(kinds))
    detail = " ".join(i.detail for i in issues)
    check("给出真实实现位置（context.py 的 Compressor）",
          "agentforge/context.py" in detail and "Compressor" in detail, detail[:200])
    check("给出真实实现位置（permissions.py 的 Guard）",
          "agentforge/permissions.py" in detail and "Guard" in detail, detail[:200])
    check("点明引用文件不是实现处", "不是该能力的实现处" in detail, detail[:200])

    truly_absent = (
        "| 维度 | AgentForge 现状 |\n"
        "| 量子计算 | `agentforge/tools/`：缺乏量子计算模块，没有 QAOA 实现。 |\n"
    )
    check("真缺失不误判（找不到定义就沉默）",
          audit_claims(truly_absent, tmp) == [], str(audit_claims(truly_absent, tmp))[:200])

    external_side = (
        "| 子代理 | `external/mini-swe-agent/README.md`："
        "对方缺乏子代理机制，没有 subagent。 |"
    )
    check("对方侧否定断言不误判",
          audit_claims(external_side, tmp) == [], str(audit_claims(external_side, tmp))[:200])

    positive = "| 上下文压缩 | `agentforge/context.py`：已有 Compressor，按 token 预算裁剪。 |"
    check("肯定表述不拦", audit_claims(positive, tmp) == [])

    # ---------- 端到端：经 ops_batch 写盘被拦，改正后放行 ----------
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))

    bad_doc = (
        "# 差距清单\n\n"
        "| 维度 | AgentForge 现状 |\n"
        "| :-- | :-- |\n"
        "| 上下文压缩 | `agentforge/tools/notes.py`：缺乏基于 token 预算的上下文压缩逻辑。 |\n"
    )
    script = ("print(call('write_file', path='evals/gap.md', "
              "content=" + repr(bad_doc) + "))")
    ag.hub = _StubHub([
        _Res(tool_calls=[_TC("ops_batch", {"script": script})]),
        _Res(content="已修正。"),
    ])
    evs = list(ag.run([], "写一份差距清单", auto_approve=True))
    out = next((e.get("output") or "" for e in evs if e.get("type") == "tool_result"), "")
    check("写盘被语义闸门拦下", "语义核查未通过" in out, out[:200])
    check("未落盘", not (tmp / "evals" / "gap.md").exists())
    check("拒绝原因带真实实现位置", "agentforge/context.py" in out, out[:300])

    good_doc = (
        "# 差距清单\n\n"
        "| 维度 | AgentForge 现状 |\n"
        "| :-- | :-- |\n"
        "| 上下文压缩 | `agentforge/context.py`：已有 Compressor，按 token 预算裁剪。 |\n"
    )
    script2 = ("print(call('write_file', path='evals/gap.md', "
               "content=" + repr(good_doc) + "))")
    ag.hub = _StubHub([
        _Res(tool_calls=[_TC("ops_batch", {"script": script2})]),
        _Res(content="已写出。"),
    ])
    list(ag.run([], "写一份差距清单", auto_approve=True))
    check("改正后正常落盘", (tmp / "evals" / "gap.md").is_file())

    # ---------- rerun25 回归：圆括号引用也要过核查 ----------
    # 模型把引用从反引号改写成圆括号 (lc_loop/code.py:7-13)，旧门槛
    # `if content and "`" in content` 直接跳过核查，9 条虚构路径全部落盘。
    paren_doc = (
        "# 差距清单（圆括号引用形态）\n\n"
        "| 对比点 | 对方做法 | AgentForge 现状 |\n"
        "| :-- | :-- | :-- |\n"
        "| agent loop | 极简 while 循环 (lc_loop/code.py:7-13) "
        "| 多步调度 (agentforge/loop.py:100-400) |\n"
        "| 上下文压缩 | 滑动窗口 (lc_compact/code.py:30-50) "
        "| Compressor 多级压缩 (agentforge/context.py:190) |\n"
    )
    check("样本确实不含反引号", "`" not in paren_doc)
    script3 = ("print(call('write_file', path='evals/paren.md', "
               "content=" + repr(paren_doc) + "))")
    # 写前证据门槛（writepolicy）会在「零读 + 引用密集」时先拦，那属于另一层。
    # 这里要让引用核查本身受检：同一次 run 里先真读一个文件，把证据门槛放行。
    (tmp / "agentforge").mkdir(parents=True, exist_ok=True)
    (tmp / "agentforge" / "loop.py").write_text("class Agent:\n    pass\n", encoding="utf-8")
    script0 = "print(call('read_file', path='agentforge/loop.py'))"
    ag.hub = _StubHub([
        _Res(tool_calls=[_TC("ops_batch", {"script": script0})]),
        _Res(tool_calls=[_TC("ops_batch", {"script": script3})]),
        _Res(content="已写出。"),
    ])
    evs3 = list(ag.run([], "写一份差距清单", auto_approve=True))
    out3 = "\n".join(e.get("output") or "" for e in evs3
                     if e.get("type") == "tool_result")
    check("圆括号虚构引用也被拦下", "引用核查未通过" in out3, out3[:200])
    check("圆括号形态未落盘", not (tmp / "evals" / "paren.md").exists())

    print("\n结果：全部通过")


if __name__ == "__main__":
    main()
