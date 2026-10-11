"""任务拆解与模型分配：把一个复合任务切成子任务，各交给最合适的那一个模型。

为什么要拆：一个模型池里有擅长编码的、擅长写作的、擅长推理的。复杂的用户请求
往往同时包含「先分析现有代码 → 再改实现 → 最后写一段说明」这类多性质步骤，
让同一个模型硬扛所有步骤，等于放弃池子里其它模型的长处。

流程三步，全部走规则，不额外调用模型（省 token、不引入不确定性）：

    1. 拆解   按连接词（然后 / 接着 / 最后…）与编号切分子任务
    2. 定性   按信号词判断每个子任务属于 编码 / 写作 / 数学 / 分析 / 通用
    3. 分配   在该性质对应的评分维度上，挑池子里得分最高的模型

只做「明显需要拆」的拆解：单个动作的请求不会被切碎，避免把简单任务复杂化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 性质判定

KIND_LABEL = {
    "coding": "编码",
    "writing": "写作",
    "math": "数学",
    "analysis": "分析",
    "general": "通用",
}

# 子任务性质 → 评分维度（分配模型时看哪个维度）
KIND_DIM = {
    "coding": "coding",
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
        r"定位", r"规划", r"设计", r"架构", r"方案", r"拆解", r"看看", r"检查",
        r"analy", r"review", r"diagnose", r"design", r"plan",
    ],
}

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
    """判断一个片段属于哪种性质，取信号命中数最多的那一类。"""
    scores = {k: 0 for k in KIND_SIGNALS}
    for kind, patterns in KIND_SIGNALS.items():
        for p in patterns:
            if re.search(p, text, re.I):
                scores[kind] += 1
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "general"
    # 编码 + 写作同时命中时，以编码优先（写作信号里"写"太泛）
    if scores["coding"] > 0 and best == "writing" and scores["coding"] >= scores["writing"] - 1:
        return "coding"
    return best


def needs_plan(text: str, pool_size: int) -> bool:
    """是否值得拆解：池子里至少两个模型，且请求确实包含多个动作。"""
    if pool_size < 2:
        return False
    return len(split_task(text)) >= 2


def _title_of(part: str, limit: int = 26) -> str:
    s = re.sub(r"\s+", " ", part).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


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
    "Subtask", "make_plan", "split_task", "kind_of", "needs_plan",
    "plan_summary", "KIND_LABEL", "KIND_DIM",
]
