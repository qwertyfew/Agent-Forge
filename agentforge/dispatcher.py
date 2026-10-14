"""多 agent 调度：由一个本地「决策模型」把请求拆成任务、指派执行者、并维护共享进度。

这是把「单 agent 架构」升级成「多 agent 架构」的核心：

    单 agent      一个模型从头干到尾
    多 agent      调度模型先把请求拆成若干【语义独立】的任务，各派给最合适的 agent，
                  任务在 agent 之间接力流转；调度方维护一份共享进度（做过什么、
                  产出在哪、需要什么能力），后面的 agent 接着干而不是从头开始。

三件事由这一层负责：

    1. 拆解   判断一句话里是不是压了几件互不相关的事（代码改动 + 文案规划 = 两个任务）
    2. 指派   每个任务要什么能力 → 交给哪个 agent（模型）；必要时调度方自己做翻译桥接
    3. 交接   把前置任务的「进度 + 产出文件 + 注意事项」整理成简报递给下一个 agent

拆解与指派优先交给本地决策模型完成（见 config 的 agent.dispatcher.model）；
模型不可用或输出无法解析时，自动回退到 planner 的规则拆解——保证「没有调度模型也能干活」。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field

from .config import Config
from .planner import KIND_DIM, KIND_LABEL, kind_of, resolve_kind, short_title, split_task

# 决策模型不该“脑补”出候选里没有的模型名，所以提示词里给白名单并要求留空表示交给系统。
DISPATCH_SYSTEM = """你是 AgentForge 的任务调度器（Dispatcher）。你的职责是把用户请求拆成若干
【语义独立】的任务，并为每个任务指出所需能力与最合适的执行者。

拆解原则：
- 先问一句：这几件事**能各自独立交付**吗？只有能各自独立交付、互不相关的事才拆成多个任务。
- **同一件事的执行步骤绝不要拆**：读取 / 查看 / 搜索 / 分析 / 定位 都属于「执行步骤」，
  不是独立任务，必须并进它服务的那个任务里。
  例：「先读一下 main.py，再把里面的登录函数改掉」= **1 个任务**（读只是改的前置步骤）；
      「改一下这个函数，再帮我写一段发布说明」= 2 个任务（代码 / 文案，各自能交付）。
- **宁可少拆**：拿不准合成就合成一个任务。拆多了比拆少了更糟。
- **绝不缩小任务范围**：用户明确要求的产物（文件名、路径、格式、写入还是回复里给出）
  必须**原样**出现在对应任务的 goal 里。不许把「通读素材并覆盖写入 outline.md」
  简化成「分析素材并给结论」——那等于偷偷把交付物删了，执行者是照 goal 干活的，
  goal 里没写的它不会做。
- 每个任务都必须自包含、可被独立执行；不要把「然后」「接着」这类连续的同一件事硬拆开。
- 用户任务里写明的验证命令（如「验证：python tools/x.py」）要**原样保留**在对应任务
  的 goal 里——系统会在任务收尾时亲自运行它核对结果，删掉它等于拆掉验收。
- depends_on **只在「后一个任务必须拿到前一个任务的产出文本或文件才能开始」时才填**。
  只是主题相关、措辞上提到对方，都不算依赖——那种情况留空，让它们并行跑。
  例：「统计日志里的错误数」与「给客户写周报」互不依赖，两条都留空（各写各的，可以同时做）；
      「先算出统计结果，再把这份结果写进周报」才需要第 2 个任务依赖第 1 个。
- 能并行的任务 depends_on 留空；拿不准是否需要依赖时，**留空**。

对每个任务给出：
- goal        自包含的执行指令（执行者只看这一条就能干活）
- kind        coding / inspect / writing / math / analysis / general
              （inspect = 要先看现场才知道答案的活：列目录、看有哪些文件、读现有实现）
