"""工具预检（agentforge.preflight）回归。

预检的纪律是「宁漏勿误」：信号保守、只有真缺才吭声。这里逐条钉住：
信号命中判定、能力在时不吭声、缺了才出通告、通告内容含硬指令、缺口入台账。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import gaps, preflight


class StubCfg:
    def __init__(self, root: Path):
        self._root = root

    def state(self) -> Path:
        return self._root


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "ok" if cond else "FAIL"
    print(f"  [{_n[0] + 1}] {mark}  {name}" + (f"  {detail}" if detail and not cond else ""))
    _n[0] += 1
    if not cond:
        _fails.append(name)


_n = [0]
_fails: list = []

FULL = ["read_file", "write_file", "list_dir", "find_files", "run_command",
        "web_search", "fetch_url", "github_search",
        "git_status", "git_clone", "git_commit", "use_skill"]

with tempfile.TemporaryDirectory() as td:
    cfg = StubCfg(Path(td))

    # P1 能力齐全时，任何意图都不吭声
    check("P1 能力齐全 → 不通告",
          preflight.note_for("帮我查一下 GitHub 上 star 最多的 Rust 项目", FULL, cfg) == "")
    check("P2 联网意图、工具在 → 不通告",
          preflight.assess("联网搜一下最新的 ollama 版本", FULL) == [])
    check("P3 命令意图、工具在 → 不通告",
          preflight.assess("运行 pip install requests", FULL) == [])

    # P4 无关文本不触发任何信号（宁漏勿误）
    check("P4 无关文本 → 空",
          preflight.assess("把 notes.txt 里的待办按优先级排序", FULL) == [])

    # P5 github 意图 + github_search 缺 → 记缺口
    missing = preflight.assess("查一下 GitHub 上 whisper 的仓库", 
                               [n for n in FULL if n != "github_search"])
    check("P5 缺 github_search → 判 github 能力缺失",
          [m["key"] for m in missing] == ["github"], str(missing))

    # P6 缺失时通告内容：含能力名 + 硬指令（不许编造）
    note = preflight.note_for("查一下 GitHub 上 whisper 的仓库",
                              [n for n in FULL if n != "github_search"], cfg)
    check("P6 通告含硬指令", "绝不允许" in note and "如实" in note, note[:120])
    check("P7 通告列明缺什么", "GitHub 仓库检索" in note, note[:200])

    # P8 web 双工具任一在即可（fetch_url 还在 → 不缺）
    check("P8 web 只缺一个工具不算缺",
          preflight.assess("联网搜一下最新消息",
                           [n for n in FULL if n != "web_search"]) == [])

    # P9 双工具全缺才判缺
    missing = preflight.assess("联网搜一下最新消息",
                               [n for n in FULL if n not in ("web_search", "fetch_url")])
    check("P9 web 全缺 → 判缺失", [m["key"] for m in missing] == ["web"], str(missing))

    # P10 通告生成时缺口入台账
    snap = gaps.snapshot(cfg)
    check("P10 预检缺口入台账",
          any(i["kind"] == "capability_missing" and i["key"] == "github"
              for i in snap["items"]),
          str(snap))

    # P11 空文本安全
    check("P11 空文本安全", preflight.assess("", FULL) == []
          and preflight.note_for("", [], cfg) == "")

    # P12 多能力同时缺 → 都列出来
    bare = ["read_file", "write_file"]
    missing = preflight.assess("克隆 github 上的仓库然后运行里面的测试脚本", bare)
    keys = {m["key"] for m in missing}
    check("P12 多能力同时缺", {"github", "shell"} <= keys, str(missing))

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
