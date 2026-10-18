"""模型指派与 Leader 通信层（第 47 步后瘦身）。

一次性「拆完再跑」的规划机器（plan/_llm_plan/_rules_plan/goal 保真/SharedState
黑板/synthesize）已由 agentforge/leader.py 的 Leader 双环取代并退役。本模块保留：

    1. TaskSpec        一批小步骤的载体（事件负载与前端计划卡共用它的 as_dict）
    2. is_simple_chat  决策模型只接简单对话的判定
    3. Dispatcher      与决策模型对话（_chat）、指派（_resolve：防自派/防重活）、
                       能力桥接翻译（translate）
    4. _catalog_text / _extract_json   候选模型目录与 JSON 提取的共用工具
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .config import Config
from .planner import KIND_DIM, KIND_LABEL, short_title

TRANSLATE_SYSTEM = "你是翻译器。把用户给出的内容准确翻译成{target}，只输出译文本身，不要任何解释、前缀或代码块。"



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


# ---------------------------------------------------------------------------
# goal 保真核查（收口「绝不缩小任务范围」——全链路唯一「拆错=交付错」的条款）
#
# 调度器把「通读素材并覆盖写入 outline.md」拆成「分析素材并给结论」，执行者照
# 缩水后的 goal 干活，交付物凭空消失——拆分质量没有架构校验，这里补上：
# 程序比对 + 一轮修正（重拆一次）+ 仍缺则程序化补进 goal。
# 宁漏勿误：只认三类**点名的**交付物——文件名、路径、引号内容；动词短语
# （「写个报告」）不认，避免 corrective 重拆被泛词空转。
# ---------------------------------------------------------------------------

# 文件 / 路径 token：带可识别扩展名（含 Windows 反斜杠路径与带目录前缀的相对路径）。
_FILE_TOK_RX = re.compile(
    r"[\w\u4e00-\u9fff.\-/\\]+\.(?:txt|md|markdown|py|js|ts|jsx|tsx|json|ya?ml|"
    r"html?|css|csv|log|xml|ini|cfg|toml|bat|sh|ps1|xlsx?|docx?|pptx?|pdf|sql|env)\b",
    re.I,
)
# 引号内容：用户特意加引号点名的东西（「结论先行」），丢了同样是缩水。
_QUOTE_TOK_RX = re.compile(r"[「『\"']([^「』\"'\n]{2,40})[」』\"']")


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

    def _chat(self, system: str, user: str, num_predict: int = 8192,
              temperature: float = 0.2) -> str:
        # num_predict 8192（原 2048）+ think=True（原 False）：调度模型换成
        # gpt-oss:20b 后暴露的两个问题——
        #   ① 2048 被它的 analysis（思考）通道吃掉大半：思考 token 与正文共享生成
        #      预算，实测规划 JSON 只吐出 428 字符就在中间被截断，_extract_json
        #      必然失败，Leader 每轮退回兜底（rerun8 实测 8 连败、12 轮全烧在
        #      兜底循环上）。
        #   ② think=False 更糟：Ollama 对 gpt-oss 并不真正关闭思考——analysis
        #      照常生成但不计入 message.content（reasoning 字段也是空的，纯烧掉），
        #      且提前触发 done_reason=length。think=True 时 Ollama 走正常 harmony
        #      通道，final 才是正文，预算也真实生效（实测 done_reason=stop）。
        # gemma 作为调度模型时 think=True 本来就是它的推荐配置（false 会把推理
        # 混进正文），两任调度模型统一 true。
        spec = self.spec()
        if spec is None:
            return ""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # 空响应重试：gpt-oss 偶发「analysis 生成完毕但 final 通道为空」的空回答
        # （rerun8 观测到过）。同一 prompt 重发一次通常就好了，空结果让 Leader
        # 整轮退回兜底的代价远大于这一次重试。
        for attempt in range(2):
            res = self.hub.chat(
                spec, messages, tools=None,
                options={"temperature": temperature, "num_predict": num_predict},
                think=True,
            )
            out = (res.content or "").strip()
            if out:
                return out
        return ""

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
            # 决策模型能不能接这一单：默认只允许它做简单对话；开启 leader_as_worker 后，
            # 它作为「常驻员工」也进入可分配名单，与其它模型按维度比拼分数（规划完顺手干重活）。
            allow_disp = ((bool(getattr(self.cfg, "dispatcher_execute_simple", True))
                          and is_simple_chat(t))
                         or bool(getattr(self.cfg, "leader_as_worker", False)))
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


__all__ = ["Dispatcher", "TaskSpec"]
