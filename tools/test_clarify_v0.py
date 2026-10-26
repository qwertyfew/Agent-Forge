"""澄清判定（试行版 v0，docs/clarify-policy.md §12）的回归测试。

口径按 §12.5 的准入规则：**每条规则都必须同时有「该弹」和「不该弹」两类用例**，
缺一条不许进。所以本文件分四组：

  1. R1 该弹（盲选不可逆目标）
  2. R1 不该弹（读过 / 新建 / 目标不在候选里 / 非删除命令 / 同槽位只问一次）
  3. R2 该弹（零命中 + 末尾问号）
  4. R2 不该弹（没问号 / 问号在窗口外 / 零命中后又有命中 / read_file 不算零命中）
  5. 合并文本安全（绝不扫 ops_batch 的合并输出）+ 端到端（PTC 真实形态）
  6. 影子模式（只记事件、不弹、动作照做）

运行：python tools/test_clarify_v0.py  → 退出码 0 = 全过
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import interact as amb_interact            # noqa: E402
from agentforge.clarify import (Watcher, candidates_of,   # noqa: E402
                                _lookup_signal)
from agentforge.core.config import Config                       # noqa: E402
from agentforge.loop import Agent                          # noqa: E402
from agentforge.permissions import Guard                   # noqa: E402
from agentforge.tools import build_registry                # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------- 固定样例

LIST_TWO = ("D:\\ws\n共 2 个目录 / 0 个文件\n"
            "  [DIR ] rea-old/\n  [DIR ] rea-new/")
LIST_ONE = ("D:\\ws\n共 1 个目录 / 0 个文件\n"
            "  [DIR ] only/")
LIST_EMPTY = "D:\\ws\n共 0 个目录 / 0 个文件"


def _watcher(tmp: Path, mode: str = "on") -> Watcher:
    return Watcher(mode=mode, workspace_root=tmp)


# ---------------------------------------------------------------- 1. R1 该弹

def test_r1_should_ask() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "rea-old").mkdir()
        (tmp / "rea-new").mkdir()
        w = _watcher(tmp)
        w.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        d = w.precheck("delete_file", {"path": str(tmp / "rea-old")})
        check("R1 该弹：多候选 + 未读 + 紧接着删除 → ask",
              d is not None and d.rule == "R1", str(d))
        check("R1 问题带候选名（用户能据此选）",
              d and "rea-old" in d.question and "rea-new" in d.question, str(d and d.question))
        check("R1 选项=候选名 + 其他（不超过 4 个）",
              d and d.options[0] == "rea-old" and "其他" in d.options
              and len(d.options) <= 4, str(d and d.options))
        check("R1 有理由码与证据（弹窗上要看得出为什么问）",
              d and d.reason == "BLIND_IRREVERSIBLE_TARGET" and d.evidence, str(d and d.evidence))

        # 覆盖已有文件同样算不可逆
        (tmp / "rea-new" / "README.md").write_text("x", encoding="utf-8")
        w2 = _watcher(tmp)
        w2.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        d2 = w2.precheck("write_file", {"path": str(tmp / "rea-new" / "README.md"),
                                        "content": "y"})
        check("R1 该弹：写一个已存在的文件 = 覆盖 → ask", d2 is not None, str(d2))

        # 含删除语义的命令也算
        w3 = _watcher(tmp)
        w3.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        d3 = w3.precheck("run_command", {"command": f"rm -rf {tmp / 'rea-old'}"})
        check("R1 该弹：run_command 含删除语义 → ask", d3 is not None, str(d3))


# ---------------------------------------------------------------- 2. R1 不该弹

def test_r1_should_not_ask() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "rea-old").mkdir()
        (tmp / "rea-new").mkdir()

        # 读过其中一个候选 → 能自证，不是盲选
        w = _watcher(tmp)
        w.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        w.observe("read_file", {"path": str(tmp / "rea-old" / "README.md")}, "# 旧版")
        check("R1 不该弹：读过候选之一 → 放行",
              w.precheck("delete_file", {"path": str(tmp / "rea-old")}) is None)

        # 新建（目标不存在）是可逆的
        w = _watcher(tmp)
        w.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        check("R1 不该弹：write_file 目标不存在（新建）",
              w.precheck("write_file", {"path": str(tmp / "rea-old" / "new.md"),
                                        "content": "x"}) is None)

        # 目标不在候选里 → 不是从这批候选里盲选
        w = _watcher(tmp)
        w.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        check("R1 不该弹：目标是候选之外的东西",
              w.precheck("delete_file", {"path": str(tmp / "unrelated.txt")}) is None)

        # 非删除语义的命令
        w = _watcher(tmp)
        w.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        check("R1 不该弹：run_command 不是删除命令",
              w.precheck("run_command", {"command": "python -m pytest -q"}) is None)

        # 单候选 → 不构成「多候选不确定」
        w = _watcher(tmp)
        w.observe("list_dir", {"path": str(tmp)}, LIST_ONE)
        check("R1 不该弹：只有 1 个候选",
              w.precheck("delete_file", {"path": str(tmp / "only")}) is None)

        # 同一槽位只问一次（不重复打扰）
        w = _watcher(tmp)
        w.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        first = w.precheck("delete_file", {"path": str(tmp / "rea-old")})
        second = w.precheck("delete_file", {"path": str(tmp / "rea-old")})
        check("R1 不该弹第二次：同一槽位只问一次",
              first is not None and second is None)

        # 关闭模式：什么都不问
        w = _watcher(tmp, mode="off")
        w.observe("list_dir", {"path": str(tmp)}, LIST_TWO)
        check("off 模式：不判定",
              w.precheck("delete_file", {"path": str(tmp / "rea-old")}) is None)


# ---------------------------------------------------------------- 3. R2 该弹

def test_r2_should_ask() -> None:
    with tempfile.TemporaryDirectory() as td:
        w = _watcher(Path(td))
        w.observe("list_dir", {"path": "D:\\ws"}, LIST_EMPTY)
        d = w.on_reply("当前工作区是空的，无法确定您说的 REA 是指什么。\n"
                       "请问您说的 REA 是指什么？")
        check("R2 该弹：零命中 + 末尾问号 → ask",
              d is not None and d.rule == "R2", str(d))
        check("R2 问题是 harness 模板（带通道与查询词）",
              d and "工作区" in d.question, str(d and d.question))
        check("R2 保留模型原话里的实体名（用户看得懂在问什么）",
              d and "REA" in d.question, str(d and d.question))
        check("R2 理由码就位",
              d and d.reason == "ZERO_HIT_AND_MODEL_ASKED", str(d and d.reason))
        # 同一槽位再问一次：不再弹
        check("R2 不该重复问同一槽位", w.on_reply("请问 REA 是什么？") is None)


# ---------------------------------------------------------------- 4. R2 不该弹

def test_r2_should_not_ask() -> None:
    with tempfile.TemporaryDirectory() as td:
        # 没问号
        w = _watcher(Path(td))
        w.observe("list_dir", {"path": "D:\\ws"}, LIST_EMPTY)
        check("R2 不该弹：回复没有问号",
              w.on_reply("工作区是空的，我按本地项目处理。") is None)

        # 问号在末尾窗口之外（前文举例用的问号）
        w = _watcher(Path(td))
        w.observe("list_dir", {"path": "D:\\ws"}, LIST_EMPTY)
        long_tail = "。".join(["已完成分析"] * 80)
        check("R2 不该弹：问号不在末尾窗口内",
              w.on_reply("你说的「REA 是什么？」我已按本地项目处理。" + long_tail) is None)

        # 零命中之后又有命中 → 答案已在证据里
        w = _watcher(Path(td))
        w.observe("list_dir", {"path": "D:\\ws"}, LIST_EMPTY)
        w.observe("search_code", {"pattern": "REA"},
                  "src/rea.py:12:def rea():\nsrc/a.py:3:x = 1")
        check("R2 不该弹：零命中之后又有命中（答案在证据里）",
              w.on_reply("请问需要我改哪个文件？") is None)

        # read_file 的「文件不存在」不是检索零命中（R2 白名单排除）
        w = _watcher(Path(td))
        w.observe("read_file", {"path": "notes.md"}, "[错误] 文件不存在: notes.md")
        check("R2 不该弹：读一个不存在的文件不算检索零命中",
              w.on_reply("请问要从哪里开始？") is None)

        # 没有任何检索
        w = _watcher(Path(td))
        check("R2 不该弹：这一轮没有检索动作",
              w.on_reply("请问您希望先做哪一步？") is None)


# ---------------------------------------------------------------- 5. 合并文本安全

def test_merged_text_is_never_parsed() -> None:
    """PTC 下 ops_batch 的合并文本里同时混着多种工具的产出，按文本反解必然张冠李戴。

    这里钉住两件事：① 拿合并文本冒充单次调用喂进来，不会被当成检索信号；
    ② 候选提取只认单次调用的真实输出。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "rea-old").mkdir()
        (tmp / "rea-new").mkdir()
        merged = ("[ops_batch] 2 次工具调用（list_dir×1、write_file×1）\n"
                  + LIST_TWO + "\n[错误] 文件不存在: notes.md")
        w = _watcher(tmp)
        w.observe("ops_batch", {"script": "..."}, merged)
        check("合并文本不会被记成检索信号（ops_batch 不参与判定）",
              w.precheck("delete_file", {"path": str(tmp / "rea-old")}) is None
              and w.on_reply("请问您指哪个？") is None)

        check("候选提取：list_dir 取条目名",
              candidates_of("list_dir", LIST_TWO) == ["rea-old", "rea-new"],
              str(candidates_of("list_dir", LIST_TWO)))
        check("候选提取：find_files 取路径行",
              candidates_of("find_files", "匹配 **/*.py  共 2 项\na.py\nb.py")
              == ["a.py", "b.py"])
        check("候选提取：search_code 取命中文件（去重）",
              candidates_of("search_code", "src/a.py:1:x\nsrc/a.py:9:y\nb.py:2:z")
              == ["src/a.py", "b.py"])
        check("候选提取：ops_batch 一律空（不反解合并文本）",
              candidates_of("ops_batch", LIST_TWO) == [])


