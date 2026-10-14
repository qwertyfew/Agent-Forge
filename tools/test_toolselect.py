"""工具按需下发（toolselect）与「简单对话」判据的自测。

运行：python tools/test_toolselect.py
全部断言内建，退出码 0 = 全过。不需要模型在线——只用真实工具的描述做关键词匹配。

两个事故现场（都来自端到端实测）：

1. 「检索一下 GitHub 上叫 ERA 的项目，看看是干什么的。」——又短又口语，
   按需下发只命中了 `web_search`，`fetch_json` / `fetch_url` 落选、没给 schema。
   模型手里只剩最弱的检索工具，于是「只用搜索摘要、不抓一手来源」；
   同一个需求写长到 138 字就命中了 `fetch_json`。**能力不能被问句措辞决定。**
   对策：能力族成套下发（见 toolselect.FAMILIES）。

2. 上面那句话只有 31 字，若性质判成 general，就够格被当成「简单对话」交给
   决策模型（零工具）。**问题简短 ≠ 问题简单**——哥德巴赫猜想只有六个字。
   对策：判据只看「性质 general + 有没有要动手/要外部事实的信号」，篇幅只单向否决长。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                       # noqa: E402
from agentforge.dispatcher import TaskSpec, is_simple_chat  # noqa: E402
from agentforge.permissions import Guard                   # noqa: E402
from agentforge.toolselect import FAMILIES                 # noqa: E402
from agentforge.tools import build_registry                # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


cfg = Config.load(str(ROOT / "config.yaml"))
reg = build_registry(Guard(cfg, approver=None), cfg)
sel = reg.selector

# 事故现场的原话，以及同一需求的「写长版」
SHORT_VAGUE = "检索一下 GitHub 上叫 ERA 的项目，看看是干什么的。"
LONG_CLEAR = ("检索并查询 GitHub 上的项目 REA（Reverse Engineer Anything，"
              "https://github.com/morluto/rea），做一份项目介绍：定位/解决问题、"
              "技术栈、主要功能、集成方式、仓库活跃度（star 数、fork 数、最近更新时间）。")

FIRST_PARTY = ("fetch_url", "fetch_json", "browser_snapshot")


def names_for(text: str) -> list[str]:
    return [(s.get("function") or {}).get("name") for s in sel.select([text])]


# ---------------------------------------------------------------- 按需下发

def test_first_party_available() -> None:
    """事故现场：短而口语的检索提问，也必须拿到抓一手来源的工具。"""
    got = names_for(SHORT_VAGUE)
    for t in FIRST_PARTY:
        check(f"短口语检索提问下发 {t}", t in got, f"实得 {len(got)} 个：{got}")


def test_family_complete() -> None:
    """检索族成套：只要族里有人入选，整族都要在。"""
    for text in (SHORT_VAGUE, LONG_CLEAR, "用 GitHub API 查 morluto/rea 的 star 数。"):
        got = names_for(text)
        fam = next(f for f in FAMILIES if "web_search" in f)
        if "web_search" in got:
            for t in fam:
                check(f"族成套：{t} 随 web_search 一起下发（{len(text)} 字）", t in got, str(got))
        else:
            check(f"未命中检索族时不强塞（{len(text)} 字）", True)


def test_length_does_not_gate() -> None:
    """同一件事写长写短，能力集合不能被长度改写。"""
    short_got, long_got = set(names_for(SHORT_VAGUE)), set(names_for(LONG_CLEAR))
    for t in FIRST_PARTY:
        check(f"长短无关：{t} 两种篇幅都在", t in short_got and t in long_got,
              f"短={sorted(short_got)} 长={sorted(long_got)}")


def test_no_false_family() -> None:
    """纯本地任务不该被塞进整族检索工具（省上下文）。"""
    for text in ("把 agentforge/loop.py 里的工具超时改成 60 秒。",
                 "在 tools/ 下新建一个 test_foo.py，写两条断言。"):
        got = names_for(text)
        check(f"本地任务不引入 web_search —— {text[:16]}", "web_search" not in got, str(got))


def test_all_names_exist() -> None:
    """下发的名字必须都是注册表里真实存在的（族里写了不存在的名字也不能崩）。"""
    for fam in FAMILIES:
        sel._complete_families(list(fam) + ["__not_a_tool__"])
    got = names_for(SHORT_VAGUE)
    check("下发清单全是真实工具", all(n in reg.names() for n in got), str(got))


# ---------------------------------------------------------------- 简单对话判据

def test_is_simple_chat() -> None:
    def simple(goal: str, kind: str = "general") -> bool:
        return is_simple_chat(TaskSpec(id="t", goal=goal, kind=kind))

    check("事故现场：短检索句不再判成简单对话", not simple(SHORT_VAGUE))
    check("短但含取数需求：不判简单", not simple("帮我查查那个仓库现在多少 star"))
    check("纯闲聊仍然判简单（决策模型接得住）", simple("你好，你是谁"))
    check("追问式闲聊仍判简单", simple("谢谢，再说说上一条"))
    check("超长目标一律不判简单（单向上限）", not simple("嗯" * 200))
    check("非 general 类一律不判简单", not simple("你好", kind="coding"))
    check("kind=inspect 不判简单", not simple("看一下这个目录", kind="inspect"))


def test_github_search_in_family() -> None:
    """「找某个 GitHub 项目」光有 web_search 会静默劣化——候选清单得靠官方接口。

    实测（REA 检索四跑）：web_search 打回来的全是中文镜像站与转载文章，正主
    （19 万 star）一条都没有，模型于是锚定了它看到的第一个 GitHub 链接（80 star
    的镜像站）。所以按名字搜仓库这一档必须和检索族一起下发。
    """
    fam = next(f for f in FAMILIES if "web_search" in f)
    check("github_search 在检索族里（缺了它只能靠搜索摘要猜哪个是正主）",
          "github_search" in fam, str(fam))
    check("github_search 确实注册过（族里写了不存在的名字等于没写）",
          "github_search" in reg.names())
    got = names_for(SHORT_VAGUE)
    check("事故原句下发了 github_search", "github_search" in got, str(got))
    check("github_search 是只读工具（不该混进写 / 执行族）",
          reg.get("github_search").risk == "safe")


def main() -> int:
    for fn in (test_first_party_available, test_family_complete,
               test_length_does_not_gate, test_no_false_family,
               test_all_names_exist, test_github_search_in_family,
               test_is_simple_chat):
        print(f"\n--- {fn.__name__} ---")
        fn()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
