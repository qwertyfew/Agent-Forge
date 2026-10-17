"""Leader 双环调度（第 47 步：七步架构更新）。

与旧「一次性拆完再跑」的区别：

    旧（run_multi_agent）  决策模型一次拆出全部子任务 → 拓扑排序跑完 → 汇总
    新（run_leader）      Leader 外环滚动规划：每轮只规划【下一批】小步骤 →
                          员工并行执行（内环保留多轮工具调用）→ Leader 评估
                          （完成判定 + 规划下一批 + 精简历史，一次调用做完）
                          → 全部完成出「拟回答」→ 程序裁定 + Leader 语义验收。

上下文采用【固定槽位】结构（架构级保护，不是提示词约定）：

    [system] [任务原文槽(不可变)] [失败原因槽(最近K条)] [历史摘要槽(滚动)]
    [本批执行结果(全量,只活一轮)]

任务原文与失败原因放独立槽位，压缩/替换只发生在历史摘要槽——Leader 上下文
头部字节级稳定，服务端上下文缓存（DeepSeek 等）可以完整命中；同时满足
「最新一轮结果全量进 Leader、历史不膨胀」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dispatcher import TaskSpec, _catalog_text, _extract_json
from .planner import KIND_DIM, KIND_LABEL, kind_of, short_title

# 六类能力判定表（2.2：保留六类而不是四类——检索/看现场是真实高频能力）
KINDS = ("coding", "inspect", "writing", "math", "analysis", "general")

KINDS_TEXT = """- coding    编码/改代码（写脚本、改函数、跑命令改文件）
- inspect   看现场（列目录、读文件、查现有实现、检索信息）
- writing   文案/文档写作
- math      数学计算
- analysis  分析/推理（读懂材料后下结论，不写代码不写文档）
- general   其他杂务（无法归入上述五类的零碎活）"""

# 失败原因保留条数（2.4：累积保留最近 K 条，旧教训不被新失败冲掉）
REASONS_KEEP = 3

LEADER_PLAN_SYSTEM = """你是 AgentForge 的任务指挥者（Leader）。用户给你一段任务原文，你把它规划成
一批【小步骤】交给员工模型执行。你只负责规划与验收，自己绝不动工具、不读写文件。

小步骤判定标准（硬性，输出前自检）：每个小步骤必须能概括为以下六类能力中的
**一类且仅一类**，做不到就继续拆细：
""" + KINDS_TEXT + """

小步骤提示词（prompt 字段）写法（两条边界都要守住）：
- 【必须自包含操作信息】：执行者只看这一条提示词就要能干活——用到哪个路径、
  哪些文件、期望产出什么（写到哪个文件/回复里给什么格式），全部写明；
  原文中的交付物要求（文件名/路径/格式）必须原样出现在对应步骤的 prompt 里；
  原文写明的验证命令（如「验证：python tools/x.py」）原样保留进对应步骤。
- 【不要包含背景叙事】：不要复述任务总目标与整体方案——执行者不需要知道项目
  全貌，把自己负责的那一块弄好就行。

分批规则：
- 本批 1~N 个小步骤；【只有相互独立（谁都无需等待别人的产出）的步骤才能放进
  同一批】；后一步要用前一步产出时，留到下一批——你会先看到本批的执行结果；
- 每批只规划「当前信息下确定能做」的步骤，宁少勿错，做一步看一步；
  拆分本身有交接成本，能并成一批独立步骤的不要拖成多批；
- model 只在某个候选员工明显更合适时才填它的名字，否则留空（系统按能力分维度挑）；
  不要把任务派给你自己——你是 Leader，不执行。

候选员工（只能从这些名字里选；格式：名字（各维度分））：
{catalog}

只输出一个 JSON 对象，不要任何解释与代码块围栏：
{{"batch":[{{"title":"步骤短名","kind":"coding","prompt":"给执行者的完整提示词","model":""}}],
"rationale":"一句话说明这批为什么这样拆"}}"""

LEADER_EVAL_SYSTEM = """你是 AgentForge 的任务指挥者（Leader）。你此前把任务拆成小步骤交给员工执行，
现在你收到最新一批的执行结果。你要在同一份 JSON 里完成：完成判定 + 规划下一批 + 精简历史。