# ---------------------------------------------------------------- 6. 端到端

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
        i = len(self.rounds) - 1
        yield "result", (self.script[i] if i < len(self.script)
                         else _Res(content="（剧本用尽）"))


def _mk_agent(tmp: Path, mode: str = "on") -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    cfg.ptc_resident = True              # 线上默认：所有调用走 ops_batch
    cfg.clarify_mode = mode
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    return Agent(cfg, client=None, registry=registry, guard=guard,
                 router=None, hub=_StubHub([]))


def _script(tmp: Path, *lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_e2e_r1_blocks_delete() -> None:
    """PTC 真实形态：脚本里列两个候选后直接删其中一个 → 弹窗且**不执行删除**。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "rea-old").mkdir()
        (tmp / "rea-new").mkdir()
        (tmp / "rea-old" / "README.md").write_text("old", encoding="utf-8")

        agent = _mk_agent(tmp)
        agent.hub = _StubHub([
            _Res(tool_calls=[_TC("ops_batch", {"script": _script(
                tmp, f'print(call("list_dir", path={str(tmp)!r}))')})]),
            _Res(tool_calls=[_TC("ops_batch", {"script": _script(
                tmp, f'print(call("delete_file", path={str(tmp / "rea-old" / "README.md")!r}))')})]),
            _Res(content="好的，先不动它，等你确认。"),
        ])
        amb_interact.begin(answers=["rea-new 那个才对"])
        try:
            evs = list(agent.run([], "把 REA 那个旧项目清理一下", auto_approve=True))
        finally:
            amb_interact.end()

        decisions = [e for e in evs if e.get("type") == "clarify_decision"]
        check("端到端：落了一条 clarify_decision（rule=R1）",
              decisions and decisions[0]["rule"] == "R1", str(decisions))
        names = [e.get("name") for e in evs if e.get("type") == "tool_start"]
        check("端到端：harness 亲自弹了 ask_user", "ask_user" in names, str(names))
        check("端到端：文件没被删（执行前就问，不是事后道歉）",
              (tmp / "rea-old" / "README.md").exists())
        outs = [str(e.get("output", "")) for e in evs if e.get("type") == "tool_result"]
        check("端到端：模型收到「已拦下 + 本次未执行」",
              any("澄清已介入" in o and "未执行" in o for o in outs),
              str(outs)[:300])
        check("端到端：脚本答案如实回传给弹窗",
              "rea-new 那个才对" in outs, str(outs)[:300])
        check("端到端：答复以 user 消息回灌进下一轮上下文",
              any("澄清弹窗答复" in str(m.get("content"))
                  and "rea-new 那个才对" in str(m.get("content"))
                  for m in agent.hub.rounds[-1]))


def test_e2e_r1_passes_after_read() -> None:
    """同一剧本，差别只有「先 read_file」→ 不弹，删除照做。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "rea-old").mkdir()
        (tmp / "rea-new").mkdir()
        target = tmp / "rea-old" / "README.md"
        target.write_text("old", encoding="utf-8")

        agent = _mk_agent(tmp)
        agent.hub = _StubHub([
            _Res(tool_calls=[_TC("ops_batch", {"script": _script(
                tmp, f'print(call("list_dir", path={str(tmp)!r}))')})]),
            _Res(tool_calls=[_TC("ops_batch", {"script": _script(
                tmp,
                f'print(call("read_file", path={str(target)!r}))',
                f'print(call("delete_file", path={str(target)!r}))')})]),
            _Res(content="已确认并清理。"),
        ])
        amb_interact.begin(answers=[])
        try:
            evs = list(agent.run([], "删掉那个旧版本的 README", auto_approve=True))
        finally:
            amb_interact.end()
        names = [e.get("name") for e in evs if e.get("type") == "tool_start"]
        check("读过内容后再删 → 不弹窗", "ask_user" not in names, str(names))
        check("读过内容后再删 → 真的删了", not target.exists())


