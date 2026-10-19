"""PTC 常驻（程序化工具调用）的自测：脚本面限制 + 工具名归一化。

运行：python tools/test_ptc_resident.py
全部断言内建，退出码 0 = 全过。纯程序测试：模型用 stub hub 按剧本回话，不需要模型在线。

覆盖三件事：
1. 常驻模式下，模型直接拿到的工具 schema 只有「脚本面」（ops_batch / ask_user / find_tools），
   其余工具只在索引里列名字（供脚本内 tools.call 用）。
2. _execute 工具名归一化：git.clone（点号）→ git_clone（下划线）自动解析，不再误报 tool_missing；
   乱码名（this??...?）判定为格式非法，不污染能力缺口台账。
3. 顶层硬闸：常驻模式下模型若直接发非脚本面工具调用，被拦截并提示改走 ops_batch；
   关闭常驻则放行（走原路径）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                        # noqa: E402
from agentforge.gaps import snapshot as gaps_snapshot        # noqa: E402
from agentforge.loop import Agent, _is_malformed_tool_name  # noqa: E402
from agentforge.permissions import Guard                    # noqa: E402
from agentforge.toolselect import PTC_SURFACE                # noqa: E402
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


def _mk_agent(tmp: Path, script, ptc: bool) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.state_dir = tmp                      # 别污染真实 .agentforge/gaps.json
    cfg.compact_auto = False
    cfg.ptc_resident = ptc
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))
    ag.hub = _StubHub(script)
    return ag


def test_selector_surface() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_ptc_"))
    ag = _mk_agent(tmp, [], ptc=True)
    names = [(s.get("function") or {}).get("name") for s in ag.registry.selector.select([])]
    check("常驻模式只下发脚本面工具", set(names) <= set(PTC_SURFACE), str(names))
    check("脚本面含 ops_batch / ask_user",
          "ops_batch" in names and "ask_user" in names, str(names))
    idx = ag.registry.selector.index_text()
    check("索引仍列出可编入脚本的工具（如 read_file）", "read_file" in idx, idx[:120])


def test_name_normalization() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_ptc_"))
    ag = _mk_agent(tmp, [], ptc=True)
    # git.clone（点号变体）→ 应解析到真实存在的 git_clone，不报 tool_missing
    r1 = ag._execute("git.clone", {})
    snap = gaps_snapshot(ag.cfg)
    keys = {it["kind"] + "|" + it["key"] for it in snap["items"]}
    check("git.clone 自动归一化为 git_clone（不误报缺失）",
          not r1.startswith("[错误] 未知工具"), r1[:80])
    check("git.clone 未污染能力缺口台账", "tool_missing|git.clone" not in keys,
          str(sorted(keys))[:160])
    # 乱码名：格式非法，不记台账
    r2 = ag._execute("this??...?", {})
    snap2 = gaps_snapshot(ag.cfg)
    keys2 = {it["kind"] + "|" + it["key"] for it in snap2["items"]}
    check("乱码工具名被判格式非法", "格式非法" in r2, r2[:80])
    check("乱码名未污染能力缺口台账", "tool_missing|this??...?" not in keys2,
          str(sorted(keys2))[:160])
    # 真实不存在的工具：应当记 tool_missing
    r3 = ag._execute("definitely_not_real_xyz", {})
    snap3 = gaps_snapshot(ag.cfg)
    keys3 = {it["kind"] + "|" + it["key"] for it in snap3["items"]}
    check("真实缺失工具仍记 tool_missing",
          r3.startswith("[错误] 未知工具") and "tool_missing|definitely_not_real_xyz" in keys3,
          r3[:80] + " / " + str(sorted(keys3))[:160])


def test_malformed_helper() -> None:
    check("合法名不误判", not _is_malformed_tool_name("read_file"))
    check("大写名不误判（会走归一化/选择器）", not _is_malformed_tool_name("ReadFile"))
    check("点号名不误判（会走归一化）", not _is_malformed_tool_name("git.clone"))
    check("乱码名判非法", _is_malformed_tool_name("this??...?"))
    check("空白名判非法", _is_malformed_tool_name("read file"))
    check("超长名判非法", _is_malformed_tool_name("x" * 65))


def test_top_level_guard() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="af_ptc_"))
    # 常驻开启：直接发 read_file 应被拦截
    ag1 = _mk_agent(tmp, [
        _Res(tool_calls=[_TC("read_file", {"path": "nope.txt"})]),
        _Res(content="收到拦截提示。"),
    ], ptc=True)
    evs = list(ag1.run([], "测试直接调用", auto_approve=True))
    tr = next((e for e in evs if e.get("type") == "tool_result" and e.get("name") == "read_file"), None)
    check("常驻模式拦截直接非脚本面调用", tr is not None and "[PTC 常驻]" in (tr.get("output") or ""),
          str(tr)[:160])

    # 常驻关闭：同样的调用不拦截（走原路径，此处 read_file 因文件不存在报错而非被拦）
    ag2 = _mk_agent(tmp, [
        _Res(tool_calls=[_TC("read_file", {"path": "nope.txt"})]),
        _Res(content="继续。"),
    ], ptc=False)
    evs2 = list(ag2.run([], "测试直接调用", auto_approve=True))
    tr2 = next((e for e in evs2 if e.get("type") == "tool_result" and e.get("name") == "read_file"), None)
    check("关闭常驻不拦截直接调用", tr2 is not None and "[PTC 常驻]" not in (tr2.get("output") or ""),
          str(tr2)[:160])


def test_ops_batch_not_deduped() -> None:
    """回归：ops_batch 是脚本执行器（副作用取决于脚本内容），绝不能被去重守卫按
    只读缓存——否则模型重试写脚本会被「重复调用」拦截而死锁，且首写失败也没机会重来。"""
    tmp = Path(tempfile.mkdtemp(prefix="af_ptc_"))
    ag = _mk_agent(tmp, [
        _Res(tool_calls=[_TC("ops_batch", {"script": "print('RUN_A')"})]),
        _Res(tool_calls=[_TC("ops_batch", {"script": "print('RUN_A')"})]),  # 完全相同
        _Res(content="完成。"),
    ], ptc=True)
    evs = list(ag.run([], "ops_batch 去重回归", auto_approve=True))
    outs = [e.get("output") for e in evs
            if e.get("type") == "tool_result" and e.get("name") == "ops_batch"]
    check("两次相同 ops_batch 都被执行（未被去重替换）", len(outs) == 2, f"got {len(outs)} 次")
    check("第二次不是重复调用拦截", "重复调用" not in outs[1], str(outs))
    check("两次都真实运行了脚本", "RUN_A" in outs[0] and "RUN_A" in outs[1], str(outs))


def test_ops_batch_contract_gate() -> None:
    """A 方案回归：契约闸门下沉到 PTC 脚本边界。

    PTC 常驻下模型用 ops_batch（带 tool_calls）反复写-失败死循环，永不到
    「无 tool_calls 收工」，使 800 行的收工拦截整轮碰不到（rerun20：22 轮
    死循环、0 次闸门）。这里验证：脚本内调了 write_file 但交付物未落盘
    （引用核查拒绝），loop 应在 ops_batch 执行后立即触发 contract_gate 回灌，
    而非等收工——且同一份交付物只触发一次，round2 修正引用后写入则不再触发。
    """
    tmp = Path(tempfile.mkdtemp(prefix="af_ptc_cg_"))
    deliverable = "deliverable.md"
    # 上轮遗留的交付物（mtime 早于本轮 _t0），模拟 stale 场景
    (tmp / deliverable).write_text("# old\n", encoding="utf-8")

    script1 = (
        "call('write_file', path='" + deliverable + "', content='# 报告\\n"
        "- 见 `external/learn-claude-code/s01_agent_loop/README.md:28`\\n"
        "- 另见 `nonexistent/path_xyz.md:99`')\n"
    )
    script2 = (
        "call('write_file', path='" + deliverable + "', content='# 报告\\n"
        "这是修正后的报告，不含任何反引号引用。')\n"
    )
    ag = _mk_agent(tmp, [
        _Res(tool_calls=[_TC("ops_batch", {"script": script1})]),
        _Res(tool_calls=[_TC("ops_batch", {"script": script2})]),
        _Res(content="交付物已落盘。"),
    ], ptc=True)
    ag.full_access = True   # 让脚本内写工具能执行并被记录到 _ops_write_report

    evs = list(ag.run([], "产出交付物 deliverable.md", auto_approve=True,
                      contract=[deliverable]))
    gates = [e for e in evs if e.get("type") == "contract_gate"]
    vias = [g.get("via") for g in gates]
    check("PTC 路径下脚本写失败触发契约闸门",
          any(g.get("via") == "ops_batch" for g in gates), str(vias))
    check("契约闸门仅触发一次（staleness 不重复触发）",
          len(gates) == 1, f"gates={len(gates)} vias={vias}")
    check("contract_gate 事件带 missing 字段且含交付物",
          bool(gates) and deliverable in (gates[0].get("missing") or []),
          str(gates[0].get("missing") if gates else None))
    body = (tmp / deliverable).read_text(encoding="utf-8")
    check("交付物最终真实落盘（round2 修正引用后）",
          "修正后的报告" in body, body[:80])
    check("round2 后终态正常到达（最终答复出现，闸门未阻塞）",
          any(e.get("type") == "assistant" and "交付物已落盘" in (e.get("content") or "")
              for e in evs), "未找到最终答复")


def main() -> None:
    test_selector_surface()
    test_name_normalization()
    test_malformed_helper()
    test_top_level_guard()
    test_ops_batch_not_deduped()
    test_ops_batch_contract_gate()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