完成判定（对照【任务原文】逐项核对）：
- 原文要求做的事是否每一项都有对应产出？
- 交付物的文件名/路径/格式是否与原文一致？
- 全部满足才允许 done=true；缺任何一项都是 done=false。

输出字段：
- done     本任务是否已完成（布尔）
- answer   done=true 时必填：拟回答，面向用户的完整答复，自包含（用户不会再看到
           执行过程），关键结论与产出文件名都要写进去
- batch    done=false 时必填：下一批小步骤，格式与规划时相同
           （[{{"title","kind","prompt","model"}}]；仅相互独立的才同批）
- reasons  done=false 时必填：本批暴露的失败原因/差距清单，一条一行——明确差什么、
           错在哪。架构会把它们原样保留并传给后续规划，不会被压缩丢弃。
- note     对【截至本轮全部执行历史】的精简摘要（滚动覆盖）：保留对后续有用的事实
           （已确认的结论、产出文件路径、走过的死路及其原因），删掉过程性细节。
           架构会用它替换掉之前所有轮次的历史，所以它必须自包含。

候选员工（给 batch.model 用，只能从这些名字里选）：
{catalog}

只输出一个 JSON 对象，不要任何解释与代码块围栏。"""

LEADER_SEMANTIC_SYSTEM = """你是 AgentForge 的最终验收官。给你一段【任务原文】和一份【拟回答】，
请判定这份回答是否完成了原文的全部要求：要求做的事是否都做了、交付物是否都给到
（文件名/路径/格式）、有没有明显的事实缺口。只看这两份材料，不要脑补执行过程。

