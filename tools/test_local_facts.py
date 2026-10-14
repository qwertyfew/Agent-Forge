"""本机事实核查的自测：sources.py 第二路判定 + orchestrator._verify_gate 的接入。

运行：python tools/test_local_facts.py
全部断言内建，退出码 0 = 全过。纯程序测试，不需要模型在线、不需要联网。

用例取自真实事故（端到端实测）：让 agent「列出工作区根目录下的文件」，
25 秒、0 次工具调用，答复里凭空列着 agent-forge.json / core/ / ui/ / venv/
这些工作区里根本不存在的东西，而 route 把这条任务判成了「写作」类。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import sources as sg                    # noqa: E402
from agentforge.orchestrator import _verify_gate        # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def _call(name: str, args: dict | None = None) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}


def _tool(name: str) -> dict:
    return {"role": "tool", "content": f"{name} 的结果"}


# 事故现场：真实任务书 + 真实编造的答复（原样，只去掉行尾空白）
ASK = "先列出当前工作区根目录下的文件（不要读内容），然后用一句话说明你都做了什么。"
FABRICATED = (
    "以下是当前工作区根目录下的文件列表：\n\n"
    "agent-forge.json\nconfig.yaml\ncore/\ndocs/\nscripts/\nstart.bat\nstop.bat\n"
    "tools/\nui/\nvenv/\nREADME.md\n\n"
    "我已列出工作区根目录下的所有文件。"
)


# ---------------------------------------------------------------- 判定

def test_asks_workspace_state() -> None:
    check("任务书：要列工作区文件 → True", sg.asks_workspace_state(ASK))
    check("任务书：问有哪些目录 → True", sg.asks_workspace_state("这个项目有哪些目录？"))
    check("任务书：看一下某文件 → True",
          sg.asks_workspace_state("看一下 agentforge/loop.py 里工具超时是怎么处理的。"))
    check("任务书：让造东西（设计目录结构）→ False",
          not sg.asks_workspace_state("帮我设计一下这个项目的目录结构"))
    check("任务书：让造东西（生成文件清单）→ False",
          not sg.asks_workspace_state("生成一个文件列表，列出工作区里的文件"))
    check("任务书：与工作区无关 → False", not sg.asks_workspace_state("今天天气怎么样"))
    check("任务书：空 → False", not sg.asks_workspace_state(""))
    check("任务书：None 安全", not sg.asks_workspace_state(None))
    # GM 回归：「实现」是检视提问的高频词（「X 是怎么实现的」「X 的实现」），
    # 原来一律按创建动作处理，这类最日常的检视问题整类漏过闸门。
    check("检视：问某处是怎么实现的 → True",
          sg.asks_workspace_state("看一下 agentforge/loop.py 里工具超时是怎么实现的。"))
    check("检视：问某处的实现 → True",
          sg.asks_workspace_state("看看这个项目的实现方式。"))
    check("检视：如何实现的 → True",
          sg.asks_workspace_state("读一下 context.py，看看压缩是如何实现的。"))
    check("创建：实现一个脚本 → False",
          not sg.asks_workspace_state(
              "实现一个导出脚本，顺便列出工作区里有哪些文件"))
    check("创建：重新实现函数 → False",
          not sg.asks_workspace_state(
              "重新实现 parse_args 这个函数，列出它现在有哪些配置"))


def test_ran_local_inspection() -> None:
    check("没调任何工具 → False", not sg.ran_local_inspection([]))
    check("None → False", not sg.ran_local_inspection(None))
    check("只调了 web_search → False",
          not sg.ran_local_inspection([_call("web_search"), _tool("web_search")]))
    for t in ("list_dir", "find_files", "read_file", "search_code", "impact_of",
              "git_status", "git_log", "git_diff", "git_show"):
        check(f"调过 {t} → True", sg.ran_local_inspection([_call(t), _tool(t)]))
    check("跑过 run_command 也算读过（宁宽不宁窄）",
          sg.ran_local_inspection([_call("run_command"), _tool("run_command")]))
    check("调过 fetch_url 不算读过工作区",
          not sg.ran_local_inspection([_call("fetch_url"), _tool("fetch_url")]))


def test_local_fact_tokens() -> None:
    toks = sg.local_fact_tokens(FABRICATED)
    check("认出编造的文件名（agent-forge.json）", "agent-forge.json" in toks, repr(toks))
    check("认出编造的目录名（core/）", "core/" in toks, repr(toks))
    check("认出真实存在的名字（README.md）", "README.md" in toks, repr(toks))
    check("去重且保序", toks == list(dict.fromkeys(toks)), repr(toks))
    check("条数受限", len(toks) <= sg.MAX_HITS, repr(toks))
    check("带目录前缀的文件名也认（web/index.html）",
          "web/index.html" in sg.local_fact_tokens("改动在 web/index.html 里。"),
          repr(sg.local_fact_tokens("改动在 web/index.html 里。")))
    check("Windows 绝对路径也认",
          any("D:\\" in t for t in sg.local_fact_tokens(r"文件在 D:\proj\a\b.py 下。")),
          repr(sg.local_fact_tokens(r"文件在 D:\proj\a\b.py 下。")))
    check("任务书里自己提过的名字不算（exclude_text）",
          "web/index.html" not in sg.local_fact_tokens(
              "已改好 web/index.html。", exclude_text="给 web/index.html 加个按钮"),
          repr(sg.local_fact_tokens("已改好 web/index.html。",
                                    exclude_text="给 web/index.html 加个按钮")))
    check("裸词不当文件名（框架 / 工具）",
          sg.local_fact_tokens("这套框架和工具都不错。") == [],
          repr(sg.local_fact_tokens("这套框架和工具都不错。")))
    check("网址不算本机文件",
          sg.local_fact_tokens("见 https://example.com/docs/x.html") == [],
          repr(sg.local_fact_tokens("见 https://example.com/docs/x.html")))
    check("空总结安全", sg.local_fact_tokens("") == [])
    # GM 回归：Windows 反斜杠的相对路径（agentforge\loop.py）——原来三条
    # 识别路（斜杠相对 / 盘符绝对 / 斜杠目录）都走不通，整条漏判。
    toks_bs = sg.local_fact_tokens(r"改动落在 agentforge\loop.py 和 web\index.html。")
    check("反斜杠相对路径也认（agentforge\\loop.py）",
          any("loop.py" in t for t in toks_bs), repr(toks_bs))
    check("反斜杠相对路径也认（web\\index.html）",
          any("index.html" in t for t in toks_bs), repr(toks_bs))
    check("反斜杠路径被任务书点过名也不算（exclude_text 归一）",
          not sg.local_fact_tokens(r"已改好 agentforge\loop.py。",
                                   exclude_text=r"改 agentforge\loop.py"),
          repr(sg.local_fact_tokens(r"已改好 agentforge\loop.py。",
                                    exclude_text=r"改 agentforge\loop.py")))


def test_needs_local_note() -> None:
    zero_tools: list[dict] = []
    check("事故现场：要列工作区 + 零工具调用 + 列着文件名 → 出手",
          sg.needs_local_note(FABRICATED, zero_tools, ASK) != [],
          repr(sg.needs_local_note(FABRICATED, zero_tools, ASK)))
    check("读过工作区 → 不出手",
          sg.needs_local_note(FABRICATED, [_call("list_dir"), _tool("list_dir")], ASK) == [])
    check("跑过命令也算读过 → 不出手",
          sg.needs_local_note(FABRICATED, [_call("run_command"), _tool("run_command")], ASK) == [])
    check("任务书不是在问现状 → 不出手",
          sg.needs_local_note(FABRICATED, zero_tools, "帮我写一篇项目介绍文章") == [])
    check("任务书让造东西 → 不出手",
          sg.needs_local_note(FABRICATED, zero_tools, "帮我设计目录结构并写进 README") == [])
    check("没列任何文件名（纯定性）→ 不出手",
          sg.needs_local_note("工作区里东西不多，结构比较简单。", zero_tools, ASK) == [])
    check("单个名字且没在列清单 → 不出手（避免误伤「已创建 foo.py」）",
          sg.needs_local_note("本次改动集中在 renderHistory。", zero_tools, ASK) == [],
          repr(sg.needs_local_note("本次改动集中在 renderHistory。", zero_tools, ASK)))
    check("单个名字 + 明确在列清单 → 出手",
          sg.needs_local_note("根目录下只有 README.md。", zero_tools, ASK) != [])
    check("正常收尾（真读过、说自己改了哪几个文件）→ 不出手",
          sg.needs_local_note(
              "已修改 web/index.html 与 tools/test_task_view.mjs。",
              [_call("read_file"), _tool("read_file")], ASK) == [])


def test_blocks() -> None:
    hits = sg.needs_local_note(FABRICATED, [], ASK)
    block = sg.local_unverified_block(hits)
    check("声明块带系统侧标记", "【系统侧核查】" in block)
    check("声明块点明缺的是「读工作区」", "list_dir" in block and "工作区" in block)
    check("声明块逐条列出未经核实的名字", any("agent-forge.json" in ln for ln in block.splitlines()))
    check("声明块声明程序完成、不依赖模型自报", "不依赖模型自报" in block)

    prompt = sg.LOCAL_FACTS_CORRECTIVE_PROMPT.format(
        tokens="\n".join(f"- {t}" for t in hits), summary=FABRICATED)
    check("修正提示点出被点出的项", "agent-forge.json" in prompt)
    check("修正提示要求先 list_dir", "list_dir" in prompt)
    check("修正提示给出「如实说未核实」的退路", "未核实" in prompt)


# ---------------------------------------------------------------- 接入闸门

class FakeCfg:
    verify_gate = True
    sources_gate = True
    local_facts_gate = True
    workspace_root = str(Path(tempfile.mkdtemp(prefix="af_localfacts_")))


class FakeAgent:
    """假 agent：run() 只负责模拟「一轮修正」。

    fix=None  → 修正轮什么也没做（没读工作区、答复照旧）
    fix=文本  → 修正轮真的读了工作区（往历史里塞 list_dir 调用），并改写答复
    fix=""   → 修正轮读了工作区，但没改写答复
    """

    cfg = FakeCfg()

    def __init__(self, fix: str | None = None):
        self.fix = fix
        self.prompts: list[str] = []

    def run(self, convo, prompt, **kw):
        self.prompts.append(prompt)
        if self.fix is not None:
            convo.append(_call("list_dir", {"path": "."}))
            convo.append(_tool("list_dir"))
            yield {"type": "assistant", "history": convo, "content": self.fix}
        else:
            yield {"type": "assistant", "history": convo, "content": ""}


def test_gate_wiring() -> None:
    # 1) 零工具调用 + 编造清单 → 修正一轮后仍没读 → 追加声明 + 播报 verify 事件
    agent = FakeAgent(fix=None)
    holder = {"summary": FABRICATED, "convo": []}
    events = list(_verify_gate(agent, ASK, holder))
    check("闸门接入：先给了模型一轮修正机会", len(agent.prompts) == 1, repr(agent.prompts))
    check("闸门接入：修正提示要求它去看", "list_dir" in agent.prompts[0])
    check("闸门接入：没改 → 答复末尾出现系统侧声明",
          "【系统侧核查】" in holder["summary"], holder["summary"][-200:])
    check("闸门接入：播报了本机事实核查事件",
          any(e.get("type") == "verify" and e.get("unverified_local") for e in events),
          repr([e.get("type") for e in events]))

    # 2) 修正轮真去读了工作区 → 不再追加声明
    agent2 = FakeAgent(fix="根目录下有 tools/ 与 README.md。")
    holder2 = {"summary": FABRICATED, "convo": []}
    events2 = list(_verify_gate(agent2, ASK, holder2))
    check("闸门接入：修正轮读了工作区 → 不加声明",
          "【系统侧核查】" not in holder2["summary"], holder2["summary"][-200:])
    check("闸门接入：修正后的答复被采纳", holder2["summary"].startswith("根目录下有"))
    check("闸门接入：没问题时不播报核查事件",
          not any(e.get("type") == "verify" for e in events2))

    # 3) 修正轮读了工作区但答复原样退回（读了就够，不再追究）→ 不追加
    agent3 = FakeAgent(fix="")
    holder3 = {"summary": FABRICATED, "convo": []}
    list(_verify_gate(agent3, ASK, holder3))
    check("闸门接入：读过工作区即放行（哪怕答复没重写）",
          "【系统侧核查】" not in holder3["summary"], holder3["summary"][-200:])

    # 4) 本来就读过工作区 → 既不修正也不加声明
    agent4 = FakeAgent()
    holder4 = {"summary": FABRICATED,
               "convo": [_call("find_files"), _tool("find_files")]}
    list(_verify_gate(agent4, ASK, holder4))
    check("闸门接入：本来就看过现场 → 不打扰", agent4.prompts == [])
    check("闸门接入：本来就看过现场 → 正文原样", holder4["summary"] == FABRICATED)

    # 5) 开关关掉 → 一律不出手
    class FakeCfgOff(FakeCfg):
        local_facts_gate = False

    class FakeAgentOff(FakeAgent):
        cfg = FakeCfgOff()

    agent5 = FakeAgentOff()
    holder5 = {"summary": FABRICATED, "convo": []}
    list(_verify_gate(agent5, ASK, holder5))
    check("闸门接入：local_facts_gate=false 时不出手",
          holder5["summary"] == FABRICATED and agent5.prompts == [])

    # 6) 纯写作任务（不是问现状）→ 不误伤
    agent6 = FakeAgent()
    holder6 = {"summary": FABRICATED, "convo": []}
    list(_verify_gate(agent6, "帮我写一篇项目介绍文章", holder6))
    check("闸门接入：纯写作任务不误伤",
          holder6["summary"] == FABRICATED and agent6.prompts == [])


def main() -> None:
    test_asks_workspace_state()
    test_ran_local_inspection()
    test_local_fact_tokens()
    test_needs_local_note()
    test_blocks()
    test_gate_wiring()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
