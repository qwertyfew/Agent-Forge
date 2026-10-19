"""交付物契约闸门的自测：做之前亮标准（contract_block）+ 收工时停止拦截（loop 内嵌）。

运行：python tools/test_contract_gate.py
全部断言内建，退出码 0 = 全过。纯程序测试：模型用 stub hub 按剧本回话。

背景（rerun14 实测）：11 步任务里「任务书点名的交付物不存在」的核查拦截出现
多次，全部发生在**收尾**——员工把分析做完了、上下文里全是素材，却宣布完成，
收尾闸门才发现文件没落盘，只能整轮返工。升级：契约两端。
  ① 开工：contract_block 把「必须写出哪些文件」显式写进员工 prompt；
  ② 收工：模型不再调工具想给最终答复时，程序先对契约，缺了就拦截
    （Claude Code Stop hook 同款），把缺口当观察塞回去继续跑——此刻分析
    内容都在上下文里，补写只是落盘，不用重新分析。
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
from agentforge.tasksession import contract_block, contract_paths  # noqa: E402


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
    """按剧本回话：第 n 轮返回 script[n-1]。记录每轮收到的 messages 供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.rounds: list[list[dict]] = []

    def chat_stream(self, spec, messages, tools=None, stop_check=None, **kw):
        self.rounds.append([dict(m) for m in messages])
        yield "result", self.script[len(self.rounds) - 1]


def _mk_agent(tmp: Path, script: list[_Res]) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))
    ag.hub = _StubHub(script)
    return ag


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_contract_"))

    # ---------- contract_paths / contract_block（契约前端） ----------
    goal = ("任务：分析 TODO 机制，写入 tmp/agentforge-todo-system.txt；"
            "参考 agentforge/loop.py 已有实现")
    req = contract_paths(goal, tmp)
    check("点名且不存在 → 契约项", req == ["tmp/agentforge-todo-system.txt"], repr(req))
    check("已存在的路径是材料不是交付",
          contract_paths("参考 agentforge/loop.py 写总结", tmp) == [])
    check("裸文件名不进契约（产出目录兜底）",
          contract_paths("写 report.md 交差", tmp) == [])
    blk = contract_block(goal, tmp)
    check("契约块亮出文件与拦截警告",
          "tmp/agentforge-todo-system.txt" in blk and "核对磁盘" in blk)
    check("无契约 → 空块", contract_block("纯分析任务", tmp) == "")
    (tmp / "evals").mkdir(exist_ok=True)
    (tmp / "evals" / "done.md").write_text("x", encoding="utf-8")
    check("存在文件不重复要求", "evals/done.md" not in
          contract_block("重读 evals/done.md 后写 tmp/new.txt", tmp))

    # ---------- 停止拦截（契约后端）：宣布完成但文件没写 → 拦截补写 ----------
    goal2 = "把 TODO 机制分析写入 tmp/agentforge-todo-system.txt"
    script = [
        _Res(content="任务已完成。"),                      # 想收工：文件还没写
        _Res(tool_calls=[_TC("write_file", {              # 被拦后补写
            "path": "tmp/agentforge-todo-system.txt",
            "content": "TODO 机制对比……"})]),
        _Res(content="已写入 tmp/agentforge-todo-system.txt。"),  # 再收工：过了
    ]
    ag = _mk_agent(tmp, script)
    evs = list(ag.run([], goal2, auto_approve=True,
                      contract=contract_paths(goal2, tmp)))
    gates = [e for e in evs if e.get("type") == "contract_gate"]
    check("缺交付 → 触发一次拦截", len(gates) == 1, str(len(gates)))
    check("拦截报出缺失文件", gates and gates[0]["missing"] == ["tmp/agentforge-todo-system.txt"])
    check("文件真实落盘", (tmp / "tmp" / "agentforge-todo-system.txt").exists())
    finals = [e for e in evs if e.get("type") == "assistant"]
    check("最终答复是补写后的那次（被拦草稿不当答复）",
          finals and finals[-1]["content"].startswith("已写入"), repr(finals[-1]["content"][:30]) if finals else "无")
    hist = finals[-1]["history"]
    roles = [m["role"] for m in hist]
    check("草稿与拦截消息留在上下文",
          roles.count("assistant") >= 2 and "user" in roles)
    gate_msg = next((m for m in hist if m["role"] == "user"
                     and "停止拦截" in (m.get("content") or "")), None)
    check("拦截消息点名缺失文件并要求落盘",
          gate_msg is not None and "write_file" in gate_msg["content"])

    # ---------- 拦截上限：模型就是不写 → 两次后放行（收尾闸门兜底） ----------
    script2 = [
        _Res(content="完成 1"), _Res(content="完成 2"),
        _Res(content="完成 3"),                       # 拦满 2 次，这次放行
    ]
    ag2 = _mk_agent(tmp, script2)
    goal3 = "写 tmp/never-written.txt"
    evs2 = list(ag2.run([], goal3, auto_approve=True,
                        contract=contract_paths(goal3, tmp)))
    gates2 = [e for e in evs2 if e.get("type") == "contract_gate"]
    check("最多拦两次", len(gates2) == 2, str(len(gates2)))
    finals2 = [e for e in evs2 if e.get("type") == "assistant"]
    check("拦满后最终答复放行（兜底交收尾闸门）",
          finals2 and finals2[-1]["content"] == "完成 3")

    # ---------- 无契约任务零打扰 ----------
    script3 = [_Res(content="普通答复")]
    ag3 = _mk_agent(tmp, script3)
    evs3 = list(ag3.run([], "纯聊天：什么是 todo list？", auto_approve=True))
    check("无契约不拦截", not [e for e in evs3 if e.get("type") == "contract_gate"])
    check("无契约正常答复", any(e.get("type") == "assistant"
                                and e["content"] == "普通答复" for e in evs3))

    print(f"\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