只输出一个 JSON 对象：
{{"pass": true/false, "reasons": ["不通过时逐条列出缺什么/错在哪；通过时为空数组"]}}"""


# ---------------------------------------------------------------- Leader 上下文


@dataclass
class StepResult:
    """一批里某个小步骤的执行结果（进入 Leader 评估输入）。"""
    title: str
    kind: str
    prompt: str
    reply: str = ""
    files: list = field(default_factory=list)
    failed: bool = False
    error: str = ""


class LeaderContext:
    """Leader 的任务上下文：固定槽位 + 滚动摘要。

    任务原文与失败原因是架构级保护的独立槽位——后续任何「精简/压缩」都只
    替换 note 槽，绝不触碰前两者（2.4/步骤④：更新完再压缩，最新一轮全量进）。
    """

    def __init__(self, original: str):
        self.original = original or ""
        self.reasons: list[str] = []       # 最近 REAS_KEEP 条，累积
        self.note = ""                     # 执行历史精简摘要（滚动覆盖）
        self.round = 0                     # 已执行的批次数

    # ---- 槽位更新（架构级：只有这三条路径能改上下文）----
    def add_round(self) -> None:
        self.round += 1

    def add_reasons(self, rs) -> None:
        keep = [str(r).strip() for r in (rs or []) if str(r).strip()]
        if keep:
            self.reasons = (self.reasons + keep)[-REASONS_KEEP:]

    def set_note(self, note: str) -> None:
        self.note = (note or "").strip()

    # ---- 消息组装 ----
    def _slots(self) -> list[dict]:
        msgs = [{"role": "user", "content": f"【任务原文】\n{self.original}"}]
        if self.reasons:
            msgs.append({"role": "user", "content":
                         "【未完成原因（此前尝试的教训，规划与判定时必须规避）】\n"
                         + "\n".join(f"- {r}" for r in self.reasons)})
        if self.note:
            msgs.append({"role": "user", "content":
                         f"【执行历史摘要（更早轮次的全部历史已精简为此）】\n{self.note}"})
        return msgs

    def plan_messages(self) -> list[dict]:
        """规划下一批的输入：原文 + 失败原因 + 历史摘要。"""
        msgs = self._slots()
        msgs.append({"role": "user", "content":
                     "请规划下一批小步骤（JSON 格式见系统提示）。"
                     + ("优先补齐上面「未完成原因」里列出的缺口。" if self.reasons else "")})
        return msgs

    def eval_messages(self, batch: list[StepResult]) -> list[dict]:
        """评估输入：固定槽位 + 本批全量（回复不截断——最新一轮全量进 Leader）。"""
        msgs = self._slots()
        blocks = []
        for i, r in enumerate(batch, 1):
            body = (f"[{i}] {r.title}（{KIND_LABEL.get(r.kind, r.kind)}）\n"
                    f"指令：{r.prompt}\n")
            if r.failed:
                body += f"执行失败：{r.error or '（未产出结果）'}"
            else:
                body += f"员工回复（全文）：\n{r.reply or '（空）'}"
                if r.files:
                    body += "\n产出文件：" + "、".join(r.files[:8])
            blocks.append(body)
        msgs.append({"role": "user", "content":
                     f"【第 {self.round} 批执行结果（全量）】\n" + "\n\n".join(blocks)
                     + "\n\n请按系统提示输出判定 JSON。"})
        return msgs


# ---------------------------------------------------------------- JSON 解析与校验


def parse_batch(obj, entries: list[dict], resolver=None,
                start_index: int = 1) -> tuple[list[TaskSpec], str]:
    """把 Leader 输出的 JSON 解析成一排 TaskSpec（一批小步骤）。

    校验规则：prompt 为空的条目丢弃；kind 不在六类里时按 prompt 文本推断；
    兜底整批失败返回空列表。解析成功时每个 TaskSpec.goal = Leader 写的
    小步骤提示词（自包含操作信息，员工只看这一条）。

    resolver（可选）：Dispatcher._resolve——沿用旧指派语义（决策模型不接重活、
    点名的模型明显偏弱时改派最强者、原因如实写进 reason）。不传则按维度挑最强。
    """
    if not isinstance(obj, dict):
        return [], ""
    items = obj.get("batch") or obj.get("tasks")
    if not isinstance(items, list):
        return [], ""
    specs: list[TaskSpec] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        prompt = str(it.get("prompt") or it.get("goal") or "").strip()
        if not prompt:
            continue
        kind = str(it.get("kind") or "").strip()
        if kind not in KINDS:
            kind = kind_of(prompt)
        specs.append(TaskSpec(
            id=f"l{start_index + len(specs)}",
            goal=prompt,
            kind=kind,
            capability=str(it.get("capability") or "").strip(),
            model=str(it.get("model") or "").strip(),
            title=str(it.get("title") or "").strip(),
            needs_chinese=bool(it.get("needs_chinese", True)),
        ))
    if not specs:
        return [], ""
    if resolver is not None:
        resolver(specs, entries)          # 指派 + index + reason（旧语义全保留）
    else:
        for i, t in enumerate(specs, start_index):
            t.index = i
            t.title = t.title or short_title(t.goal)
            dim = KIND_DIM.get(t.kind, "reasoning")
            best = max(entries, key=lambda e: (e["scores"].get(dim, 0), e.get("total", 0)))
            t.model_key, t.model_name, t.model_id = best["key"], best["name"], best["id"]
            t.score = best["scores"].get(dim, 0)
            t.reason = f"{KIND_LABEL.get(t.kind, t.kind)}维度最强（{t.score} 分）"
    return specs, str(obj.get("rationale") or "").strip()


def parse_eval(obj) -> dict:
    """解析评估输出，容忍字段缺失：返回规范化 dict。"""
    if not isinstance(obj, dict):
        return {}
    out = {
        "done": bool(obj.get("done")),
        "answer": str(obj.get("answer") or "").strip(),
        "note": str(obj.get("note") or "").strip(),
        "reasons": [str(r).strip() for r in (obj.get("reasons") or []) if str(r).strip()],
        "raw_batch": obj.get("batch"),
    }
    if out["done"] and not out["answer"]:
        out["done"] = False
        out["reasons"] = out["reasons"] or ["Leader 判定完成但没有给出拟回答"]
    return out
