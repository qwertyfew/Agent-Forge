"""P30 回归测试：find_files / search_code 的 fmt="json" 机器可读回执。

运行：python tools/test_p30_fmt.py
全部断言内建，退出码 0 = 全过。

背景（docs/n2-baseline-20261025.md）：n2018/o1 实证——散文回执
「匹配 app.log 共 1 项 / [FILE] 路径」在 PTC 脚本里要靠字符串解析，
模型抓错 token（把「匹」当路径）→ read_file 打到 \\output\\匹。
"""
from __future__ import annotations

import json
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


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_p30_"))
    (tmp / "app.log").write_text("ERROR a\nINFO b\nERROR c\n", encoding="utf-8")
    (tmp / "sub").mkdir()
    (tmp / "sub" / "notes.md").write_text("# hello world\n", encoding="utf-8")

    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)

    # ---------- 直调（不经 agent.run） ----------
    ff = registry.get("find_files").run
    sc = registry.get("search_code").run

    # text 模式行为不变
    t = ff(pattern="app.log")
    check("P30 text 模式回执保持原格式",
          "匹配 app.log" in t and "[FILE]" in t, t[:150])

    # json 模式：机器可读、路径可直接用
    j = json.loads(ff(pattern="app.log", fmt="json"))
    check("P30 find_files json 返回结构完整",
          j.get("count") == 1 and len(j.get("files")) == 1, str(j)[:200])
    p = Path(j["files"][0])
    check("P30 json 路径可直接 read 且解析零摩擦",
          p.exists() and "ERROR a" in p.read_text(encoding="utf-8"), str(p))

    j2 = json.loads(ff(pattern="*.nope", fmt="json"))
    check("P30 find_files json 0 命中也是合法 JSON",
          j2.get("count") == 0 and j2.get("files") == [], str(j2)[:150])

    j3 = json.loads(ff(pattern="app.log", path="不存在目录", fmt="json"))
    check("P30 find_files json 错误也是合法 JSON",
          "error" in j3, str(j3)[:150])

    j4 = json.loads(sc(pattern="ERROR", fmt="json"))
    check("P30 search_code json 结构完整且行号可直接引用",
          j4.get("count") == 2 and j4["hits"][0]["line"] == 1
          and "ERROR" in j4["hits"][0]["text"], str(j4)[:200])

    # ---------- 端到端：ops_batch 脚本内 json.loads → read_file ----------
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))
    script = (
        "import json as _j\n"
        "r = _j.loads(call('find_files', pattern='app.log', fmt='json'))\n"
        "txt = call('read_file', path=r['files'][0])\n"
        "print('ERROR_LINES:', sum(1 for ln in txt.splitlines() if 'ERROR' in ln))\n"
    )
    ag.hub = _StubHub([_Res(tool_calls=[_TC("ops_batch", {"script": script})]),
                       _Res(content="完成。")])
    evs = list(ag.run([], "找日志统计错误", auto_approve=True))
    out = (next((e for e in evs if e.get("type") == "tool_result"), None)
           or {}).get("output") or ""
    check("P30 端到端：脚本内 json 路径直读成功（不再解析散文）",
          "ERROR_LINES: 2" in out, out[:300])

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
