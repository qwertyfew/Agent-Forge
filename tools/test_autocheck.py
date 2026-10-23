"""核查打满后的「程序补查」测试（agentforge/autocheck.py + loop 集成）。

规矩（用户 2026-10-21 定）：打回 2 轮仍不合格时，**不再直接盖章**——先由程序自己
去抓一手源；抓到就重新诊断（能对上就正常交付，对不上再给模型最后一轮）；一个源
都没抓到才在交付物上标注。

运行：./.venv/Scripts/python.exe tools/test_autocheck.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import autocheck as A                       # noqa: E402
from agentforge import grounding as G                       # noqa: E402
from agentforge.config import Config                        # noqa: E402
from agentforge.loop import Agent                           # noqa: E402
from agentforge.permissions import Guard                    # noqa: E402
from agentforge.tools import build_registry                 # noqa: E402

GOAL = "寻找git上的项目UI-Critic，这个项目是干嘛的，怎么样操作才能修改前端"
README = ("[实测 · 网页] https://github.com/akins20/ui-critic\n"
          "# ui-critic\n## Quick start\nnpm i -g @akins20/ui-critic\n"
          "然后运行 ui-critic 命令；入口 bin/ui-critic.mjs。\n")
README_BLIND = ("[实测 · 网页] https://github.com/akins20/ui-critic\n"
                "# ui-critic\nA second pair of eyes for a UI. 只讲怎么评审站点，"
                "没有安装与使用命令。\n")


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + "  " + name
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"测试未通过: {name}")


# ---------------------------------------------------------------------------
# A. plan：程序该抓什么（纯确定性）
# ---------------------------------------------------------------------------
def _diag(*, kinds=("zero_retrieval",), repos=(), refs=(), kw="UI-Critic"):
    return {"ok": False, "findings": [{"kind": k} for k in kinds],
            "refs": list(refs), "issues": [], "provenance": [],
            "retrieval": {"repos": list(repos), "task_keyword": kw}}


def test_plan() -> None:
    _check("A1 已通过的诊断不补抓", A.plan({"ok": True}) == [])

    p = A.plan(_diag(repos=["akins20/ui-critic"]))
    _check("A2 有候选仓库 → 抓它的 README",
          p and p[0]["kind"] == "readme" and p[0]["target"] == "akins20/ui-critic", str(p))

    p = A.plan(_diag(repos=[], refs=["marp-team/marp"]))
    _check("A3 没候选但有引用 → 抓引用的仓库",
          p and p[0]["kind"] == "readme" and p[0]["target"] == "marp-team/marp", str(p))

    p = A.plan(_diag(repos=[], refs=[]))
    _check("A4 什么都没有 → 按任务关键词重搜一次",
          p and p[0]["kind"] == "search" and p[0]["target"] == "UI-Critic", str(p))

    p = A.plan(_diag(kinds=("unadmitted_citation",), repos=[],
                     refs=["https://example.com/docs/usage"]))
    _check("A5 非 GitHub 引用 → 直接抓那个地址",
          p and p[0]["kind"] == "url" and p[0]["target"].startswith("https://example.com"),
          str(p))

    p = A.plan(_diag(kinds=("unadmitted_citation",), repos=[],
                     refs=["https://github.com/marp-team/marp-cli/blob/main/README.md"]))
    _check("A6 GitHub 子页引用 → 归一成仓库抓 README",
          p and p[0]["kind"] == "readme" and p[0]["target"] == "marp-team/marp-cli", str(p))

    hist = [{"role": "tool", "content": "[实测 · 网页] https://github.com/akins20/ui-critic\n"
                                        "# ui-critic\nnpm i -g x\n"}]
    p = A.plan(_diag(repos=["akins20/ui-critic"]), history=hist)
    _check("A7 模型已抓过的源不重复抓", p == [], str(p))

    p = A.plan(_diag(repos=["a/one", "b/two", "c/three"]), limit=2)
    _check("A8 抓住上限", len(p) == 2, str(p))

    _check("A9 仓库名归一（@scope/name → owner/name）",
          A.slug_of("@akins20/ui-critic") == "akins20/ui-critic", A.slug_of("@akins20/ui-critic"))
    _check("A10 文件路径不当仓库",
          A.slug_of("src/components") == "", A.slug_of("src/components"))


# ---------------------------------------------------------------------------
# B. run：真抓（用注入的 call 代替网络）
# ---------------------------------------------------------------------------
def test_run() -> None:
    calls = []

    def call(name, **kw):
        calls.append((name, kw))
        if name == "github_search":
            return ("[实测 · API] GitHub 官方搜索接口 · 查询「UI-Critic」\n"
                    "  1. akins20/ui-critic  star=120\n"
                    "     https://github.com/akins20/ui-critic\n")
        if "203.0.113" in kw.get("url", ""):
            return "[错误] 请求失败: ConnectTimeout"
        return README

    got = A.run([{"kind": "readme", "target": "akins20/ui-critic"}], call)
    _check("B1 README 抓取成功", got and got[0]["ok"], str(got))
    _check("B2 抓到的是 GitHub 仓库地址",
          got[0]["url"] == "https://github.com/akins20/ui-critic", got[0]["url"])
    _check("B3 成功源进语料", "npm i -g @akins20/ui-critic" in A.sources(got))
    _check("B4 地址可用于声明", A.urls(got) == ["https://github.com/akins20/ui-critic"])

    bad = A.run([{"kind": "url", "target": "https://203.0.113.9/x"}], call)
    _check("B5 抓失败记为 ok=False", bad and not bad[0]["ok"], str(bad))
    _check("B6 失败不进语料、不进行声明",
          A.sources(bad) == "" and A.urls(bad) == [] and len(A.failures(bad)) == 1)

    got_s = A.run([{"kind": "search", "target": "UI-Critic"}], call, limit=2)
    _check("B7 先检索再抓命中的 README",
          len(got_s) == 2 and got_s[0]["kind"] == "search" and got_s[1]["ok"], str(got_s))
    _check("B8 检索结果作为候选池（供证据准入）",
          "akins20/ui-critic" in A.pool(got_s), A.pool(got_s)[:80])

    def boom(name, **kw):
        raise RuntimeError("网络炸了")

    err = A.run([{"kind": "readme", "target": "a/b"}], boom)
    _check("B9 工具异常不抛出，记成失败",
          err and not err[0]["ok"] and "网络炸了" in err[0]["text"], str(err))


# ---------------------------------------------------------------------------
# C. 交给模型的最后一轮：只对齐，不再搜
# ---------------------------------------------------------------------------
def test_prompt() -> None:
    got = [{"kind": "readme", "target": "akins20/ui-critic",
            "url": "https://github.com/akins20/ui-critic", "text": README, "ok": True}]
    txt = A.render_prompt(got)
    _check("C1 标题写明最后一轮", "【程序补查 · 最后一轮】" in txt, txt[:40])
    _check("C2 附上抓到的源正文", "npm i -g @akins20/ui-critic" in txt)
    _check("C3 不许再发起新检索", "不要再发起新的检索" in txt)
    _check("C4 没抓到源就不生成提示", A.render_prompt([{**got[0], "ok": False}]) == "")

    big = {**got[0], "text": "x" * 20000}
    _check("C5 单源有预算上限", len(A.render_prompt([big])) < 4000,
          str(len(A.render_prompt([big]))))


# ---------------------------------------------------------------------------
# D. 声明与诊断口径
# ---------------------------------------------------------------------------
def test_note() -> None:
    diag = {"ok": False, "issues": ["python main.py"], "refs": [],
            "findings": [{"kind": "ungrounded_command"}]}
    plain = G.render_unverified_note(diag)
    _check("D1 没抓过：仍写「未经核实」", "未经核实" in plain and "打回 2 轮" in plain, plain)
    checked = G.render_unverified_note(diag, checked=["https://github.com/a/b"])
    _check("D2 抓过：写明程序补抓了几个源", "自行补抓 1 个源" in checked, checked)
    _check("D3 抓过：要求不要照着操作", "不要照着操作" in checked, checked)
    _check("D4 通过时不给声明", G.render_unverified_note({"ok": True}) == "")

    lie = "根据官方 README 提取了安装命令：`npm i -g x`"
    _check("D5 模型没抓却说核实 → 判虚假来源",
          bool(G.false_provenance_issues(lie, [])))
    _check("D6 程序补抓过 → 不再判虚假来源（源确实被核对过）",
          G.false_provenance_issues(lie, [], program_fetched=["https://github.com/a/b"]) == [])


# ---------------------------------------------------------------------------
# E. 循环集成：打满之后的三种走法
# ---------------------------------------------------------------------------
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
        idx = min(len(self.rounds) - 1, len(self.script) - 1)
        yield "result", self.script[idx]


def _mk_agent(tmp: Path, script, auto_call) -> Agent:
    cfg = Config.load(str(ROOT / "config.yaml"))
    cfg.workspace_root = tmp
    cfg.compact_auto = False
    cfg.ptc_resident = False
    cfg.autocheck_max_fetch = 3
    guard = Guard(cfg, approver=None)
    registry = build_registry(guard, cfg)
    ag = Agent(cfg, client=None, registry=registry, guard=guard,
               router=None, hub=_StubHub([]))
    ag.hub = _StubHub(script)
    ag._auto_call = auto_call
    return ag


def _run(ag: Agent, text: str, history=None):
    ag.hub.rounds = []
    return list(ag.run(history if history is not None else [], text,
                       auto_approve=True, contract=[]))


BAD = "安装：`npm i -g @akins20/ui-critic`，然后运行 `ui-critic`。"
HONEST = "程序抓到的那份 README 只讲怎么评审站点，未提到安装命令。"


def test_loop(tmp: Path) -> None:
    # ---- E1: 补抓到真源、且能对上 → 直接放行，不盖章 ----
    def call_hit(name, **kw):
        if name == "github_search":
            return ("[实测 · API] GitHub 官方搜索接口 · 查询「UI-Critic」\n"
                    "  1. akins20/ui-critic  star=120\n"
                    "     https://github.com/akins20/ui-critic\n")
        return README

    ag = _mk_agent(tmp, [_Res(content=BAD)], call_hit)
    evs = _run(ag, GOAL)
    finals = [e for e in evs if e.get("type") == "assistant"]
    _check("E1 补抓能对上：正常交付", finals and "程序核查未通过" not in finals[-1]["content"],
          finals[-1]["content"][:80] if finals else "无")
    _check("E2 补抓能对上：告知用户核查改为通过",
          any(e.get("type") == "notify" and "核查改为通过" in str(e.get("text"))
              for e in evs), str([e.get("text") for e in evs if e.get("type") == "notify"]))
    _check("E3 补抓能对上：不再走最后一轮", len(ag.hub.rounds) == 3,
          str(len(ag.hub.rounds)))

    # ---- E4: 抓不到 → 才盖章，且模型不多烧一轮 ----
    def call_fail(name, **kw):
        return "[错误] 请求失败: ConnectTimeout"

    ag = _mk_agent(tmp, [_Res(content=BAD)], call_fail)
    evs = _run(ag, GOAL)
    finals = [e for e in evs if e.get("type") == "assistant"]
    _check("E4 抓不到：照样交付但带声明",
          finals and "程序核查未通过" in finals[-1]["content"],
          finals[-1]["content"][-80:] if finals else "无")
    _check("E5 抓不到：不白烧一轮模型", len(ag.hub.rounds) == 3, str(len(ag.hub.rounds)))
    _check("E6 抓不到：告知用户补抓失败",
          any("没拿到可用材料" in str(e.get("text")) for e in evs if e.get("type") == "notify"),
          str([e.get("text") for e in evs if e.get("type") == "notify"]))

    # ---- E7: 抓到了但对不上 → 最后一轮对齐；模型改对后放行 ----
    def call_blind(name, **kw):
        if name == "github_search":
            return ("[实测 · API] GitHub 官方搜索接口 · 查询「UI-Critic」\n"
                    "  1. akins20/ui-critic  star=120\n"
                    "     https://github.com/akins20/ui-critic\n")
        return README_BLIND

    # 前两轮必被拒（第一轮零检索、第二轮零检索），第三轮打满 → 程序补查 → 第 4 轮对齐
    ag = _mk_agent(tmp, [_Res(content=BAD), _Res(content=BAD), _Res(content=BAD),
                         _Res(content=HONEST)], call_blind)
    evs = _run(ag, GOAL)
    finals = [e for e in evs if e.get("type") == "assistant"]
    _check("E7 对不上：多给一轮模型", len(ag.hub.rounds) == 4, str(len(ag.hub.rounds)))
    last_prompt = str([m.get("content") for m in ag.hub.rounds[-1]])
    _check("E8 最后一轮提示带上了程序抓的源",
          "程序补查 · 最后一轮" in last_prompt and "github.com/akins20/ui-critic" in last_prompt,
          last_prompt[:200])
    _check("E9 模型改对后不盖章",
          finals and "程序核查未通过" not in finals[-1]["content"],
          finals[-1]["content"][-60:] if finals else "无")

    # ---- E10: 最后一轮还是对不上 → 盖章，且声明写明「程序抓过」 ----
    ag = _mk_agent(tmp, [_Res(content=BAD)], call_blind)
    evs = _run(ag, GOAL)
    finals = [e for e in evs if e.get("type") == "assistant"]
    _check("E10 最后一轮仍不合格：写进声明",
          finals and "程序核查未通过" in finals[-1]["content"],
          finals[-1]["content"][-90:] if finals else "无")
    _check("E11 声明区分「抓过但对不上」",
          finals and "自行补抓" in finals[-1]["content"],
          finals[-1]["content"][-90:] if finals else "无")
    _check("E12 只补查一次（不循环抓）", len(ag.hub.rounds) == 4, str(len(ag.hub.rounds)))


def main() -> None:
    test_plan()
    test_run()
    test_prompt()
    test_note()
    test_loop(Path(tempfile.mkdtemp(prefix="af_autocheck_")))
    print("\n全部通过")


if __name__ == "__main__":
    main()