def test_e2e_shadow_mode() -> None:
    """影子模式：只记判定事件、不弹窗、动作照做（上线第一阶段用）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "rea-old").mkdir()
        (tmp / "rea-new").mkdir()
        target = tmp / "rea-old" / "README.md"
        target.write_text("old", encoding="utf-8")

        agent = _mk_agent(tmp, mode="shadow")
        agent.hub = _StubHub([
            _Res(tool_calls=[_TC("ops_batch", {"script": _script(
                tmp, f'print(call("list_dir", path={str(tmp)!r}))')})]),
            _Res(tool_calls=[_TC("ops_batch", {"script": _script(
                tmp, f'print(call("delete_file", path={str(target)!r}))')})]),
            _Res(content="已完成。"),
        ])
        amb_interact.begin(answers=[])
        try:
            evs = list(agent.run([], "清理旧版本", auto_approve=True))
        finally:
            amb_interact.end()
        check("影子模式：判定事件照记",
              any(e.get("type") == "clarify_decision" and e.get("rule") == "R1"
                  for e in evs))
        names = [e.get("name") for e in evs if e.get("type") == "tool_start"]
        check("影子模式：不弹窗", "ask_user" not in names, str(names))
        check("影子模式：动作照做（这就是影子期的代价，别误以为它是保护）",
              not target.exists())


if __name__ == "__main__":
    test_r1_should_ask()
    test_r1_should_not_ask()
    test_r2_should_ask()
    test_r2_should_not_ask()
    test_merged_text_is_never_parsed()
    test_e2e_r1_blocks_delete()
    test_e2e_r1_passes_after_read()
    test_e2e_shadow_mode()
    print("全部通过。")
