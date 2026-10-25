"""P31 回归测试：脚本内 read_file 自动降级 raw——展示格式不再被当内容。

运行：python tools/test_p31_raw.py
全部断言内建，退出码 0 = 全过。

背景（docs/n3-baseline-20261025.md 归因 A）：
- f2 数出 8 行 = 1 条头行 + 7 行内容（回执整段被 splitlines）；
- a1 把回执喂 csv.DictReader → 行号前缀污染解析 → 总和 0.0；
- c7 把展示文本（含头行）排序写盘。
统一根因：read_file 展示格式的装饰与内容无机器可读边界。
修法：ops_batch 脚本内自动注入 fmt="raw"（上下文分流，非提示词）。
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


CSV_TEXT = "date,amount\n2026-10-01,50.5\n2026-10-02,80\n2026-10-03,100\n"
NOTES_TEXT = "买牛奶\n修自行车\n交房租\n预约牙医\n给妈妈打电话\n还书\n买灯泡\n"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_p31_"))
    (tmp / "sales.csv").write_text(CSV_TEXT, encoding="utf-8")
    (tmp / "notes.txt").write_text(NOTES_TEXT, encoding="utf-8")

    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)

    # ---------- 直调：默认仍是展示格式（顶层引用核查依赖它） ----------
    rf = registry.get("read_file").run
    disp = rf(path="sales.csv")
    check("P31 顶层默认仍是展示格式（头行+行号）",
          "行, 显示" in disp and " 1| " in disp, disp[:200])

    raw = rf(path="sales.csv", fmt="raw")
    check("P31 fmt=raw 无头行无行号、与磁盘字节一致",
          raw == CSV_TEXT, repr(raw)[:200])

    # offset/limit 在 raw 下仍生效
    raw2 = rf(path="notes.txt", fmt="raw", offset=2, limit=2)
    check("P31 raw 支持 offset/limit 分页", raw2 == "修自行车\n交房租", repr(raw2))

    # ---------- 端到端：ops_batch 脚本内自动 raw ----------
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))

    # a1 复现：csv.DictReader 直接吃回执——raw 下应解析出正确总和
    script_a1 = (
        "import csv, io\n"
        "content = call('read_file', path='sales.csv')\n"
        "reader = csv.DictReader(io.StringIO(content))\n"
        "total = sum(float(r['amount']) for r in reader)\n"
        "print('TOTAL:', total)\n"
    )
    ag.hub = _StubHub([_Res(tool_calls=[_TC("ops_batch", {"script": script_a1})]),
                       _Res(content="完成。")])
    evs = list(ag.run([], "算总和", auto_approve=True))
    out = (next((e for e in evs if e.get("type") == "tool_result"), None)
           or {}).get("output") or ""
    check("P31 端到端 a1 复现：csv 解析出 230.5（不再被行号污染）",
          "TOTAL: 230.5" in out, out[:300])

    # f2 复现：脚本内数行 = 真实行数 7（不是 8）
    script_f2 = (
        "content = call('read_file', path='notes.txt')\n"
        "print('LINES:', len(content.splitlines()))\n"
    )
    ag.hub = _StubHub([_Res(tool_calls=[_TC("ops_batch", {"script": script_f2})]),
                       _Res(content="完成。")])
    evs = list(ag.run([], "数行数", auto_approve=True))
    out = (next((e for e in evs if e.get("type") == "tool_result"), None)
           or {}).get("output") or ""
    check("P31 端到端 f2 复现：数出真实 7 行（不是 8）",
          "LINES: 7" in out and "LINES: 8" not in out, out[:300])

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
