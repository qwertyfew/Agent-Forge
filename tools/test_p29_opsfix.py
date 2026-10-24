"""P29 回归测试：ops_batch 权威回执说实话 + numeric 判定剔噪。

运行：python tools/test_p29_opsfix.py
全部断言内建，退出码 0 = 全过。

背景（docs/n3-attribution-20261025.md）：
- M1 sanitize_script 抢救产物丢失写类调用 → 宁可 SyntaxError 也不静默截脚本；
- M2 失败的 run_command 被报成「已写入磁盘 ✓」→ 命令类单独报 exit 状态；
- M3 numeric 提取器把路径时间戳扫成候选数字 → 提取前剔噪。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                        # noqa: E402
from agentforge.evals import _numbers                       # noqa: E402
from agentforge.loop import Agent                           # noqa: E402
from agentforge.permissions import Guard                    # noqa: E402
from agentforge.tools import build_registry                 # noqa: E402
from agentforge.tools.ops import sanitize_script, _EXIT_RX  # noqa: E402


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


def _run(ag: Agent, text: str):
    ag.hub.rounds = []
    return list(ag.run([], text, auto_approve=True))


def main() -> None:
    # ---------- M1：抢救不许丢调用 ----------
    # n1064/c5-fib 原样复刻：content 字符串里裸换行 → 整段语法非法。
    bad = ('call("write_file", path="fib.py", content="def fib():\n'
           '    a, b = 0, 1\n'
           '    print(a)\n'
           '")\n'
           'print(call("run_command", command="python fib.py"))')
    s, note = sanitize_script(bad)
    check("M1 裸换行脚本不被静默截改（原样返回）", s == bad and note == "",
          f"note={note!r} 长度变化 {len(bad)}→{len(s)}")

    # rerun56 型（思考文本混入）：抢救仍工作，且调用保全。
    mixed = ('<|channel|>thought 先写文件\n<channel|>\n'
             'call("write_file", path="a.txt", content="x")')
    s2, note2 = sanitize_script(mixed)
    check("M1 思考混入脚本仍抢救且保全调用",
          'call("write_file"' in s2 and "write_file" in note2 + s2,
          f"s2={s2!r}")

    # 语法非法且无调用可保（纯垃圾）→ 原样返回，交给 SyntaxError 回执。
    junk = "def f(:\n    pass\n"
    s3, note3 = sanitize_script(junk)
    check("M1 纯垃圾脚本原样返回", s3 == junk and note3 == "")

    # ---------- M3：numeric 提取剔噪 ----------
    found = _numbers("路径：history/20261024-164158/output/x.py，共 4 个邮箱。")
    check("M3 路径时间戳不再进 found",
          20261024.0 not in found and -164158.0 not in found and 4.0 in found,
          f"found={found}")
    check("M3 普通文本提取不受影响", _numbers("共 3 处不同。") == [3.0])

    # ---------- M2：端到端回执 ----------
    tmp = Path(tempfile.mkdtemp(prefix="af_p29_"))
    script = ("call('write_file', path='ok.txt', content='hi')\n"
              "print(call('run_command', "
              "command='python -c \"import sys; sys.exit(3)\"'))\n")
    ag = _mk_agent(tmp, [_Res(tool_calls=[_TC("ops_batch", {"script": script})]),
                         _Res(content="完成。")])
    evs = _run(ag, "写个文件再跑个会失败的命令")
    out = (next((e for e in evs if e.get("type") == "tool_result"), None)
           or {}).get("output") or ""
    check("M2 写盘块仍正常报告落盘成功",
          "ok.txt" in out and "已写入磁盘" in out
          and "以上文件已确认存在于磁盘" in out, out[:400])
    check("M2 命令块单独报告失败 exit 码",
          "【系统核对·命令执行】" in out and "执行失败（exit=3）" in out,
          out[-400:])
    check("M2 失败命令不再冒充「已写入磁盘」",
          "run_command → " not in out
          and out.count("已写入磁盘") == 1, out[:600])

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
