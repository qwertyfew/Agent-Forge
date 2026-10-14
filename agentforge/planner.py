"""任务拆解与模型分配：把一个复合任务切成子任务，各交给最合适的那一个模型。

为什么要拆：一个模型池里有擅长编码的、擅长写作的、擅长推理的。复杂的用户请求
往往同时包含「先分析现有代码 → 再改实现 → 最后写一段说明」这类多性质步骤，
让同一个模型硬扛所有步骤，等于放弃池子里其它模型的长处。

流程三步，全部走规则，不额外调用模型（省 token、不引入不确定性）：

    1. 拆解   按连接词（然后 / 接着 / 最后…）与编号切分子任务
    2. 定性   按信号词判断每个子任务属于 编码 / 检视 / 写作 / 数学 / 分析 / 通用
    3. 分配   在该性质对应的评分维度上，挑池子里得分最高的模型

只做「明显需要拆」的拆解：单个动作的请求不会被切碎，避免把简单任务复杂化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 性质判定

KIND_LABEL = {
    "coding": "编码",
    "inspect": "检视",
    "writing": "写作",
    "math": "数学",
    "analysis": "分析",
    "general": "通用",
}

# 子任务性质 → 评分维度（分配模型时看哪个维度）
KIND_DIM = {
    "coding": "coding",
    "inspect": "reasoning",
    "writing": "writing",
    "math": "math",
    "analysis": "reasoning",
    "general": "reasoning",
}

KIND_SIGNALS: dict[str, list[str]] = {
    "coding": [
        r"代码", r"编码", r"函数", r"类\b", r"重构", r"实现", r"写个", r"写一段", r"脚本",
        r"bug", r"报错", r"异常", r"调试", r"修复", r"测试", r"编译", r"运行",
        r"\.py\b", r"\.js\b", r"\.ts\b", r"\.java\b", r"\.go\b", r"\.rs\b", r"\.html\b",
        r"\.css\b", r"\.sql\b", r"正则", r"接口", r"api\b", r"数据库", r"部署",
        r"code", r"function", r"refactor", r"implement", r"debug", r"fix",
    ],
    # 「先去看一眼现场」类：要读现有目录 / 文件 / 实现才能答。
    # 单独成一类是因为它选模型时该看**推理**而不是**文风**——实测教训：
    # 「先列出工作区根目录下的文件，再用一句话说明你都做了什么」被"说明"判成了写作，
    # 交给一个擅长长文的模型，它没去读，直接编了一份目录清单。
    "inspect": [
        r"列出", r"列一下", r"罗列", r"有哪些", r"哪些文件", r"文件列表", r"目录列表",
        r"目录结构", r"项目结构", r"文件结构", r"文件夹结构", r"目录下", r"文件夹?里",
        r"工作区", r"根目录", r"项目里", r"仓库里", r"里面有什么", r"包含哪些",
        r"看一下", r"看看", r"查看", r"查阅", r"读一下", r"读读", r"打开", r"显示",
        r"现状", r"当前实现", r"现有的", r"已有的",
        r"list", r"look at", r"show me", r"inspect",
    ],
    "writing": [
        r"写", r"撰写", r"起草", r"文案", r"文档", r"说明", r"注释", r"总结", r"摘要",
        r"大纲", r"报告", r"周报", r"邮件", r"翻译", r"润色", r"改写", r"起名",
        r"README", r"介绍", r"write", r"draft", r"summary", r"translate",
    ],
    "math": [
        r"计算", r"推导", r"证明", r"方程", r"概率", r"矩阵", r"积分", r"数学",
        r"统计", r"算法复杂度", r"排序", r"动态规划", r"求值", r"求解",
    ],
    "analysis": [
        r"分析", r"梳理", r"调研", r"评估", r"对比", r"比较", r"排查", r"诊断",
        r"定位", r"规划", r"设计", r"架构", r"方案", r"拆解", r"检查",
        r"analy", r"review", r"diagnose", r"design", r"plan",
    ],
}

# ------------------------------------------------- 「这是在让动手改东西吗」
#
# kind_of 的老优先级「编码优先于写作」只认「代码/函数/bug/脚本」这类**成品名**，
# 认不出「改动物件 + 改动动作」这种**说法**。实测事故：用户说「每当思维链打印完就
# 把它隐藏，并把结果总结成一句话」——这是要改 `web/index.html` 的渲染，只因为句子里
# 有一个"总结"，整条被判成**写作**；按写作维度选的模型于是去"写一段说明"、
# "记下您的偏好"，一个文件都没碰（用户原话：两个指令都不执行）。
#
# 判据刻意只看**搭配**、不猜意图：改动物件与改动动作出现在同一个小句里即为真。
_CHANGE_OBJ = (
    r"(?:代码|源码|文件|脚本|函数|方法|模块|组件|插件|接口|路由|配置|参数|"
    r"页面|界面|前端|样式|布局|渲染|输出|显示|逻辑|机制|行为|流程|按钮|卡片|"
    r"思维链|思考过程|推理过程|文案|提示词|系统提示|记忆|工具链|"
    r"bug|缺陷|报错|异常|故障|"
    r"\.py\b|\.js\b|\.ts\b|\.html\b|\.css\b|\.json\b|\.ya?ml\b|\.vue\b|\.tsx\b)"
)
_CHANGE_ACT = (
    r"(?:修改|改成|改为|改一下|改掉|改动|调整|替换|换成|新增|添加|加上|补上|"
    r"去掉|移除|删掉|删除|隐藏|收起|折叠|关闭|关掉|启用|停用|禁用|接入|接上|"
    r"抽出来|抽成|收口|统一|重写|修复|修好|渲染|展示|输出)"
)
_CHANGE_RX = re.compile(
    rf"(?:{_CHANGE_ACT}[^。；;\n]{{0,12}}?{_CHANGE_OBJ})"
    rf"|(?:{_CHANGE_OBJ}[^。；;\n]{{0,12}}?{_CHANGE_ACT})",
    re.I,
)


def looks_like_change(text: str) -> bool:
    """这句话是不是在要求「动手改 / 加 / 去掉某样东西」。

    只判一件事：把「改代码的活」从写作怀里抢回来。它**不是**「要不要动手」的唯一
    判据——「把结论写进报告」不算（报告是产出物，不是改动物件），那是写作任务。
    """
    return bool(_CHANGE_RX.search(str(text or "")))


# 连接词切分：这些词出现在句中，通常意味着"一件事做完了接着做下一件"
SPLIT_RX = re.compile(
    r"(?:然后|接着|再然后|紧接着|随后|之后|最后|其次|另外|并且|同时|以及|再帮我|再帮我做|再)"
    r"|(?:\n\s*(?:[-*•]|\d+[.、)）])\s*)"
)

# 编号列表（1. / 一、）整体优先按编号切
NUMBER_RX = re.compile(r"(?m)^\s*(?:\d+[.、)）]|[一二三四五六七八九十]+[、.)）])\s*")

MIN_PART_CHARS = 6      # 短于这个长度的碎片不单独成任务
MAX_PARTS = 4           # 最多切几段，避免把一个任务拆得过碎


@dataclass
class Subtask:
    index: int
    title: str
    detail: str
    kind: str
    model_key: str = ""
    model_name: str = ""
    model_id: str = ""
    score: int = 0
    reason: str = ""
    keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "detail": self.detail,
            "kind": self.kind,
            "kind_label": KIND_LABEL.get(self.kind, self.kind),
            "model_key": self.model_key,
            "model_name": self.model_name,
            "model_id": self.model_id,
            "score": self.score,
            "reason": self.reason,
        }


# ---------------------------------------------------------------- 拆解

def _clean(part: str) -> str:
    return re.sub(r"[\s,，。;；]+$", "", re.sub(r"^[\s,，。;；、]+", "", part or "")).strip()


def split_task(text: str) -> list[str]:
    """把用户请求切成子任务片段。切不动就原样返回一段。"""
    text = (text or "").strip()
    if not text:
        return []

    # 1) 明显的编号列表优先
    numbered = [p for p in NUMBER_RX.split(text) if p is not None]
    parts = [_clean(p) for p in numbered]
    parts = [p for p in parts if len(p) >= MIN_PART_CHARS]

    # 2) 没有编号就用连接词切
    if len(parts) < 2:
        parts = [_clean(p) for p in SPLIT_RX.split(text)]
        parts = [p for p in parts if len(p) >= MIN_PART_CHARS]

    # 3) 还是切不开：看是不是多行
    if len(parts) < 2:
        lines = [_clean(l) for l in text.split("\n")]
        lines = [l for l in lines if len(l) >= MIN_PART_CHARS]
        if len(lines) >= 2:
            parts = lines

    if not parts:
        return [text]

    # 段数过多时，把尾部合并进最后一段，避免把任务拆得太碎
    if len(parts) > MAX_PARTS:
        head = parts[: MAX_PARTS - 1]
        tail = "；".join(parts[MAX_PARTS - 1:])
        parts = head + [tail]
    return parts


def kind_of(text: str) -> str:
    """判断一个片段属于哪种性质，取信号命中数最多的那一类。

    三条刻意写死的优先级，都是实测误判逼出来的：

    1. **编码优先于写作**：写作信号里的「写」「说明」「介绍」太泛，几乎任何请求都会
       命中一两个；而「写个脚本」里的那个"写"是"实现"，不是"写文章"。
    2. **改动物件 + 改动动作 → 编码**（哪怕信号词只命中了"总结"）：写作信号的
       「写/总结/说明/文档」描述的是**产出物是文本**，可「把思维链隐藏、结果总结成
       一句话」说的是**要改的那段渲染**——产品是代码，不是文章。见 looks_like_change。
    3. **检视优先于写作/通用**：要读现场的任务，读不到就无从写起，所以先按「检视」定性。
       实测：「先列出工作区根目录下的文件（不要读内容），然后用一句话说明你都做了什么」
       原本会被"说明"判成写作 → 交给擅长长文的模型 → 它没去读、直接编了份目录清单。
    """
    scores = {k: 0 for k in KIND_SIGNALS}
    text = str(text or "")          # None / 非字符串一律当空文本，别在定型这一步炸掉
    for kind, patterns in KIND_SIGNALS.items():
        for p in patterns:
            if re.search(p, text, re.I):
                scores[kind] += 1
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "general"
    if scores["coding"] > 0 and best == "writing" and scores["coding"] >= scores["writing"] - 1:
        return "coding"
    if best == "writing" and looks_like_change(text):
        return "coding"
    if (scores["inspect"] > 0 and best in ("writing", "general")
            and scores["inspect"] >= scores[best] - 1):
        return "inspect"
    return best


def resolve_kind(text: str, proposed: str = "") -> str:
    """定一个片段的性质；**上游（调度模型）给的标签要过一遍规则复核**。

    proposed 是调度模型给的 kind，可空。刻意不是无条件采信——实测事故：
    「修改思维链输出机制…把结果总结成一句话」这条**要改前端渲染**的任务书被调度
    模型标成 writing（因为句子里有"总结"），于是按写作维度挑模型；模型于是去
    "写一段说明""记下您的偏好"，一个文件都没碰。规则复核认得出「改动物件 + 改动
    动作」，一律纠回 coding。

    「分析」不纠：分析任务本就该走推理维度；「分析并修改…」这类会由 kind_of 的
    计数规则自己落到 coding。
    """
    k = str(proposed or "").strip().lower()
    if k not in KIND_LABEL:
        return kind_of(text)
    if k == "writing" and looks_like_change(text):
        return "coding"
    return k


# ------------------------------------------------- 失败后果分档（swe-router 思路）

# 路由的第二只眼：不看「任务像什么」，看「做错了会怎样」。
# 同样是"写一段话"，随手草稿和发给客户的合同，对模型质量的要求完全不同。
# 高后果 → 给模型能力设高下限；低后果 → 最便宜的够用模型就行。
# 信号刻意保守：宁可不是高后果，也不要把日常任务误抬进高代价档。
STAKES_LABEL = {"low": "低风险", "medium": "常规", "high": "高后果"}

_STAKES_HIGH_RX = re.compile(
    r"生产环境|上线|正式发布|发布到|部署|数据库|删库|支付|付款|账单|合同|客户|"
    r"不可逆|不可恢复|覆盖写|系统配置|管理员权限|密钥|密码", re.I)
_STAKES_LOW_RX = re.compile(
    r"草稿|随便|试试|先看看|了解一下|练习|玩具|demo|示例|好奇|好玩|不用真的|假想|假设", re.I)


def stakes_of(text: str) -> tuple[str, str]:
    """判断任务的失败后果档位，返回 (档位, 一句话理由)。

    high 优先于 low：一句话同时命中两类信号时，按后果大的一侧兜底——
    误把高后果当低风险的代价（真删了库）远大于反过来（多花点算力）。
    """
    t = str(text or "")
    mh = _STAKES_HIGH_RX.search(t)
    if mh:
        return "high", f"命中高后果信号「{mh.group(0)}」"
    ml = _STAKES_LOW_RX.search(t)
    if ml:
        return "low", f"命中低风险信号「{ml.group(0)}」"
    return "medium", "未命中特殊信号，按常规档"


def needs_plan(text: str, pool_size: int) -> bool:
    """是否值得拆解：池子里至少两个模型，且请求确实包含多个动作。"""
    if pool_size < 2:
        return False
    return len(split_task(text)) >= 2


# ---------------------------------------------------------------- 短标题

# 子句边界：在这些字符之后收尾，读起来还是一个完整的短句。
# 分两档是因为「，、；：」只是停顿（收尾时要抹掉，否则会出现「，…」这种别扭组合），
# 而「。！？」本身就是句子的收尾，留着正好。
_SOFT_END = "，、；：,;:"
_HARD_END = "。！？!?"
_CLOSERS = "）)】」》\"'”’"
_BOUNDARY = _SOFT_END + _HARD_END + _CLOSERS
# 成对符号的左半边：硬切不许切进它们里面，否则会留下「（如 ev」这种断头括号
_OPENERS = "（(【[「『《<\"'“‘"
_WORD_CHARS = "_-./\\+"


def short_title(text: str, limit: int = 26) -> str:
    """把一句任务书压成能读的短标题（计划卡、泳道、侧栏都用它）。

    以前这里是两套写法：planner 的 `s[:limit-1] + "…"` 与 dispatcher 的
    `goal[:26]`。都只按字数硬切，于是出现「定位并修复导致在对话中返回非本次
    生成的文档（如 ev」——切在括号里面、断在半截单词上，收尾括号也没了。
    这一行是用户看任务的第一眼，不该长这样。

    依次退化，三种结果都补省略号（让人一眼看出这是被截断的，而不是任务本来
    就叫这个名字）。**返回长度保证 <= limit**（省略号算在预算里）——侧栏标签有
    硬上限，不能因为补了个省略号就超出一个字。

      ① 在 limit 之前的子句边界收尾 —— 输出是一个完整的短句；
      ② 退到 limit 之前最后一个未闭合的左括号之前 —— 不产出断头括号；
      ③ 硬切；若正切在一个西文单词 / 路径中间，先退到它的开头。
    """
    s = " ".join(str(text or "").split())
    if limit <= 1:
        return s[: max(0, limit)]
    if len(s) <= limit:
        return s
    budget = limit - 1                      # 省略号占一个位
    floor = max(1, budget * 3 // 5)         # 别为了凑边界切得太短

    # ① 子句边界（从 budget 往回找最近的一个）
    for i in range(budget - 1, floor - 1, -1):
        if s[i] in _BOUNDARY:
            head = s[: i + 1].rstrip()
            if head and head[-1] in _SOFT_END:
                head = head[:-1].rstrip()   # 「，…」读起来别扭，抹掉再补省略号
            if head:
                return head + "…"

    # ② 不切进括号里（「（如 evolution.md）」要整体退出，而不是留个断头括号）
    opener = -1
    for i, ch in enumerate(s[:budget]):
        if ch in _OPENERS:
            opener = i
    if opener > floor:
        head = s[:opener].rstrip(" \t" + _SOFT_END + _OPENERS)
        if head:
            return head + "…"

    # ③ 硬切；避开切在西文单词 / 路径中间
    cut = budget
    prev_ch, next_ch = s[cut - 1], s[cut]
    if prev_ch.isascii() and (prev_ch.isalnum() or prev_ch in _WORD_CHARS) \
            and next_ch.isascii() and (next_ch.isalnum() or next_ch in _WORD_CHARS):
        j = cut
        while j > floor and s[j - 1].isascii() and (s[j - 1].isalnum() or s[j - 1] in _WORD_CHARS):
            j -= 1
        if j > floor:
            cut = j
    return s[:cut].rstrip() + "…"


def _title_of(part: str, limit: int = 26) -> str:
    return short_title(part, limit)


# ---------------------------------------------------------------- 分配

def assign(subtask_part: str, entries: list[dict]) -> tuple[dict | None, str, int, str]:
    """给一个子任务挑模型：在该性质对应的维度上取最高分。

    entries 每项需含 key / name / scores / total。
    返回 (选中模型, 性质, 该维度得分, 理由)
    """
    kind = kind_of(subtask_part)
    dim = KIND_DIM[kind]
    if not entries:
        return None, kind, 0, "池子里没有可用模型"
    best = max(entries, key=lambda e: (e.get("scores", {}).get(dim, 0), e.get("total", 0)))
    score = int(best.get("scores", {}).get(dim, 0))
    label = KIND_LABEL.get(kind, kind)
    reason = f"{label}维度最强（{score} 分）"
    return best, kind, score, reason


def make_plan(text: str, entries: list[dict], max_parts: int = MAX_PARTS) -> list[Subtask]:
    """生成执行计划。entries 是「当前启用的模型」列表（含 scores）。"""
    parts = split_task(text)[:max_parts]
    if not parts:
        return []
    plan: list[Subtask] = []
    for i, part in enumerate(parts):
        best, kind, score, reason = assign(part, entries)
        plan.append(
            Subtask(
                index=i + 1,
                title=_title_of(part),
                detail=part,
                kind=kind,
                model_key=(best or {}).get("key", ""),
                model_name=(best or {}).get("name", ""),
                model_id=(best or {}).get("id", ""),
                score=score,
                reason=reason,
                keywords=[],
            )
        )
    return plan


def plan_summary(plan: list[Subtask]) -> str:
    if not plan:
        return ""
    used = []
    for st in plan:
        if st.model_name and st.model_name not in used:
            used.append(st.model_name)
    return f"{len(plan)} 个子任务 → {len(used)} 个模型（{'、'.join(used)}）"


__all__ = [
    "Subtask", "make_plan", "split_task", "kind_of", "resolve_kind",
    "looks_like_change", "needs_plan", "stakes_of",
    "plan_summary", "short_title", "KIND_LABEL", "KIND_DIM", "STAKES_LABEL",
]
