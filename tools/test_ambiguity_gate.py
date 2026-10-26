"""确定性澄清触发（ambiguity gate）的自测：loop._lookup_signal /
_has_clarify_intent + Agent.run 端到端的弹窗插桩。

运行：python tools/test_ambiguity_gate.py
全部断言内建，退出码 0 = 全过。纯程序测试：模型用 stub hub 按剧本回话，
工具走真实注册表（list_dir 在临时工作区真执行），澄清答复走评测脚本模式。

背景（2026-10-15 评测 r1 两轮同因失败）：检索不到时模型把澄清问题写在
回复文本里而不是调 ask_user——意图对、通道错。本闸门把触发下沉到架构层：
检索信号（未命中/多候选）由程序按工具层固定格式识别，模型收尾追问时由
harness 亲自弹窗拿答复再继续，不再指望提示词管住小模型。

**2026-10-21 起按 PTC 真实形态测**：`ptc_resident=True`（线上默认）时直连
`list_dir` 会被硬闸拒掉，真实调用一律是「ops_batch + 脚本内 call(...)」。
判定器（agentforge/clarify.py）喂的是 `_execute` 里的单次调用，所以脚本内层
调用一样可见——这条测试同时钉住「PTC 常驻下澄清闸门仍然可达」。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import interact as amb_interact            # noqa: E402
from agentforge.core.config import Config                       # noqa: E402
from agentforge.loop import (Agent, _has_clarify_intent,   # noqa: E402
                             _lookup_signal)
from agentforge.permissions import Guard                   # noqa: E402
from agentforge.tools import build_registry                # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------- 信号识别

GH_MULTI = (
    "[实测 · API] GitHub 官方搜索接口 · 查询「REA」· 排序：star 数 · 命中 6258887 个，取前 5：\n"
    "  1. morluto/rea  star=260363  fork=65487  lang=TypeScript\n"
    "     Reverse engineer anything with agents.\n"
    "  2. react/react  star=251047  fork=51510  lang=JavaScript\n"
    "  3. react/react-native  star=126683  fork=25317  lang=C++\n"
)

def test_lookup_signal() -> None:
    sig = _lookup_signal("github_search", GH_MULTI)
    check("github_search 多候选 → multi", sig is not None and sig["kind"] == "multi", str(sig))
    check("候选名取自编号行前 3 个",
          sig["candidates"] == ["morluto/rea", "react/react", "react/react-native"],
          str(sig and sig["candidates"]))
    check("查询词从「」提取", sig["query"] == "REA", str(sig and sig["query"]))

    sig = _lookup_signal("github_search",
                         "[实测 · API] GitHub 仓库搜索「rea-rs」没拿到结果：没有命中任何仓库")
    check("github_search 空结果 → empty", sig is not None and sig["kind"] == "empty", str(sig))
    check("空结果查询词=rea-rs", sig["query"] == "rea-rs", str(sig))

    sig = _lookup_signal("list_dir", "D:\\tmp\\empty\n共 0 个目录 / 0 个文件")
    check("list_dir 空目录 → empty", sig is not None and sig["kind"] == "empty", str(sig))
    check("list_dir 查询词取首行路径", "empty" in sig["query"], str(sig))

    sig = _lookup_signal("search_code", "未找到匹配 /REA/ （已扫描 12 个文件）")
    check("search_code 未找到 → empty", sig is not None and sig["kind"] == "empty", str(sig))

    sig = _lookup_signal("read_file", "[错误] 文件不存在: notes.md")
    check("read_file 文件不存在 → empty", sig is not None and sig["kind"] == "empty", str(sig))

    sig = _lookup_signal("fetch_url", "[错误] 请求失败: 404")
    check("fetch_url 请求失败 → empty", sig is not None and sig["kind"] == "empty", str(sig))

    check("写类工具的「文件不存在」不算检索信号（edit_file 不在检索清单）",
          _lookup_signal("edit_file", "[错误] 文件不存在: x.md") is None)
    check("正常命中的输出无信号",
          _lookup_signal("github_search",
                         "命中 1 个：\n  1. morluto/rea  star=260363") is None)
    check("空输出安全", _lookup_signal("list_dir", "") is None)


def test_clarify_intent() -> None:
    check("f5 现场回复：请问您是指… → True",
          _has_clarify_intent("请问您是指 morluto/rea 还是其他项目？"))
    check("r1 现场回复：项目来源三选一问句 → True",
          _has_clarify_intent(
              "1. 项目来源：该项目是本地文件、GitHub 仓库，还是一个特定的技术文档/需求描述？\n"
              "请告知我 REA 项目的相关信息。"))
    check("普通交付回复 → False", not _has_clarify_intent("已修复 bug.py，运行确认输出 24。"))
    check("只有句号没问号 → False", not _has_clarify_intent("结果如下，请查收。"))
    check("空文本安全", not _has_clarify_intent(""))


# ---------------------------------------------------------------- 端到端

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


def _ops(script: str) -> "_TC":
    """PTC 真实形态：一次 ops_batch 调用，工具写在脚本里。"""
    return _TC("ops_batch", {"script": script})


class _StubHub:
    """按剧本回话：第 n 轮返回 script[n-1]。记录每轮收到的 messages 供断言。"""

    def __init__(self, script):
        self.script = list(script)
        self.rounds: list[list[dict]] = []

    def chat_stream(self, spec, messages, tools=None, stop_check=None, **kw):
        self.rounds.append([dict(m) for m in messages])
        yield "result", self.script[len(self.rounds) - 1]


def _mk_agent(tmp: Path) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    return Agent(cfg, client=None, registry=registry, guard=guard,
                 router=None, hub=_StubHub([]))


def _events(agent: Agent, prompt: str):
    return list(agent.run([], prompt, auto_approve=True))


def test_end_to_end_popup() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent = _mk_agent(tmp)
        hub = _StubHub([
            _Res(tool_calls=[_ops(f'print(call("list_dir", path={str(tmp)!r}))')]),
            _Res(content=("当前工作区是空的，无法确定您说的 REA 是指什么。\n"
                          "请问您说的 REA 是指什么？")),         # 轮2：文本追问 → 触发弹窗
            _Res(content=("明白了，已按「git 上的项目」处理：建议用 github_search "
                          "检索 REA 并整理方案。")),             # 轮3：拿答复继续后收尾
        ])
        agent.hub = hub
        amb_interact.begin(answers=["REA实际上是git上的一个项目"])
        try:
            evs = _events(agent, "帮我在git检索REA项目")
        finally:
            amb_interact.end()

        names = [e.get("name") for e in evs if e.get("type") == "tool_start"]
        ask_events = [e for e in evs if e.get("type") == "tool_start"
                      and e.get("name") == "ask_user"]
        check("ask_user 由 harness 亲自发起（出现在工具事件里）", "ask_user" in names, str(names))
        check("追问弹窗一次 + 存记忆确认弹窗一次，共两次",
              names.count("ask_user") == 2, str(names))
        ask_ev = ask_events[0]
        check("弹窗问题取模型追问原话", "REA" in ask_ev["args"]["question"],
              str(ask_ev["args"]))
        check("空命中选项=本地项目/git上的项目/其他",
              ask_ev["args"]["options"] == ["本地项目", "git上的项目", "其他"],
              str(ask_ev["args"]["options"]))
        check("第二问=是否存入记忆（是/否）",
              ask_events[1]["args"]["question"].startswith("是否将当前信息")
              and ask_events[1]["args"]["options"] == ["是", "否"],
              str(ask_events[1]["args"]))
        ans_ev = next(e for e in evs if e.get("type") == "tool_result"
                      and e.get("name") == "ask_user")
        check("脚本答案如实回传", ans_ev["output"] == "REA实际上是git上的一个项目",
              str(ans_ev["output"]))
        # 只给了 1 条脚本答案：确认问拿到「无用户作答」提示 → 按跳过处理，不写记忆
        notes = [e for e in evs if e.get("type") == "notify"]
        check("跳过存入时也有绿色提示（notify 事件）",
              notes and "跳过存入记忆" in notes[-1]["text"], str(notes))
        check("跳过后记忆文件里没有新条目",
              "用户澄清" not in agent.memory.read(), agent.memory.read()[:120])
        final = [e for e in evs if e.get("type") == "assistant"][-1]
        check("模型拿到答复后继续并产出最终回复", "github_search" in final["content"],
              final["content"])
        hub_round3 = hub.rounds[2]
        check("答复以 user 消息回灌进历史",
              any("澄清弹窗答复" in str(m.get("content")) and
                  "REA实际上是git上的一个项目" in str(m.get("content"))
                  for m in hub_round3), "")


def test_confirm_save_yes() -> None:
    """确认弹窗选「是」→ 记忆真实落条目 + 绿色提示带 reload=memory。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent = _mk_agent(tmp)
        hub = _StubHub([
            _Res(tool_calls=[_ops(f'print(call("list_dir", path={str(tmp)!r}))')]),
            _Res(content="本地没有找到 REA，请问您说的 REA 是指什么？"),
            _Res(content="明白了，按 git 项目处理。"),
        ])
        agent.hub = hub
        amb_interact.begin(answers=["REA是git上的项目", "是"])
        try:
            evs = _events(agent, "帮我找找REA项目")
        finally:
            amb_interact.end()
        mem = agent.memory.read()
        check("选「是」→ 记忆真实写入用户澄清条目",
              "用户澄清" in mem and "REA是git上的项目" in mem, mem[:200])
        notes = [e for e in evs if e.get("type") == "notify"]
        check("存入后绿色提示（ok）且带 reload=memory",
              notes and notes[-1]["kind"] == "ok" and notes[-1].get("reload") == "memory",
              str(notes))