- capability  完成任务所需的关键能力（一句话）
- needs_chinese  产出是否必须是中文（true/false）
- model       只在某个候选模型**明显更合适**时才填它的名字；不确定就留空（由系统按能力挑）。
              **不要因为你自己是某个模型就把任务都派给自己**——留空时系统会挑该维度最强的。
              **你自己（调度器）体量小，只负责拆解、指派、交接和几句简单对话**；凡是要调工具、
              读写字、写代码、做数学的任务，一律留空交给系统挑最强的执行模型，绝不能自己上。
- depends_on  依赖的任务 id 列表（没有就空数组）
- handoff     给执行者的交接说明（前置产出、注意点、已有结论）

候选模型（只能从这些名字里选）：
{catalog}

只输出一个 JSON 对象，不要任何解释与代码块围栏：
{{"tasks":[{{"id":"t1","goal":"...","kind":"coding","capability":"...","needs_chinese":true,"model":"","depends_on":[],"handoff":"..."}}],"rationale":"一句话说明为什么这样拆"}}
"""

TRANSLATE_SYSTEM = "你是翻译器。把用户给出的内容准确翻译成{target}，只输出译文本身，不要任何解释、前缀或代码块。"

SYNTH_SYSTEM = """你是 AgentForge 的主协调者。下面是一个总任务被拆成多个子任务、分别由不同 agent
执行后的结果清单。请把它们整合成一份给用户的中文答复：
- 只保留用户真正关心的结论与产出（改了哪些文件、得到什么结果），不要复述过程；
- 子任务之间有先后关系的，按最终状态叙述，不要罗列中间态；
- 不要编造清单里没有的事实；
- 【语言】即使子任务结果、工具输出或摘要里有英文，答复也必须全部用简体中文
（用户长期偏好如此，专有名词和代码除外）。"""


# 调度模型点名的执行模型，在该任务维度上比场上最强者低这么多分时就不采纳。
# 小决策模型爱把自己派给所有任务，这条阈值挡住「一律用我」的退化指派。
MODEL_CHOICE_GAP = 8

# 决策模型（体量小）只该干两件事：调度、以及几句简单对话。但凡要动工具、读写文件、
# 写代码、做数学的活都不给它——否则真正的强模型闲着，活却被小模型做砸。
# 除了 coding/math 这类显然的重活，写作/分析/检视默认也要动手（读素材、读现场、
# 写文件），同样算重活。
NOT_SIMPLE_KINDS = {"coding", "math", "writing", "analysis", "inspect"}

# 即便是 general（闲聊/解释）类，出现下面任一信号也说明「要动手或要算力」，不算简单对话。
_TOOL_HINTS = (
    "文件", "目录", "文件夹", "路径", "写入", "创建", "新建", "删除", "重命名", "覆盖",
    "修改", "编辑", "重构", "运行", "执行", "命令", "脚本", "安装", "部署", "提交", "推送",
    "拉取", "克隆", "编译", "调试", "报错", "异常", "测试", "搜索", "查找", "抓取", "下载",
    "爬取", "接口", "数据库", "表格", "文档", "技能", "工具", "代码", "函数", "算法",
    # 检索/取数类：这类词意味着「结论要有外部来源」，是重活而不是闲聊。
    # 实测漏过：「检索一下 GitHub 上叫 ERA 的项目」——一个信号词都没命中，
    # 只因为短（31 字）就被判成简单对话，工具体系整个不给它。
    "检索", "查询", "查一下", "搜一下", "调研", "评估", "核实", "取证", "比对", "对比",
    "一手来源", "官方", "仓库", "github", "api",
    ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".md", ".txt", ".csv", ".html",
    "read_file", "write_file", "edit_file", "list_dir", "run_command", "find_files",
    "skill", "tools", "file", "command", "script", "install", "deploy",
)

# 简单对话的单向上限：**只用来否决「太长」，绝不用来推断「短就是简单」**。
# 「问题简短 ≠ 问题简单」——哥德巴赫猜想只有六个字。旧版把它当主判据，
# 于是「问得短」被当成「不需要本事」；而篇幅本来就不是能力的证据，
# 判据应该是「性质 general + 目标里没有任何要动手/要外部事实的信号」（见上表）。
# 这个上限只保一件事：长目标不交给决策模型（它指令跟随弱，长活容易做砸）。
SIMPLE_CHAT_HARD_CHARS = 120


def is_simple_chat(t: "TaskSpec") -> bool:
    """这个任务是不是「不涉及工具/代码/外部事实」的简单对话（可以交给决策模型做）。

    两条判据：性质是 general，且目标里没有任何「要动手 / 要取外部事实」的信号。
    篇幅只做单向否决（超过上限一律不给决策模型），不参与「简单」的推断。
    """
    if t.kind != "general":
        return False
    goal = (t.goal or "").strip()
    if not goal or len(goal) > SIMPLE_CHAT_HARD_CHARS:
        return False
    low = goal.lower()
    return not any(h in low for h in _TOOL_HINTS)


@dataclass
class TaskSpec:
    """一个待执行的 agent 任务。"""
    id: str
    goal: str
    kind: str = "general"
    capability: str = ""
    needs_chinese: bool = True
    model: str = ""            # 调度模型点名的模型名（可为空）
    depends_on: list[str] = field(default_factory=list)
    handoff: str = ""
    # 执行模型是否被系统改写过（决策模型点了自己、或点的模型明显偏弱时会被换掉）。
    # 前端据此把「为什么换了模型」明确播报出来，而不是悄悄换掉。
    reassigned: bool = False

    # 运行期填入（由执行层按能力/可用性解析出的具体模型）
    index: int = 0
    model_key: str = ""
    model_name: str = ""
    model_id: str = ""
    score: int = 0
    reason: str = ""
    title: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "title": self.title or short_title(self.goal),
            "detail": self.goal,
            "kind": self.kind,
            "kind_label": KIND_LABEL.get(self.kind, self.kind),
            "capability": self.capability,
            "needs_chinese": self.needs_chinese,
            "depends_on": list(self.depends_on),
            "handoff": self.handoff,
            "model_key": self.model_key,
            "model_name": self.model_name,
            "model_id": self.model_id,
            "score": self.score,
            "reason": self.reason,
            "reassigned": self.reassigned,
            "role": f"{KIND_LABEL.get(self.kind, self.kind)} agent",
        }


@dataclass
class DispatchPlan:
    tasks: list[TaskSpec]
    rationale: str = ""
    source: str = "rules"          # llm | rules
    dispatcher_name: str = ""

    def summary(self) -> str:
        used = []
        for t in self.tasks:
            if t.model_name and t.model_name not in used:
                used.append(t.model_name)
        if not self.tasks:
            return ""
        return f"{len(self.tasks)} 个任务 → {len(used)} 个 agent（{'、'.join(used)}）"


@dataclass
class TaskResult:
    task_id: str
    title: str
    status: str = "pending"        # pending | running | done | error
    summary: str = ""
    files: list[str] = field(default_factory=list)


class SharedState:
    """多 agent 共享的任务状态（黑板）。

    调度方与各 agent 都读写它：agent 完成后写入摘要与产出文件，下一步的 agent
    通过 brief() 拿到「前面做了什么、产出在哪」，从而不必从头开始。
    """

    def __init__(self, message: str):
        self.message = message
        self.results: dict[str, TaskResult] = {}
        # 并行执行时多个 agent 线程会同时读写，所有访问都在锁内。
        self._lock = threading.Lock()

    def register(self, tasks: list[TaskSpec]) -> None:
        with self._lock:
            for t in tasks:
                self.results[t.id] = TaskResult(task_id=t.id, title=t.title or short_title(t.goal))

    def start(self, task_id: str) -> None:
        with self._lock:
            if task_id in self.results:
                self.results[task_id].status = "running"

    def finish(self, task_id: str, summary: str, files: list[str] | None = None,
               status: str = "done") -> None:
        with self._lock:
            r = self.results.setdefault(task_id, TaskResult(task_id=task_id, title=task_id))
            r.status = status
            r.summary = (summary or "").strip()
            if files:
                for f in files:
                    if f and f not in r.files:
                        r.files.append(f)

    def brief(self, task: TaskSpec) -> str:
        """给某个任务生成「前置进度简报」。"""
        with self._lock:
            rows: list[str] = []
            for dep in task.depends_on:
                r = self.results.get(dep)
                if not r:
                    continue
                line = f"- [{r.title}] {r.summary or '（已完成）'}"
                if r.files:
                    line += "\n    产出文件：" + "、".join(r.files[:8])
                rows.append(line)
            # 没有显式依赖时，把已完成的任务也简要带上（接力语义：默认继承前序成果）
            if not rows:
                for r in self.results.values():
                    if r.status == "done" and r.summary:
                        line = f"- [{r.title}] {r.summary}"
                        if r.files:
                            line += "\n    产出文件：" + "、".join(r.files[:8])
                        rows.append(line)
        return "\n".join(rows) if rows else "（无前置任务，从头开始）"

    def result_items(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(r.title, r.summary) for r in self.results.values()]


def _catalog_text(entries: list[dict]) -> str:
    """把候选模型压成提示词里的一行行白名单（名字 + 各维度分）。"""
    lines = []
    for e in entries[:20]:
        sc = e.get("scores", {}) or {}
        dims = " ".join(
            f"{k}={sc.get(k, 0)}" for k in ("coding", "reasoning", "writing", "chinese", "math")
        )
        lines.append(f"- {e.get('name')}（{dims}）")
    return "\n".join(lines) if lines else "（无）"


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏、前后废话、字符串里的花括号）。

    小模型偶尔会把 JSON 包在代码块里、或在后面再多说几句。用「从第一个 { 开始做
    括号配平」的扫描来截取，比正则可靠：既不会被解释文字里的括号带偏，也能容忍
    对象内部字符串里的 { }。
    """
    if not text:
        return None
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start : i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


