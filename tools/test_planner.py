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
    KIND_DIM, KIND_LABEL, KIND_SIGNALS, kind_of, looks_like_change,
    resolve_kind, short_title,
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


def test_change_beats_writing() -> None:
    """改代码的活不许落到「写作」——第二个实测事故（用户原话「其两个指令都没有执行」）。

    用户发给 agent 的消息里有两条指令，第 2 条是「每当你的思维链在完全打印出来以后，
    直接将思维链隐藏，并根据用户的文字偏好…将思维链的结果总结成简短的一句话」。
    这是要改 `web/index.html` 的渲染，只因为句子里有一个"总结"，整条被判成**写作**
    （计划卡上写着「所需能力：文本生成、逻辑抽象与格式调整」），于是按写作维度挑模型；
    模型于是去"写一段说明"、"记下您的偏好"，一个文件都没碰。

    判据刻意只看**搭配**：改动物件（代码/界面/思维链/…) 与 改动动作（修改/隐藏/去掉/…）
    同处一个小句，就是「要动手改东西」，不猜意图。
    """
    accident = [
        # 子任务书原样（调度模型复述后的那一条）
        "根据用户的文字偏好（从记忆或上下文获取），修改思维链输出机制：1. 在完全打印出"
        "思维链后，将其隐藏；2. 将完整的思维链结果总结成简短的一句话（例如：'决定调用"
        "工具 fetch_json' 或 '将任务拆解为若干步骤...'）并展示。",
        # 用户原话
        "每当你的思维链在完全打印出来以后，直接将思维链隐藏，并根据用户的文字偏好"
        "（会在记忆里面），将思维链的结果总结成简短的一句话",
        # 同一条消息里的第 1 条指令
        "定位并修复导致在对话中返回非本次生成的文档（如 evolution.md）的逻辑 bug，"
        "并修改代码以解决该问题。",
        "把 web/index.html 里的泳道头部删掉",
        "帮我修复一下登录的 bug",
    ]
    for t in accident:
        k = kind_of(t)
        check(f"改代码的活不判写作：{t[:22]} → {k}", k == "coding", k)

    # 反向：真·写作任务不能被这条规则吃掉（否则换个方向坏）
    for t, allowed in (("写一份产品介绍文档，总结一下卖点。", ("writing",)),
                       ("把这段英文翻译成中文。", ("writing",)),
                       ("帮我写个周报，总结本周进展", ("writing",)),
                       ("帮我查一下 git 上那个 REA 项目是什么，介绍一下。",
                        ("writing", "analysis", "general"))):
        k = kind_of(t)
        check(f"真写作任务不误收：{t[:18]} → {k}", k in allowed, k)

    # 调度模型给的 kind 也要过规则复核（dispatcher._llm_plan 走的是这条）
    check("调度模型误标 writing → 纠回 coding",
          resolve_kind(accident[0], "writing") == "coding",
          resolve_kind(accident[0], "writing"))
    check("真写作任务：模型说 writing 就随它",
          resolve_kind("写一份产品介绍文档", "writing") == "writing")
    check("模型给了不认识的标签 → 回退按规则定性",
          resolve_kind(accident[0], "nonsense") == "coding")
    check("模型没给标签 → 与 kind_of 同源",
          resolve_kind(accident[0], "") == kind_of(accident[0]))
    check("用户自己的 kind 不被规则推翻（只说 analysis 就保持 analysis）",
          resolve_kind("分析一下这段代码", "analysis") == "analysis")

    # 判据本身：看搭配，不看有没有出现某个动词
    check("搭配判据：有动作无对象 → 否", not looks_like_change("把这段英文翻译成中文"))
    check("搭配判据：有对象无动作 → 否", not looks_like_change("看看 index.html"))
    check("搭配判据：动作+对象同句 → 是", looks_like_change("把 index.html 里的按钮隐藏"))
    check("搭配判据：隔着句号不算同句", not looks_like_change("按钮。修改"))


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


def test_short_title() -> None:
    """短标题不许按字数硬切。

    事故现场（端到端实测）：用户要求里有「…非本次生成的文档（如 evolution.md）」，
    计划卡第一条显示成 `定位并修复导致在对话中返回非本次生成的文档（如 ev`——
    切在括号里面、断在半截单词上，收尾括号也没了。计划卡是用户看任务的第一眼。
    """
    goal = "定位并修复导致在对话中返回非本次生成的文档（如 evolution.md），并说明返回的逻辑"
    t = short_title(goal, 26)
    check("事故现场：不再切出「（如 ev」这种断头括号", "（如 ev" not in t, t)
    check("事故现场：退回未闭合左括号之前，是一个完整的短句",
          t == "定位并修复导致在对话中返回非本次生成的文档…", t)
    check("被截断时补省略号（一眼看出这不是任务本名）", t.endswith("…"), t)

    # 括号里正好能在界内闭合时，整对括号一起保留
    t2 = short_title("根据用户的文字偏好（从记忆或上下文获取），修改思维链显示逻辑", 26)
    check("括号对完整保留，不留下半截", t2 == "根据用户的文字偏好（从记忆或上下文获取）…", t2)

    # 有子句边界就在边界收尾，读起来是一句完整的话
    t3 = short_title("第一步，先执行 list_dir 看看目录结构；然后读文件", 26)
    check("在子句边界收尾，且不留「，…」这种别扭组合",
          t3.endswith("…") and not t3.endswith("，…") and "目录结构" in t3, t3)

    # 西文单词 / 路径不许被切成两半
    t4 = short_title("检查 agentforge/artifact.py 与 dispatcher.py 的产出归因", 20)
    check("不切在西文单词中间", not t4.rstrip("…").endswith("agentforge/artifact.p"), t4)
    check("切词时退回整词，仍以省略号收尾", t4.endswith("…"), t4)

    # 硬约束：长度不超过 limit（侧栏标签有 90 字硬上限，见 test_taskstore）
    for limit in (8, 12, 26, 90):
        for s in (goal, "a" * 300, "短", "中" * 200):
            check(f"len <= {limit}：{s[:10]}…", len(short_title(s, limit)) <= limit,
                  f"limit={limit} len={len(short_title(s, limit))}")

    check("本来就不长时原样返回，不画蛇添足加省略号",
          short_title("读一下 README", 26) == "读一下 README", short_title("读一下 README", 26))
    check("换行与多余空白压成单个空格",
          short_title("第一行\n\n  第二行", 26) == "第一行 第二行", short_title("第一行\n\n  第二行", 26))
    check("空输入不炸", short_title("", 26) == "" and short_title(None, 26) == "",
          repr(short_title(None, 26)))

    # 计划卡与派工标题必须同源：dispatcher 不许再自己硬切一遍
    import inspect as _inspect
    from agentforge import dispatcher as _d
    src = _inspect.getsource(_d)
    check("dispatcher 里不再有 goal[:26] 这种硬切", "goal[:26]" not in src, "还有硬切")
    check("dispatcher 用的是 planner.short_title", "short_title" in src)
    # 同上：光有 resolve_kind 这个函数不算数，采纳调度模型的 kind 时必须真的过它
    check("dispatcher 采纳调度模型的 kind 时过了规则复核",
          "resolve_kind(" in src, "没接上 resolve_kind")


def main() -> None:
    test_regressions()
    test_inspect()
    test_not_inspect()
    test_coding_beats_writing()
    test_change_beats_writing()
    test_unknown_text()
    test_kind_contract()
    test_dispatcher_prompt_lists_every_kind()
    test_short_title()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