def test_memory_hit_no_popup() -> None:
    """本地未命中但记忆里有线索 → 不弹窗，把记忆条目喂给模型继续。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        agent = _mk_agent(tmp)
        agent.memory.append("REA 是 git 上的一个项目（用户此前澄清）")
        hub = _StubHub([
            _Res(tool_calls=[_ops(f'print(call("list_dir", path={str(tmp)!r}))')]),
            _Res(content="本地找不到 REA，请问您说的 REA 是指什么？"),  # 追问
            _Res(content="根据记忆，REA 是 git 上的项目，建议用 github_search 检索。"),
        ])
        agent.hub = hub
        amb_interact.begin(answers=[])
        try:
            evs = _events(agent, "帮我在本地找REA项目")
        finally:
            amb_interact.end()
        names = [e.get("name") for e in evs if e.get("type") == "tool_start"]
        check("记忆里有线索 → 不弹窗（ask_user 未发起）", "ask_user" not in names, str(names))
        check("记忆线索以 user 消息喂给模型",
              any("长期记忆里有这些相关记录" in str(m.get("content"))
                  and "REA 是 git 上的一个项目" in str(m.get("content"))
                  for m in hub.rounds[2]), "")
        final = [e for e in evs if e.get("type") == "assistant"][-1]
        check("模型基于记忆继续并产出最终回复", "github_search" in final["content"],
              final["content"])


def test_no_trigger_without_signal() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "a.txt").write_text("hello", encoding="utf-8")   # 工作区非空：list_dir 有命中
        agent = _mk_agent(tmp)
        hub = _StubHub([
            _Res(tool_calls=[_ops(f'print(call("list_dir", path={str(tmp)!r}))')]),
            _Res(content="已完成列出。请问还需要做什么吗？"),   # 有追问但无信号 → 不弹
        ])
        agent.hub = hub
        amb_interact.begin(answers=[])
        try:
            evs = _events(agent, "看看工作区里有什么")
        finally:
            amb_interact.end()
        names = [e.get("name") for e in evs if e.get("type") == "tool_start"]
        check("无检索信号时不弹窗（哪怕回复带问号）", "ask_user" not in names, str(names))


def test_multi_candidate_options() -> None:
    """多候选信号 + 追问 → 选项直接用命中项目名。单测走信号合成路径。"""
    from agentforge.loop import _CLARIFY_RX
    m = _CLARIFY_RX.search("请问您是指 morluto/rea 还是其他项目？")
    check("追问原话可提取", m is not None and "morluto/rea" in m.group(0), str(m))
    # 选项合成逻辑与 run() 内一致：candidates + 其他（repo 名放宽到 24 字符不切坏）
    sig = _lookup_signal("github_search", GH_MULTI)
    options = [*sig["candidates"], "其他"]
    options = [str(o)[:24] for o in options if str(o).strip()][:4]
    check("多候选选项=命中项目名+其他（repo 名不切坏）",
          options == ["morluto/rea", "react/react", "react/react-native", "其他"],
          str(options))


if __name__ == "__main__":
    test_lookup_signal()
    test_clarify_intent()
    test_end_to_end_popup()
    test_confirm_save_yes()
    test_memory_hit_no_popup()
    test_no_trigger_without_signal()
    test_multi_candidate_options()
    print("全部通过。")
