"""ops_batch（PTC 程序化工具调用）的自测：管线纪律 + 端到端。

运行：python tools/test_ops_batch.py
全部断言内建，退出码 0 = 全过。纯程序测试：模型用 stub hub 按剧本回话。

核心纪律（docs/changes/token-economy-20261019.md T1）：
- SDK 调用走 Agent._execute 同一条管道：审批/证据台账/file_log 继承，
  脚本里的 read_file 一样记入 read 硬闸；
- 只读工具自动放行；写类工具要求运行本身是完全访问权限；
- 结果预算：超长掐中段留头尾；异常回给模型而不是崩溃；超时兜底。
"""
from __future__ import annotations

import sys
import tempfile
import time
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


def _mk_agent(tmp: Path, script: list[_Res], full: bool = True) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))
    ag.hub = _StubHub(script)
    return ag


def _run(ag: Agent, text: str, **kw):
    """每次 run 前清零 stub 的轮次计数——hub 按累计轮数索引剧本，跨 run 不清会错位。"""
    ag.hub.rounds = []
    return list(ag.run([], text, auto_approve=True, **kw))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_ops_"))
    for i in range(1, 4):
        d = tmp / "src"
        d.mkdir(exist_ok=True)
        (d / f"mod{i}.py").write_text(f"# module {i}\ndef f{i}():\n    return {i}\n",
                                      encoding="utf-8")

    # ---------- 直调工具（不经 agent.run）：管线纪律 ----------
    ag0 = _mk_agent(tmp, [])
    script = (
        "lines = []\n"
        "for i in range(1, 4):\n"
        "    t = tools.call('read_file', path=f'src/mod{i}.py')\n"
        "    lines.append(t.splitlines()[0])\n"
        "print('HEADS: ' + ' | '.join(lines))\n"
    )
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script": script})]),
        _Res(content="三个模块头部已汇总。"),
    ]
    evs = _run(ag0, "用 ops_batch 批量读取 src/")
    out = next((e for e in evs if e.get("type") == "tool_result"), None)
    check("脚本执行有结果回传",
          out is not None and "HEADS: " in (out.get("output") or "")
          and "mod1.py" in (out.get("output") or "") and "mod3.py" in (out.get("output") or ""),
          repr((out or {}).get("output", ""))[:160])
    check("结果带调用统计", "3 次工具调用" in (out.get("output") or ""),
          repr((out or {}).get("output", ""))[:160])
    check("SDK read 记入证据台账（read 硬闸继承）",
          ag0.guard.ledger.was_read(tmp / "src" / "mod2.py"))
    finals = [e for e in evs if e.get("type") == "assistant"]
    check("PTC 轮次后正常收尾", finals and finals[-1]["content"] == "三个模块头部已汇总。")

    # 预算掐中段
    long_script = "print('x' * 9000)\n"
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script": long_script, "budget": 800})]),
        _Res(content="收到。"),
    ]
    evs2 = _run(ag0, "长输出预算测试")
    out2 = next((e for e in evs2 if e.get("type") == "tool_result"), None)
    o2 = (out2 or {}).get("output") or ""
    check("超预算掐中段留头尾", len(o2) < 1400 and "中段省略" in o2 and "…\n" in (o2.replace('x', '') or o2),
          f"len={len(o2)}")

    # 脚本异常不崩溃、异常回传
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script": "x = 1 / 0"})]),
        _Res(content="脚本报错了，我已知晓。"),
    ]
    evs3 = _run(ag0, "异常测试")
    out3 = next((e for e in evs3 if e.get("type") == "tool_result"), None)
    check("脚本异常回传为 [脚本异常]", "[脚本异常] ZeroDivisionError" in ((out3 or {}).get("output") or ""),
          repr((out3 or {}).get("output", ""))[:120])

    # 禁止嵌套 / 未知工具
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "a = tools.call('ops_batch', script='pass')\n"
             "b = tools.call('no_such_tool')\n"
             "print(a.splitlines()[0]); print(b.splitlines()[0])"})]),
        _Res(content="ok"),
    ]
    evs4 = _run(ag0, "禁调测试")
    out4 = (next((e for e in evs4 if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("脚本内 ops_batch 被拒", "[已拒绝] 脚本内不能调用 ops_batch" in out4, repr(out4)[:120])
    check("未知工具报可用清单", "[错误] 未知工具 no_such_tool" in out4, repr(out4)[:160])

    # 命名冲突回归（rerun18 真实 bug）：脚本里写 `import tools` 会把名字 tools
    # 重新绑到真正的 agentforge.tools 模块，导致 tools.call 失效。修复后主 API
    # 是裸函数 call(...)，即便 import tools 覆盖别名，call(...) 仍可用。
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "import tools\n"                       # 故意模拟模型会做的 import
             "t = call('read_file', path='src/mod1.py')\n"   # 用裸 call，不受 import 影响
             "print('OK: ' + t[:80])"})]),          # 验证读到文件即证明 call 可用
        _Res(content="import tools 不再破坏调用。"),
    ]
    evs4b = _run(ag0, "import tools 冲突回归")
    out4b = (next((e for e in evs4b if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("import tools 后仍能用 call（冲突修复）",
          "OK: " in out4b and "mod1.py" in out4b and "[脚本异常]" not in out4b,
          repr(out4b)[:160])
    check("冲突场景下不再报 'has no attribute 'call''",
          "has no attribute 'call'" not in out4b, repr(out4b)[:160])

    # tools 壳 __getattr__（rerun22 真实 bug）：模型 6 次写 `from tools import list_dir`
    # / `from tools import write_file`，每次 ImportError（命中的是仓库根没有 __init__
    # 的 tools/ 测试包），白烧 6 轮。壳补上模块级 __getattr__ 后，import 形态与
    # call(...) 等价，不再有「模块里没这个属性」这条死路。
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "import tools as T\n"
             "a = T.read_file(path='src/mod1.py')\n"
             "print('A_OK' if 'mod1' in a else 'A_BAD')\n"
             "from tools import read_file\n"
             "b = read_file(path='src/mod2.py')\n"
             "print('B_OK' if 'mod2' in b else 'B_BAD')\n"
             "print('C_OK' if 'mod3' in T.call('read_file', path='src/mod3.py') else 'C_BAD')"})]),
        _Res(content="import 形态可用。"),
    ]
    evs4c = _run(ag0, "tools 壳 __getattr__")
    out4c = (next((e for e in evs4c if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("import tools + 属性调用可用", "A_OK" in out4c, repr(out4c)[:200])
    check("from tools import X 不再 ImportError",
          "B_OK" in out4c and "ImportError" not in out4c and "[脚本异常]" not in out4c,
          repr(out4c)[:200])
    check("tools.call 别名仍可用", "C_OK" in out4c, repr(out4c)[:200])

    # 未知属性不抛 AttributeError，而是回明确的「未知工具」——模型能据此改名重试
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "r = tools.no_such_tool_xyz()\nprint(r.splitlines()[0])"})]),
        _Res(content="ok"),
    ]
    evs4d = _run(ag0, "未知属性回退")
    out4d = (next((e for e in evs4d if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("未知属性回落为未知工具提示",
          "未知工具 no_such_tool_xyz" in out4d, repr(out4d)[:200])

    # 主 API 裸 call(...) 直调（覆盖文档主口径）
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "h = call('read_file', path='src/mod2.py')\n"
             "print('HEAD2: ' + h[:80])"})]),   # 首行是路径头
        _Res(content="收到。"),
    ]
    evs4c = _run(ag0, "裸 call 主 API 测试")
    out4c = (next((e for e in evs4c if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("裸 call(...) 主 API 可用", "HEAD2:" in out4c and "mod2.py" in out4c, repr(out4c)[:160])

    # 写类工具分级：完全访问 → 放行 + file_log 记账
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "tools.call('write_file', path='out/ptc.txt', content='PTC 写入')\n"
             "print('written')"})]),
        _Res(content="写好了。"),
    ]
    fl: list = []
    ag0.hub.script = ag0.hub.script
    evs5 = _run(ag0, "脚本内写文件测试", file_log=fl)
    out5 = (next((e for e in evs5 if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("完全访问下脚本写文件放行", "written" in out5, repr(out5)[:120])
    check("脚本写文件记入 file_log", any(getattr(r, "get", lambda k: None)("path") == "out/ptc.txt"
                                        or "ptc.txt" in str(r) for r in fl), str(fl)[:160])
    check("写出的文件真实落盘", (tmp / "out" / "ptc.txt").exists() and
          (tmp / "out" / "ptc.txt").read_text(encoding="utf-8") == "PTC 写入")

    # 超时兜底
    ag0.hub.script = [
        _Res(tool_calls=[_TC("ops_batch", {"script": "import time\nwhile True: time.sleep(0.1)",
                                            "timeout": 5})]),
        _Res(content="超时了，我知晓。"),
    ]
    t0 = time.monotonic()
    evs6 = _run(ag0, "超时测试")
    took = time.monotonic() - t0
    out6 = (next((e for e in evs6 if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("死循环脚本超时被拦", "超时" in out6 and took < 30, f"took={took:.0f}s out={out6[:80]}")

    # ---------- 端到端：模型先 ops_batch 再作答，事件流完整 ----------
    ag7 = _mk_agent(tmp, [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "n = 0\nfor i in range(1, 4):\n    if 'def f' in tools.call('read_file', path=f'src/mod{i}.py'):\n        n += 1\nprint(f'{n}/3 个模块含函数定义')"})]),
        _Res(content="统计完成：3/3。"),
    ])
    evs7 = _run(ag7, "统计模块")
    kinds = [e.get("type") for e in evs7]
    check("事件流含 tool_start/tool_result/assistant",
          "tool_start" in kinds and "tool_result" in kinds and "assistant" in kinds, str(kinds[:12]))
    tr = next(e for e in evs7 if e.get("type") == "tool_result")
    check("端到端脚本结果正确", "3/3 个模块含函数定义" in (tr.get("output") or ""),
          repr((tr.get("output") or ""))[:120])

    # 回归：写类工具结果即便脚本没 print 也必须回显——否则模型看不到写失败/引用核查
    # 拒绝，会以为没写而用相同脚本重试，触发去重守卫死锁（rerun19 实测 bug）。
    tmp_echo = Path(tempfile.mkdtemp())
    ag_echo = _mk_agent(tmp_echo, [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "call('write_file', path='echo_out.md', content='hello world')"  # 故意不 print
        })]),
        _Res(content="收到。"),
    ])
    evs_echo = _run(ag_echo, "写结果回显回归")
    out_echo = (next((e for e in evs_echo if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("写类工具结果未 print 也回显（↳ 前缀）", "↳ write_file:" in out_echo, repr(out_echo)[:200])
    check("回显含写入成功确认与路径", "文件" in out_echo and "echo_out.md" in out_echo, repr(out_echo)[:200])
    check("文件确实已落盘", (tmp_echo / "echo_out.md").exists(), "echo_out.md 不存在")

    # 参数名回传：rerun22 里模型用 query= 调 search_code，只收到「unexpected keyword
    # argument」，又猜了两轮。错误里带上该工具接受的参数名，一轮就能改对。
    msg = ag0._execute("read_file", {"query": "x"}, True, None)
    check("参数错误回传可接受参数名",
          "[参数]" in msg and "path" in msg and "query" not in msg.split("[参数]")[-1],
          msg[:200])

    # 多位置参数：rerun27 实证模型写 call('write_file', 'a.md', '正文')，旧壳
    # 「最多 1 个位置参数」直接抛 TypeError，白烧一整轮。必填参数按声明顺序对齐。
    tmp_pos = Path(tempfile.mkdtemp())
    ag_pos = _mk_agent(tmp_pos, [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "call('write_file', 'pos_out.md', 'from positional')\n"
             "print('done')"})]),
        _Res(content="收到。"),
    ])
    evs_pos = _run(ag_pos, "位置参数回归")
    out_pos = (next((e for e in evs_pos if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("write_file 接受两个位置参数（path, content）",
          (tmp_pos / "pos_out.md").exists()
          and (tmp_pos / "pos_out.md").read_text(encoding="utf-8") == "from positional",
          (out_pos or "")[:300])
    check("多位置参数不再抛 TypeError", "TypeError" not in out_pos, (out_pos or "")[:200])

    # 多余的位置参数仍要拦住（给错数量的调用不能被静默丢弃）
    tmp_over = Path(tempfile.mkdtemp())
    ag_over = _mk_agent(tmp_over, [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "call('read_file', 'a', 'b', 'c')"})]),
        _Res(content="收到。"),
    ])
    evs_over = _run(ag_over, "位置参数过多")
    out_over = (next((e for e in evs_over if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("超出必填个数的位置参数仍有明确报错",
          "位置参数" in out_over, out_over[:300])

    # ---------- 脚本污染：模型把 <|channel>thought 混进 script（rerun56 实况） ----------
    # 现场：脚本文本是 `<|channel>thought ... <channel|>call("web_search", ...)`，
    # 编译直接 SyntaxError: unterminated string literal → 0 次工具调用，白烧一轮。
    tmp_pol = Path(tempfile.mkdtemp(prefix="af_ops_pol_"))
    (tmp_pol / "note.txt").write_text("POLLUTED-OK", encoding="utf-8")
    ag_pol = _mk_agent(tmp_pol, [
        _Res(tool_calls=[_TC("ops_batch", {"script":
             "<|channel>thought\n"
             "I will read the note file first.\n"
             "Then I will report it back.\n"
             "<channel|>print(call('read_file', path='note.txt'))\n"})]),
        _Res(content="已读取。"),
    ])
    evs_pol = _run(ag_pol, "读取 note.txt")
    out_pol = (next((e for e in evs_pol if e.get("type") == "tool_result"), None) or {}).get("output") or ""
    check("控制标记污染仍能执行（不再 SyntaxError）",
          "POLLUTED-OK" in out_pol and "SyntaxError" not in out_pol, out_pol[:300])
    check("污染脚本不是 0 次工具调用", "0 次工具调用" not in out_pol, out_pol[:200])
    check("告知模型已做脚本预处理", "[脚本预处理]" in out_pol, out_pol[:200])

    print("\n结果：全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
