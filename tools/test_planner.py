"""任务定性（planner.kind_of）的自测，外加「新增一种 kind 时的四件套」不变量。

运行：python tools/test_planner.py
全部断言内建，退出码 0 = 全过。纯规则测试，不需要模型在线。

用例取自真实事故（端到端实测）：任务书是「先列出当前工作区根目录下的文件，
然后用一句话说明你都做了什么」，kind_of 只因为「说明」两个字就把它判成**写作**，
于是交给一个擅长长文的模型——它没去读工作区，直接编了一份目录清单。
「要读现场的任务」不能被定性成「写作」，否则选模型时看的是文风而不是推理。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.planner import (                        # noqa: E402
    KIND_DIM, KIND_LABEL, KIND_SIGNALS, kind_of,
)
from agentforge.router import _CAP_DIMS                 # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------- 事故现场

def test_regressions() -> None:
    ask = "先列出当前工作区根目录下的文件（不要读内容），然后用一句话说明你都做了什么。"
    k = kind_of(ask)
    check("事故现场：列工作区文件不再判成「写作」", k != "writing", k)
    check("事故现场：判成「检视」（要读现场的任务）", k == "inspect", k)


def test_inspect() -> None:
    cases = [
        "这个项目有哪些目录？",
        "工作区里有哪些 .py 文件？",
        "读一下 README 的架构演进一节，总结一下。",
        "show me the files in the project root",
    ]
    for c in cases:
        k = kind_of(c)
        check(f"检视类：{c[:26]} → {k}", k == "inspect", k)
    # 提到具体源码文件、或带上「测试」这类重词时归「编码」也算对（选模型看的是编码能力），
    # 关键是别再落到「写作」——那才是这次出事的那一类
    for c in ("看一下 agentforge/loop.py 里工具超时是怎么处理的。",
              "看看 web/index.html 里 renderHistory 做了什么",
              "列一下 tools/ 下的常驻回归测试"):
        k = kind_of(c)
        check(f"读某源码文件：可以是检视或编码，但不能是写作 —— {c[:20]}", k in ("inspect", "coding"), k)


def test_not_inspect() -> None:
    cases = [
        ("帮我查一下 git 上那个 REA 项目是什么，介绍一下。", ("writing", "analysis", "general")),
        ("实现一个 LRU 缓存", ("coding",)),
        ("写一个 hello.py，打印 hello。", ("coding",)),
        ("把这次改动记录到 README 然后 push。", ("writing", "coding")),
        ("今天天气怎么样", ("general",)),
    ]
    for text, allowed in cases:
        k = kind_of(text)
        check(f"不误判成检视：{text[:24]} → {k}", k in allowed, k)


def test_coding_beats_writing() -> None:
    # 老的优先级不能因为新增一类就丢了：「写个脚本」里的"写"是实现，不是写文章
    for text in ("写个脚本把日志按天切分", "写一段 Python 代码解析 CSV"):
        check(f"编码优先于写作：{text[:20]}", kind_of(text) == "coding", kind_of(text))


def test_unknown_text() -> None:
    check("没命中任何信号 → general", kind_of("今天天气怎么样") == "general")
    check("空文本 → general", kind_of("") == "general")
    check("None 安全 → general", kind_of(None) == "general")


# ---------------------------------------------------------------- 不变量

def test_kind_contract() -> None:
    """新增一种 kind 要同步四处，少一处就是线上 KeyError 或静默退化。

    这四条断言就是「同步清单」本身——加 kind 时忘了改哪儿，这里会红。
    """
    for k in KIND_LABEL:
        # general 是兜底类，故意没有信号词——什么都命中不了才落到它
        if k != "general":
            check(f"kind「{k}」有信号词", k in KIND_SIGNALS and bool(KIND_SIGNALS[k]))
        check(f"kind「{k}」有维度映射 KIND_DIM（orchestrator._single_task 直接下标取，缺了就崩）",
              k in KIND_DIM, repr(sorted(KIND_DIM)))
        check(f"kind「{k}」有能力维度 _CAP_DIMS（router 缺了就静默退回 reasoning）",
              k in _CAP_DIMS, repr(sorted(_CAP_DIMS)))
    for k in KIND_SIGNALS:
        check(f"信号词表里的「{k}」在 KIND_LABEL 里有中文名（前端要显示）", k in KIND_LABEL)
    check("KIND_DIM / KIND_LABEL / _CAP_DIMS 三张表 key 集合一致",
          set(KIND_LABEL) == set(KIND_DIM) == set(_CAP_DIMS),
          f"{sorted(KIND_LABEL)} / {sorted(KIND_DIM)} / {sorted(_CAP_DIMS)}")
    check("KIND_SIGNALS = 三张表减去兜底的 general",
          set(KIND_SIGNALS) == set(KIND_LABEL) - {"general"},
          f"{sorted(KIND_SIGNALS)}")


def test_dispatcher_prompt_lists_every_kind() -> None:
    """调度提示里得把每种 kind 都告诉决策模型，否则它永远拆不出这一类。"""
    from agentforge.dispatcher import DISPATCH_SYSTEM, NOT_SIMPLE_KINDS
    for k in KIND_LABEL:
        check(f"调度提示写到「{k}」", k in DISPATCH_SYSTEM)
    for k in ("coding", "writing", "analysis", "math", "inspect"):
        check(f"「{k}」算重活（不交给只会调度的小模型）", k in NOT_SIMPLE_KINDS)


def main() -> None:
    test_regressions()
    test_inspect()
    test_not_inspect()
    test_coding_beats_writing()
    test_unknown_text()
    test_kind_contract()
    test_dispatcher_prompt_lists_every_kind()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
