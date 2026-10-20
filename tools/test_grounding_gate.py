"""操作类答复事实核查（Option A）的循环级集成测试。

直接驱动主循环的「模型宣布收工」分支：用 stub hub 按剧本回话，验证当最终答复里的
具体命令/目录指令在已 fetch 源里找不到时，会 yield `grounding_gate` 事件并把缺口打回；
模型改对（只陈述源里有的，或诚实声明源里没提）后放行。

运行：python tools/test_grounding_gate.py
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

GOAL = "寻找git上的项目UI-Critic，这个项目是干嘛的，怎么样操作才能修改前端"


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
        self.rounds = []

    def chat_stream(self, spec, messages, tools=None, stop_check=None, **kw):
        self.rounds.append([dict(m) for m in messages])
        idx = min(len(self.rounds) - 1, len(self.script) - 1)
        yield "result", self.script[idx]


def _mk_agent(tmp: Path, script: list[_Res]) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    cfg.ptc_resident = False
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))
    ag.hub = _StubHub(script)
    return ag


def _run(ag: Agent, text: str, history=None, **kw):
    ag.hub.rounds = []
    return list(ag.run(history if history is not None else [], text,
                       auto_approve=True, **kw))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_ground_"))
    # 预置一条「已 fetch 的源」（role=="tool"）：模型本任务看到的 README 片段。
    # 只含 npm 安装命令与 bin 入口，未提及如何修改前端、没有 python main.py / src/components。
    seeded = [{"role": "tool", "content":
               "官方源 README：npm i -g @akins20/ui-critic，入口 bin/ui-critic.mjs；"
               "用于给 UI 截图做排名式 critique。未提及如何修改前端。"}]

    # ---------- 主场景：注水答复被打回，改对后放行 ----------
    script = [
        _Res(content="要修改前端，运行 `python main.py` 启动，前端代码在 src/components 目录。"),
        _Res(content="官方源 README 只讲了怎么用它评审你的站点，未提供修改前端的具体步骤。"),
    ]
    ag = _mk_agent(tmp, script)
    evs = _run(ag, GOAL, history=list(seeded), contract=[])
    gates = [e for e in evs if e.get("type") == "grounding_gate"]
    check("注水答复触发 grounding_gate 一次", len(gates) == 1, str(len(gates)))
    check("打回点出未溯源指令（python main.py）",
          gates and "python main.py" in str(gates[0].get("issues", [])),
          str(gates[0].get("issues") if gates else ""))
    check("打回点出未溯源目录（src/components）",
          gates and "src/components" in str(gates[0].get("issues", [])),
          str(gates[0].get("issues") if gates else ""))
    finals = [e for e in evs if e.get("type") == "assistant"]
    check("放行的是改对后的诚实答复", finals and "未提供修改前端" in finals[-1]["content"],
          finals[-1]["content"][:60] if finals else "无")

    # ---------- 反例：源里真有该命令 → 不拦 ----------
    script_ok = [_Res(content="安装：`npm i -g @akins20/ui-critic`，然后运行 `ui-critic`。")]
    ag_ok = _mk_agent(tmp, script_ok)
    evs_ok = _run(ag_ok, GOAL, history=list(seeded), contract=[])
    gates_ok = [e for e in evs_ok if e.get("type") == "grounding_gate"]
    check("源里真实存在的命令不拦", not gates_ok, str(len(gates_ok)))

    # ---------- 反例：非操作类问题不启用核查 ----------
    script_chat = [_Res(content="运行 `python main.py` 看看。")]
    ag_chat = _mk_agent(tmp, script_chat)
    evs_chat = _run(ag_chat, "帮我写一首关于秋天的诗", history=list(seeded), contract=[])
    gates_chat = [e for e in evs_chat if e.get("type") == "grounding_gate"]
    check("非操作类问题不拦（即便有未溯源命令）", not gates_chat, str(len(gates_chat)))

    # ---------- 盲区已闭：本任务没 fetch 过源，但给出了具体命令 → 必须拦，改对后放行 ----------
    script_no_src = [
        _Res(content="运行 `python main.py` 启动，前端在 src/components 目录。"),
        _Res(content="本次未调用 web_search 抓取官方文档，无法确认具体运行命令；按 PTC 常驻要求应经 "
                     "ops_batch 调 web_search 抓取后再引用。"),
    ]
    ag_ns = _mk_agent(tmp, script_no_src)
    evs_ns = _run(ag_ns, GOAL, history=[], contract=[])  # 空 history，无源
    gates_ns = [e for e in evs_ns if e.get("type") == "grounding_gate"]
    check("无源+含具体命令：触发 grounding_gate（盲区已闭）", len(gates_ns) == 1, str(len(gates_ns)))
    check("打回指明需经 ops_batch 抓取",
          gates_ns and "ops_batch" in str(gates_ns[0]),
          str(gates_ns[0] if gates_ns else ""))
    finals_ns = [e for e in evs_ns if e.get("type") == "assistant"]
    check("放行的是改为诚实声明的答复", finals_ns and "未调用 web_search" in finals_ns[-1]["content"],
          finals_ns[-1]["content"][:60] if finals_ns else "无")

    # ---------- 无源但只讲概念、无命令、未声称溯源 → 不拦（不误伤） ----------
    script_soft = [_Res(content="Marp 是用 Markdown 写幻灯片、由渲染器生成页面的工具，思路很直接。")]
    ag_soft = _mk_agent(tmp, script_soft)
    evs_soft = _run(ag_soft, GOAL, history=[], contract=[])
    gates_soft = [e for e in evs_soft if e.get("type") == "grounding_gate"]
    check("无源+无命令+未声称溯源：不拦", not gates_soft, str(len(gates_soft)))

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
