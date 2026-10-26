"""任务记录（文件级证据）自测：读区间、版本快照、跨 run 回填、行号区间校验。

运行：python tools/test_record.py

要证明的四件事：
1. 每次 read_file 都会把「读到的行区间」写进 evidence.jsonl，不是只记个文件名；
2. 每次成功写盘都留一个**新文件**版本，同秒连写也不会互相覆盖；
3. 新建的台账能从 evidence.jsonl 把自己读回来（换 run / 重启不丢证据）；
4. 引用行号落在读过区间之外 → 硬错误（不再只看文件读过没有）。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.core.config import Config                                  # noqa: E402
from agentforge.evidence import EvidenceLedger, audit_citations       # noqa: E402
from agentforge.loop import Agent                                     # noqa: E402
from agentforge.permissions import Guard                              # noqa: E402
from agentforge.record import Recorder, diff_stat                     # noqa: E402
from agentforge.tools import build_registry                           # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


class _TC:
    def __init__(self, name, args=None):
        self.name, self.arguments = name, args or {}


class _Res:
    def __init__(self, content="", tool_calls=None):
        self.content, self.tool_calls = content, tool_calls or []
        self.thinking = None
        self.tokens_per_second = 0.0
        self.eval_count = 0
        self.prompt_eval_count = 0


class _StubHub:
    def __init__(self, script):
        self.script, self.rounds = list(script), []

    def chat_stream(self, spec, messages, tools=None, stop_check=None, **kw):
        self.rounds.append([dict(m) for m in messages])
        yield "result", self.script[len(self.rounds) - 1]


def _mk_agent(tmp: Path, script: list[_Res], rec: Recorder | None = None) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    guard = Guard(cfg, approver=None)
    ag = Agent(cfg, client=None, registry=build_registry(guard, cfg), guard=guard,
               router=None, hub=_StubHub(script))
    ag.recorder = rec
    return ag


def _run(ag: Agent, text: str, **kw):
    ag.hub.rounds = []
    return list(ag.run([], text, auto_approve=True, **kw))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_rec_"))
    (tmp / "src").mkdir()
    body = "\n".join(f"line {i}" for i in range(1, 121))
    (tmp / "src" / "mod.py").write_text(body, encoding="utf-8")
    rec = Recorder(tmp / "history" / "t1" / "_records", rel_root=tmp)

    # ---------- A) 记录器本身 ----------
    ev = tmp / "history" / "t1" / "_records" / "evidence.jsonl"
    rec.read(tmp / "src" / "mod.py", ranges=[(1, 60)], total_lines=120)
    check("A 读取记录已落盘", ev.exists() and ev.read_text(encoding="utf-8").strip() != "")
    reads = rec.reads()
    key = "src/mod.py"
    check("A 记录里能读回文件与区间",
          key in reads and reads[key]["ranges"] == [(1, 60)] and reads[key]["lines"] == 120,
          repr(reads))

    v1, a1, r1 = rec.version(tmp / "doc.md", "a\nb\n", old="")
    v2, a2, r2 = rec.version(tmp / "doc.md", "a\nb\nc\n", old="a\nb\n")
    check("A 两次写盘 = 两个版本文件，互不覆盖", v1 != v2, f"{v1} / {v2}")
    check("A 版本文件真的存在",
          (tmp / "history" / "t1" / "_records" / v1).exists()
          and (tmp / "history" / "t1" / "_records" / v2).exists())
    check("A 第二版内容是最新的",
          (tmp / "history" / "t1" / "_records" / v2).read_text(encoding="utf-8") == "a\nb\nc\n")
    check("A 版本带行级 diff 统计", (a2, r2) == (1, 0), f"add={a2} rem={r2}")
    check("A 同内容 diff 为 0", diff_stat("x\n", "x\n") == (0, 0))

    # ---------- B) 台账从文件回填（跨 run / 重启） ----------
    led2 = EvidenceLedger(recorder=rec)
    check("B 新台账自动回填已读文件", led2.was_read(tmp / "src" / "mod.py") is True)
    check("B 行区间也回填了", led2.read_ranges(tmp / "src" / "mod.py") == [(1, 60)],
          repr(led2.read_ranges(tmp / "src" / "mod.py")))

    # ---------- C) 行号区间校验 ----------
    cfg2 = Config.load(str(ROOT / "config.yaml"))
    cfg2.workspace_root = tmp          # 引用解析必须以这个临时工作区为根
    guard = Guard(cfg2, approver=None)
    ok_doc = "见 `src/mod.py:30` 的实现。另见 `src/mod.py:12-18`。"
    bad_doc = "见 `src/mod.py:300` 的实现。另见 `src/mod.py:12-18`。"
    a_ok = audit_citations(ok_doc, guard, tmp, led2)
    a_bad = audit_citations(bad_doc, guard, tmp, led2)
    check("C 读过的行 → 不报错", not [i for i in a_ok.issues if i.level == "error"],
          repr([i.detail for i in a_ok.issues]))
    errs = [i for i in a_bad.issues if i.kind == "line_not_read"]
    check("C 没读过的行 → line_not_read 硬错误", bool(errs),
          repr([(i.kind, i.detail) for i in a_bad.issues]))
    check("C 统计口径带 refs / with_line / outside",
          a_bad.stats and a_bad.stats["with_line"] == 2 and a_bad.stats["outside"] == 1,
          repr(a_bad.stats))

    # 没读过正文（只有文件存在）时不做区间判定，避免误伤
    led_empty = EvidenceLedger()
    a_noread = audit_citations(bad_doc, guard, tmp, led_empty)
    check("C 台账里没有记录 → 不按区间误判",
          not [i for i in a_noread.issues if i.kind == "line_not_read"])

    # ---------- D) 端到端：读 → 写，版本与记录都留痕 ----------
    rec2 = Recorder(tmp / "history" / "t2" / "_records", rel_root=tmp)
    ag = _mk_agent(tmp, [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "print(call('read_file', path='src/mod.py')[:200])"})]),
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "call('write_file', path='out/report.md', content='# 报告\\n见 `src/mod.py:30`\\n')"})]),
        _Res(content="完成。"),
    ], rec=rec2)
    (tmp / "out").mkdir()
    _run(ag, "任务：写一份报告，交付物 out/report.md")
    ev2 = tmp / "history" / "t2" / "_records" / "evidence.jsonl"
    txt2 = ev2.read_text(encoding="utf-8") if ev2.exists() else ""
    check("D 读操作进了记录（含区间）", '"kind": "read"' in txt2 and '"ranges": [[1,' in txt2.replace(", ", ", "),
          txt2[:300])
    check("D 写操作进了记录（带版本路径）", '"kind": "write"' in txt2 and "versions/" in txt2, txt2[:300])
    vdir = tmp / "history" / "t2" / "_records" / "versions"
    check("D 版本目录里有该交付物的全文", any(vdir.rglob("*.md")), str(list(vdir.rglob('*'))[:5]))
    check("D 引用核查结论也落了文件",
          (tmp / "history" / "t2" / "_records" / "citations.jsonl").exists())

    print("\n全部断言通过。")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"耗时 {time.time() - t0:.1f}s")