class Dispatcher:
    """决策模型的封装：拆解 / 指派 / 翻译桥接 / 汇总。

    决策模型本身也是一个普通模型（默认本地 Qwen3-32B），走 ClientHub 调用——
    所以换成任何本地或在线模型都行，只要能稳定产出 JSON。
    """

    def __init__(self, cfg: Config, hub, store):
        self.cfg = cfg
        self.hub = hub
        self.store = store

    # ---------------------------------------------------------- 决策模型
    def spec(self):
        key = (getattr(self.cfg, "dispatcher_model", "") or "").strip()
        if not key:
            return None
        spec = self.cfg.models.get(key)
        if spec is None:
            for s in self.cfg.models.values():
                if s.name == key:
                    spec = s
                    break
        return spec

    def available(self) -> bool:
        spec = self.spec()
        return bool(spec is not None and getattr(spec, "available", False) and self.hub)

    def name(self) -> str:
        spec = self.spec()
        return spec.display if spec else ""

    def _chat(self, system: str, user: str, num_predict: int = 2048,
              temperature: float = 0.2) -> str:
        spec = self.spec()
        if spec is None:
            return ""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        res = self.hub.chat(
            spec, messages, tools=None,
            options={"temperature": temperature, "num_predict": num_predict},
            think=False,
        )
        return (res.content or "").strip()

    # ---------------------------------------------------------- 拆解 / 指派
    def plan(self, message: str, entries: list[dict]) -> DispatchPlan:
        """产出执行计划：优先让决策模型拆，失败则退回规则拆解。"""
        if self.available():
            p = self._llm_plan(message, entries)
            if p is not None:
                return p
        return self._rules_plan(message, entries)

    def _llm_plan(self, message: str, entries: list[dict]) -> DispatchPlan | None:
        max_tasks = int(getattr(self.cfg, "dispatcher_max_tasks", 6) or 6)
        system = DISPATCH_SYSTEM.format(catalog=_catalog_text(entries))
        try:
            raw = self._chat(system, message, num_predict=2048, temperature=0.2)
        except Exception:  # noqa: BLE001  决策模型挂了不该拖垮整个任务
            return None
        obj = _extract_json(raw)
        if not obj or not isinstance(obj.get("tasks"), list):
            return None

        valid_names = {e.get("name") for e in entries}
        tasks: list[TaskSpec] = []
        for i, item in enumerate(obj["tasks"][:max_tasks]):
            if not isinstance(item, dict):
                continue
            goal = str(item.get("goal") or "").strip()
            if not goal:
                continue
            # 调度模型给的 kind 要过一遍规则复核（见 planner.resolve_kind）：
            # 它把「修改思维链输出机制…总结成一句话」这条改代码的活标成了 writing，
            # 于是按写作维度挑模型，模型就去"写一段说明""记下您的偏好"，一个文件没碰。
            kind = resolve_kind(goal, item.get("kind"))
            model = str(item.get("model") or "").strip()
            if model not in valid_names:
                model = ""                    # 模型瞎报名字时忽略，交给系统按能力挑
            deps = item.get("depends_on") or []
            if not isinstance(deps, list):
                deps = []
            tasks.append(TaskSpec(
                id=str(item.get("id") or f"t{i + 1}"),
                goal=goal,
                kind=kind,
                capability=str(item.get("capability") or "").strip(),
                needs_chinese=bool(item.get("needs_chinese", True)),
                model=model,
                depends_on=[str(d) for d in deps],
                handoff=str(item.get("handoff") or "").strip(),
                # 决策模型愿意给短标题就用它的；没给则 _resolve 用 short_title 兜底，
                # 不把「标题好不好」押在模型身上。
                title=str(item.get("title") or "").strip(),
            ))
        if not tasks:
            return None
        self._resolve(tasks, entries)
        return DispatchPlan(
            tasks=tasks,
            rationale=str(obj.get("rationale") or "").strip(),
            source="llm",
            dispatcher_name=self.name(),
        )

    def _rules_plan(self, message: str, entries: list[dict]) -> DispatchPlan:
        """回退路径：复用 planner 的规则拆解，把片段串成有依赖链的任务。"""
        parts = [p for p in split_task(message) if p.strip()]
        if not parts:
            parts = [message]
        tasks: list[TaskSpec] = []
        for i, part in enumerate(parts):
            tasks.append(TaskSpec(
                id=f"t{i + 1}",
                goal=part,
                kind=kind_of(part),
                depends_on=[f"t{i}"] if i > 0 else [],   # 串成接力链，保持既有语义
            ))
        self._resolve(tasks, entries)
        return DispatchPlan(tasks=tasks, rationale="规则拆解（决策模型不可用时的回退）",
                            source="rules", dispatcher_name=self.name())

    # ---------------------------------------------------------- 指派
    def _resolve(self, tasks: list[TaskSpec], entries: list[dict]) -> None:
        """为每个任务确定执行 agent（模型）：优先用调度模型点名的，否则按能力挑最强。

        注意：**不改动任务自身的 kind**——那是调度模型（或规则回退）的判断，
        这里只按该 kind 对应的维度去挑模型（用 planner 的分类重新定 kind 会把
        「写一段说明」这类误判成编码，反而覆盖掉调度模型的正确指派）。

        「点名优先」不是无条件的，有两道闸：
        1. 决策模型只接「简单对话」，重活（工具/代码/数学/写作/分析）一律不让它当执行者；
        2. 点名的模型在该维度上明显弱于场上最强者（差 ≥ MODEL_CHOICE_GAP 分）时改用最强者。
        两种情况都把原因如实写进 reason，界面上能看出为什么换了模型。
        """
        by_name = {e.get("name"): e for e in entries}
        disp_name = self.name()

        def dim_of(e: dict, dim: str) -> int:
            return int((e.get("scores") or {}).get(dim, 0))

        for i, t in enumerate(tasks):
            t.index = i + 1
            # 短标题走 short_title（边界/括号感知），不按字数硬切——见
            # planner.short_title 的说明。决策模型给了 title 就尊重它的。
            t.title = t.title or short_title(t.goal)
            dim = KIND_DIM.get(t.kind, "reasoning")
            # 决策模型能不能接这一单：默认只允许它做简单对话
            allow_disp = (bool(getattr(self.cfg, "dispatcher_execute_simple", True))
                          and is_simple_chat(t))
            cands = entries
            if disp_name and not allow_disp:
                without_disp = [e for e in entries if e.get("name") != disp_name]
                if without_disp:                    # 一个都没剩时只能让它上
                    cands = without_disp
            excluded_disp = cands is not entries
            best = (max(cands, key=lambda e: (dim_of(e, dim), e.get("total", 0)))
                    if cands else None)
            kind_label = KIND_LABEL.get(t.kind, t.kind)
            disp_entry = by_name.get(disp_name) if disp_name else None
            picked = by_name.get(t.model) if t.model else None
            if picked is not None and picked not in cands:
                picked = None                       # 点名的正是被排除的决策模型
            reason = ""
            if allow_disp and disp_entry is not None and disp_entry in cands:
                # 简单对话本来就是决策模型的份内事：它已经加载着，不必为此唤醒执行模型
                picked = disp_entry
                reason = "简单对话：由决策模型直接处理（不必为此唤醒执行模型）"
            elif picked is not None and best is not None and picked is not best:
                gap = dim_of(best, dim) - dim_of(picked, dim)
                if gap >= MODEL_CHOICE_GAP:
                    reason = (f"调度模型点名的 {picked.get('name')} 在{kind_label}维度"
                              f"弱 {gap} 分，改用最强者（{dim_of(best, dim)} 分）")
                    picked = best
                    t.reassigned = True
                else:
                    reason = f"调度模型指定（{t.capability or kind_label}）"
            elif picked is not None:
                reason = f"调度模型指定（{t.capability or kind_label}）"
            elif best is not None:
                if excluded_disp:
                    reason = (f"决策模型不接这类要动手/要算力的任务，改派 {best.get('name')}"
                              f"（{kind_label}维度 {dim_of(best, dim)} 分）")
                    t.reassigned = True
                else:
                    reason = f"{kind_label}维度最强（{dim_of(best, dim)} 分）"
                picked = best
            t.reason = reason
            if picked:
                t.score = dim_of(picked, dim)
                t.model_key = picked.get("key", "")
                t.model_name = picked.get("name", "")
                t.model_id = picked.get("id", "")

    # ---------------------------------------------------------- 能力桥接
    def translate(self, text: str, target: str = "English") -> str:
        """把内容翻译成目标语言（给不擅长中文的模型做预处理时用）。"""
        if not self.available() or not (text or "").strip():
            return text
        try:
            out = self._chat(
                TRANSLATE_SYSTEM.format(target=target), text,
                num_predict=max(512, min(4096, len(text))), temperature=0.1,
            )
        except Exception:  # noqa: BLE001
            return text
        return out or text

    # ---------------------------------------------------------- 收尾汇总
    def synthesize(self, message: str, items: list[tuple[str, str]]) -> str:
        """把各 agent 的结果整合成一份给用户的答复（决策模型完成）。"""
        if not self.available() or not items:
            return ""
        listing = "\n".join(f"[{title}] {summary}" for title, summary in items if summary)
        if not listing.strip():
            return ""
        user = f"【总任务】\n{message}\n\n【各 agent 的结果】\n{listing}"
        try:
            return self._chat(SYNTH_SYSTEM, user, num_predict=3072, temperature=0.3)
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["Dispatcher", "DispatchPlan", "TaskSpec", "SharedState", "TaskResult"]
