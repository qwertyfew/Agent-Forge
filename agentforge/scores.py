"""模型能力评分：把每个可用模型映射到八个维度的 0-100 分。

为什么要评分：AgentForge 可以同时挂载多个模型（本地 + 付费），任务该交给谁
不能靠拍脑袋。这里把「模型擅长什么」量化成一组可比较的数字，路由、组合、
「一键最优配置」都基于它做决策。

八维度口径（归一化 0-100，100 = 当下最强）：

    coding     写代码、改 bug、跑真实工程任务
    reasoning  复杂推理、多步规划、工具调用可靠性
    writing    写作、翻译、总结、文案
    chinese    中文理解与表达
    math       数学推导与计算
    speed      生成速度与首字延迟
    cost       性价比（本地免费模型拿高分）
    context    有效长上下文能力（窗口大小 + 长文检索）

分数来源分两类：
    榜单  有公开 benchmark / 官方技术报告支撑
    估算  没有直接数据，按模型规模、家族、厂商基线推断

组合得分：一个模型池的能力 = 各维度取池内最高分（某个任务总能交给最擅长
它的那个模型），总分再按 WEIGHTS 加权。这比"取平均"更贴近实际使用感受。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 维度定义

DIMENSIONS: list[tuple[str, str]] = [
    ("coding", "编码"),
    ("reasoning", "推理"),
    ("writing", "写作"),
    ("chinese", "中文"),
    ("math", "数学"),
    ("speed", "速度"),
    ("cost", "成本"),
    ("context", "上下文"),
]
DIM_KEYS = [k for k, _ in DIMENSIONS]
DIM_LABEL = dict(DIMENSIONS)

# 总分权重：Agent 场景下编码与推理是主战场，写作/中文次之，
# 速度与成本是实际体验的隐藏项，长上下文是加分项。
WEIGHTS: dict[str, float] = {
    "coding": 0.22,
    "reasoning": 0.22,
    "writing": 0.13,
    "chinese": 0.10,
    "math": 0.10,
    "speed": 0.09,
    "cost": 0.07,
    "context": 0.07,
}


def _blank() -> dict[str, int]:
    return {k: 0 for k in DIM_KEYS}


def total_of(scores: dict) -> int:
    """按权重算总分（0-100）。"""
    return int(round(sum(scores.get(k, 0) * w for k, w in WEIGHTS.items())))


# ---------------------------------------------------------------- 内置跑分库
# 键是归一化后的模型名；同一个模型有多个写法时放进 match 列表。
# 数据来源见每项的 evidence 字段，榜单类数据为公开 benchmark 的归一化结果。

KNOWLEDGE: dict[str, dict] = {
    # ---------------- 本地模型（config.yaml 默认挂载） ----------------
    "qwen3.5:9b": {
        "match": ["qwen3.5:9b", "qwen3.5-9b", "qwen3.5:9b-instruct", "qwen3.5-9b-instruct"],
        "version": "Qwen3.5-9B",
        "scores": {"coding": 48, "reasoning": 52, "writing": 56, "math": 58,
                   "chinese": 70, "speed": 78, "cost": 95, "context": 74},
        "source": "榜单",
        "evidence": "官方模型卡 MMLU-Pro 82.5 / GPQA Diamond 81.7 / IFEval 91.5 / LongBench v2 55.2；"
                    "第三方 AIME25 56.07、LiveCodeBench v6 69.86、BFCLv4 60.13；"
                    "RTX 5060 Ti 16GB Q4 实测约 65 tok/s，原生 262K 上下文。"
                    "coding/reasoning 按 9B 规模在真实仓库任务上的表现下调。",
        "context_window": 262144,
        "price_in": 0, "price_out": 0,
    },
    "qwen2.5-coder:14b": {
        "match": ["qwen2.5-coder:14b", "qwen2.5-coder-14b", "qwen2.5-coder:14b-instruct"],
        "version": "Qwen2.5-Coder-14B-Instruct",
        "scores": {"coding": 45, "reasoning": 36, "writing": 40, "math": 42,
                   "chinese": 62, "speed": 55, "cost": 95, "context": 55},
        "source": "榜单",
        "evidence": "HumanEval 89.6 / MBPP 86.2（作者公布，Q4_K_M）；但 LiveCodeBench 仅 37.1，"
                    "SWE-bench 真实仓库类任务仅个位数，故 coding 下调；"
                    "消费级显卡 Q4 约 40~55 tok/s，128K 上下文。",
        "context_window": 131072,
        "price_in": 0, "price_out": 0,
    },
    "qwen3:4b": {
        "match": ["qwen3:4b", "qwen3-4b", "qwen3:4b-instruct"],
        "version": "Qwen3-4B",
        "scores": {"coding": 32, "reasoning": 34, "writing": 38, "math": 48,
                   "chinese": 60, "speed": 88, "cost": 96, "context": 42},
        "source": "榜单",
        "evidence": "官方 MMLU-Redux 83.7 / C-Eval 77.5 / MATH-500 97.0（thinking 模式）；"
                    "非思考版 MMLU-Pro 58.0、GPQA 41.7；RTX 3060 约 120 tok/s；原生 32K。"
                    "math 因 MATH-500 高但 AIME25 仅 19.1 取中。",
        "context_window": 32768,
        "price_in": 0, "price_out": 0,
    },
    "qwen3.5:4b": {
        "match": ["qwen3.5:4b", "qwen3.5-4b"],
        "version": "Qwen3.5-4B（2026-02，Apache-2.0，多模态）",
        "scores": {"coding": 66, "reasoning": 72, "writing": 66, "math": 74,
                   "chinese": 78, "speed": 82, "cost": 95, "context": 80},
        "source": "榜单",
        "evidence": "官方/榜单：MMLU-Pro 79.1 / GPQA Diamond 76.2 / LiveCodeBench 55.8；"
                    "262K 上下文 + 多模态（读图）。4B 里编码最强档，全面碾压上代 Qwen3-4B"
                    "（LCB 55.8 vs 约 35）。本地免费，Q4 约 2.5GB。",
        "context_window": 262144,
        "price_in": 0, "price_out": 0,
    },
    "gemma4:12b": {
        "match": ["gemma4:12b", "gemma-4-12b", "gemma4-12b", "gemma4"],
        "version": "Gemma 4 12B（2026-06，Apache-2.0，多模态）",
        "scores": {"coding": 78, "reasoning": 82, "writing": 78, "math": 84,
                   "chinese": 62, "speed": 62, "cost": 94, "context": 86},
        "source": "榜单",
        "evidence": "Artificial Analysis 小模型（<15B）综合第一：MMLU-Pro 77.2 / GPQA Diamond 78.8 / "
                    "LiveCodeBench 72.0 / AIME2026 77.5；256K 上下文；原生工具调用（tau2 68+）；"
                    "多模态（图+文）。中文能力一般（MMMLU 高但中文创作弱）。本地 Q4 约 7.7GB。",
        "context_window": 262144,
        "price_in": 0, "price_out": 0,
    },

    # ---------------- OpenAI ----------------
    "gpt-5.5": {
        "match": ["gpt-5.5", "gpt-5.5-high", "gpt-5.5-thinking"],
        "version": "GPT-5.5",
        "scores": {"coding": 96, "reasoning": 95, "writing": 94, "math": 95,
                   "chinese": 78, "speed": 72, "cost": 35, "context": 88},
        "source": "榜单",
        "evidence": "SWE-bench Verified 88.7%（2026-04 榜首）；GPQA 86.0 / AIME2025 95.2 / "
                    "MMLU-Pro 90.1 / ARC-AGI-2 71.3；LMArena 1506；约 70 tok/s；$5/$30；1M 上下文。",
        "context_window": 1000000,
        "price_in": 5, "price_out": 30,
    },
    "gpt-5.4-mini": {
        "match": ["gpt-5.4-mini", "gpt-5-mini", "gpt-5.4mini", "gpt-5.4 mini"],
        "version": "GPT-5.4 mini",
        "scores": {"coding": 72, "reasoning": 68, "writing": 70, "math": 74,
                   "chinese": 65, "speed": 82, "cost": 68, "context": 72},
        "source": "榜单",
        "evidence": "SWE-bench Verified 73% / GPQA 88% / LiveCodeBench 81.5% / HLE 28.2%；"
                    "LiveBench 数学 78.5、Agentic coding 41.7；实测吞吐 98.2 tok/s；$0.75/$4.50；400K。",
        "context_window": 400000,
        "price_in": 0.75, "price_out": 4.5,
    },

    # ---------------- Anthropic ----------------
    "claude-opus-4.5": {
        "match": ["claude-opus-4.5", "claude-opus-4-5", "opus-4.5", "claude-opus-4-5-20251101"],
        "version": "Claude Opus 4.5",
        "scores": {"coding": 90, "reasoning": 90, "writing": 91, "math": 76,
                   "chinese": 74, "speed": 70, "cost": 40, "context": 74},
        "source": "榜单",
        "evidence": "SWE-bench Verified 80.9%（首个破 80%）；GPQA 87 / MMLU-Pro 85.6 / "
                    "tau2-bench Telecom 98.2 / OSWorld 66.3；200K；$5/$25；约 42 tok/s。"
                    "该版 AIME2025 仅 48.1%，数学维度下调。",
        "context_window": 200000,
        "price_in": 5, "price_out": 25,
    },
    "claude-sonnet-4.5": {
        "match": ["claude-sonnet-4.5", "claude-sonnet-4-5", "sonnet-4.5"],
        "version": "Claude Sonnet 4.5",
        "scores": {"coding": 86, "reasoning": 82, "writing": 88, "math": 85,
                   "chinese": 70, "speed": 70, "cost": 58, "context": 70},
        "source": "榜单",
        "evidence": "SWE-bench Verified 77.2% / GPQA 83.4 / AIME2025 87 / MATH 97.7 / MMMLU 89.1；"
                    "200K；$3/$15；约 42 tok/s。性价比优于 Opus。",
        "context_window": 200000,
        "price_in": 3, "price_out": 15,
    },

    # ---------------- Google ----------------
    "gemini-3-pro": {
        "match": ["gemini-3-pro", "gemini-3-pro-preview", "gemini3-pro"],
        "version": "Gemini 3 Pro",
        "scores": {"coding": 84, "reasoning": 90, "writing": 88, "math": 92,
                   "chinese": 72, "speed": 85, "cost": 62, "context": 90},
        "source": "榜单",
        "evidence": "GPQA Diamond 91.9 / AIME2025 100 / HLE 45.8 / SWE-bench Verified 76.2 / "
                    "MMMLU 91.8；LMArena 1485；1M 上下文；$2/$12；约 90 tok/s。",
        "context_window": 1000000,
        "price_in": 2, "price_out": 12,
    },

    # ---------------- DeepSeek ----------------
    "deepseek-v4-pro": {
        "match": ["deepseek-v4-pro", "deepseek-v4"],
        "version": "DeepSeek-V4-Pro（2026-04，1.6T MoE/49B active，MIT）",
        "scores": {"coding": 91, "reasoning": 89, "writing": 84, "math": 92,
                   "chinese": 86, "speed": 78, "cost": 82, "context": 92},
        "source": "榜单",
        "evidence": "SWE-bench Verified 80.6% / LiveCodeBench 93.5% / GPQA Diamond 81.4~90.1% / "
                    "AIME2025 89.3、AIME2026 99.4%；1M 上下文；$0.435/$0.87；MIT 开源权重。",
        "context_window": 1000000,
        "price_in": 0.435, "price_out": 0.87,
    },
    "deepseek-flash": {
        "match": ["deepseek-flash", "deepseek-v4-flash"],
        "version": "DeepSeek-Flash / DeepSeek-V4-Flash（官方快速档）",
        "scores": {"coding": 76, "reasoning": 80, "writing": 72, "math": 82,
                   "chinese": 80, "speed": 86, "cost": 92, "context": 78},
        "source": "估算",
        "evidence": "官方 API 实测为带 reasoning_content 的快速推理模型，定位低于 V4-Pro："
                    "响应快、价格低，适合做工具编排与简单任务的主力。无独立榜单，按官方定位估算。",
        "context_window": 131072,
        "price_in": 0.15, "price_out": 0.6,
    },
    "deepseek-v3.2": {
        "match": ["deepseek-v3.2", "deepseek-chat", "deepseek-v3", "deepseek-v3.2-exp"],
        "version": "DeepSeek-V3.2",
        "scores": {"coding": 88, "reasoning": 86, "writing": 82, "math": 90,
                   "chinese": 84, "speed": 76, "cost": 82, "context": 90},
        "source": "榜单",
        "evidence": "SWE-bench Verified 73.1% / LiveCodeBench 83.3%（V3.2 本体）；"
                    "1M 上下文；$0.435/$0.87，价格约为同级国外模型的 1/10。",
        "context_window": 1000000,
        "price_in": 0.435, "price_out": 0.87,
    },
    "deepseek-r1": {
        "match": ["deepseek-r1", "deepseek-reasoner", "deepseek-r1-0528"],
        "version": "DeepSeek-R1",
        "scores": {"coding": 80, "reasoning": 88, "writing": 76, "math": 92,
                   "chinese": 82, "speed": 74, "cost": 84, "context": 84},
        "source": "榜单",
        "evidence": "R1-0528：AIME2024 79.8%、64K、$0.55/$2.19。推理型模型，"
                    "思考链长，不适合当工具编排主力，数学维度突出。",
        "context_window": 65536,
        "price_in": 0.55, "price_out": 2.19,
    },

    # ---------------- 月之暗面 ----------------
    "kimi-k2": {
        "match": ["kimi-k2", "kimi-k2-0905", "kimi-k2.7-code", "moonshot-v1", "kimi-latest"],
        "version": "Kimi K2 / K2.7-Code",
        "scores": {"coding": 86, "reasoning": 84, "writing": 84, "math": 88,
                   "chinese": 82, "speed": 70, "cost": 78, "context": 68},
        "source": "榜单",
        "evidence": "SWE-bench Verified 80.2% / LiveCodeBench 89.6% / GPQA Diamond 90.8% / "
                    "AIME 96.1% / HLE 36.4%（带工具 54.0）/ Toolathlon 50.0；"
                    "262K；$0.96/$3.97。Agent 与工具调用方向表现突出。",
        "context_window": 262144,
        "price_in": 0.96, "price_out": 3.97,
    },

    # ---------------- 智谱 ----------------
    "glm-5": {
        "match": ["glm-5", "glm-5.0", "glm-5.2", "glm-5.3", "glm-4.6", "glm-4-plus"],
        "version": "GLM-5 / 5.2 / 5.3",
        "scores": {"coding": 85, "reasoning": 86, "writing": 84, "math": 90,
                   "chinese": 88, "speed": 74, "cost": 72, "context": 88},
        "source": "榜单",
        "evidence": "AIME2026 99.2% / HMMT2025 94.4% / GPQA 91.2%；SWE-bench Pro 62.8%"
                    "（同口径下超过 GPT-5.5 的 58.6%）；1M 上下文；$0.75/$2.40；"
                    "SuperCLUE 国产第一梯队。",
        "context_window": 1000000,
        "price_in": 0.75, "price_out": 2.4,
    },

    # ---------------- 阿里 ----------------
    "qwen3-max": {
        "match": ["qwen3-max", "qwen-max", "qwen3-max-preview", "qwen-max-latest",
                  "qwen3.5-max", "qwen3-max-thinking"],
        "version": "Qwen3-Max",
        "scores": {"coding": 85, "reasoning": 86, "writing": 86, "math": 90,
                   "chinese": 92, "speed": 74, "cost": 68, "context": 74},
        "source": "榜单",
        "evidence": "Qwen3-Max-Thinking：SWE-bench Verified 75.3% / LiveCodeBench 85.9%，"
                    "AIME25 与 HMMT 达 100；Arena ELO 1340；约 55 tok/s；$0.78/$3.9；262K。"
                    "中文能力（SuperCLUE 智指 72.62、智能体任务规划 89.53）国产第一。",
        "context_window": 262144,
        "price_in": 0.78, "price_out": 3.9,
    },
    "qwen3.8-27b": {
        "match": ["qwen3.8-27b", "qwen3.8-27b-instruct"],
        "version": "Qwen3.8-27B（Qwen3.8 系列中杯）",
        "scores": {"coding": 76, "reasoning": 78, "writing": 80, "math": 80,
                   "chinese": 90, "speed": 76, "cost": 74, "context": 72},
        "source": "估算",
        "evidence": "Qwen3.8 系列 SuperCLUE 智指 72.62（国产第一、智能体任务规划 89.53），"
                    "但 27B 中杯无独立公开跑分，按系列定位（低于 Max、高于 9B）估算。",
        "context_window": 262144,
        "price_in": 0.35, "price_out": 1.4,
    },

    # ---------------- MiniMax ----------------
    "minimax-m2": {
        "match": ["minimax-m2", "minimax-m1", "minimax-text-01", "minimax-m2.1"],
        "version": "MiniMax-M2",
        "scores": {"coding": 78, "reasoning": 74, "writing": 76, "math": 80,
                   "chinese": 80, "speed": 74, "cost": 82, "context": 90},
        "source": "榜单",
        "evidence": "SWE-bench Verified 69.4% / LiveCodeBench 83% / GPQA 78% / "
                    "AIME2025 78% / MMLU-Pro 82% / tau2-bench Telecom 87%；"
                    "1M 上下文；$0.30/$1.20；约 70 tok/s。",
        "context_window": 1000000,
        "price_in": 0.3, "price_out": 1.2,
    },
}


def normalize(name: str) -> str:
    """把模型名归一化为查表键：小写、去掉 :latest 之类的标签修饰。"""
    s = str(name or "").strip().lower()
    s = s.split(" ")[0]
    s = re.sub(r":(latest|default)$", "", s)
    return s


_INDEX: dict[str, str] = {}
for _key, _info in KNOWLEDGE.items():
    _INDEX[_key] = _key
    for _alias in _info.get("match", []):
        _INDEX[normalize(_alias)] = _key


# ---------------------------------------------------------------- 估算兜底

# 厂商基线：拿不到该模型的具体数据时，用厂商在同档位的历史表现打底
PROVIDER_BASE = {
    "openai":    {"coding": 78, "reasoning": 76, "writing": 80, "chinese": 74, "math": 78, "speed": 76, "cost": 50, "context": 78},
    "anthropic": {"coding": 78, "reasoning": 76, "writing": 84, "chinese": 70, "math": 72, "speed": 70, "cost": 42, "context": 82},
    "google":    {"coding": 72, "reasoning": 76, "writing": 74, "chinese": 68, "math": 80, "speed": 80, "cost": 60, "context": 90},
    "deepseek":  {"coding": 74, "reasoning": 74, "writing": 74, "chinese": 82, "math": 78, "speed": 68, "cost": 78, "context": 70},
    "moonshot":  {"coding": 72, "reasoning": 70, "writing": 76, "chinese": 80, "math": 70, "speed": 66, "cost": 70, "context": 76},
    "zhipu":     {"coding": 74, "reasoning": 72, "writing": 72, "chinese": 80, "math": 72, "speed": 68, "cost": 72, "context": 70},
    # 通义在 settings 里的 provider id 是 aliyun，这里兼容 alibaba 写法
    "aliyun":    {"coding": 76, "reasoning": 74, "writing": 76, "chinese": 84, "math": 76, "speed": 70, "cost": 68, "context": 76},
    "alibaba":   {"coding": 76, "reasoning": 74, "writing": 76, "chinese": 84, "math": 76, "speed": 70, "cost": 68, "context": 76},
    "minimax":   {"coding": 72, "reasoning": 70, "writing": 74, "chinese": 78, "math": 74, "speed": 72, "cost": 80, "context": 86},
    "siliconflow": {"coding": 70, "reasoning": 68, "writing": 70, "chinese": 76, "math": 68, "speed": 78, "cost": 74, "context": 72},
    "ollama":    {"coding": 45, "reasoning": 46, "writing": 52, "chinese": 62, "math": 48, "speed": 66, "cost": 92, "context": 52},
}

# 模型名里能透露体量的线索：参数规模越大，能力基线越高
_SIZE_HINTS = [
    (r"\b(\d{2,3})\s*b\b", None),          # 70b / 14b / 4b
    (r"[-_:](\d{1,2})b", None),
]


def _size_of(name: str) -> float | None:
    s = normalize(name)
    best: float | None = None
    for m in re.finditer(r"(\d{1,3})\s*b(?![a-z0-9])", s):
        v = float(m.group(1))
        if 1 <= v <= 1000:
            best = max(best or 0.0, v)
    return best


def _size_adjust(scores: dict, size: float | None) -> dict:
    """按参数量微调估算分：越小的模型能力越弱但越快越便宜。"""
    if not size:
        return scores
    if size <= 4:
        delta = -12
    elif size <= 9:
        delta = -4
    elif size <= 16:
        delta = 0
    elif size <= 34:
        delta = 5
    elif size <= 80:
        delta = 9
    else:
        delta = 12
    out = dict(scores)
    for k in ("coding", "reasoning", "writing", "math"):
        out[k] = _clamp(out.get(k, 60) + delta)
    out["context"] = _clamp(out.get("context", 60) + (delta // 2))
    if size <= 9:
        out["speed"] = _clamp(out.get("speed", 70) + 12)
        out["cost"] = _clamp(out.get("cost", 90) + 4)
    elif size >= 34:
        out["speed"] = _clamp(out.get("speed", 70) - 14)
    return out


def _clamp(v) -> int:
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        v = 0
    return max(0, min(100, v))


# ---------------------------------------------------------------- 对外接口

@dataclass
class ScoreInfo:
    scores: dict[str, int]
    source: str = "估算"          # 榜单 | 估算
    evidence: str = ""
    version: str = ""
    context_window: int = 0
    price_in: float = 0.0
    price_out: float = 0.0
    dimension_labels: dict = field(default_factory=lambda: dict(DIM_LABEL))

    @property
    def total(self) -> int:
        return total_of(self.scores)

    def as_dict(self) -> dict:
        return {
            "scores": self.scores,
            "total": self.total,
            "source": self.source,
            "evidence": self.evidence,
            "version": self.version,
            "context_window": self.context_window,
            "price_in": self.price_in,
            "price_out": self.price_out,
        }


def lookup(name: str, provider: str = "", kind: str = "paid") -> ScoreInfo:
    """查模型得分：先查内置库，查不到按厂商基线 + 参数量估算。"""
    key = _INDEX.get(normalize(name))
    if key is None:
        # 付费模型可能是「provider/model-id」写法，再试一次尾段
        tail = normalize(name).split("/")[-1]
        key = _INDEX.get(tail)

    if key:
        info = KNOWLEDGE[key]
        return ScoreInfo(
            scores={k: _clamp(v) for k, v in info["scores"].items()},
            source=info.get("source", "榜单"),
            evidence=info.get("evidence", ""),
            version=info.get("version", name),
            context_window=int(info.get("context_window") or 0),
            price_in=float(info.get("price_in") or 0),
            price_out=float(info.get("price_out") or 0),
        )

    base = dict(PROVIDER_BASE.get(provider, PROVIDER_BASE["ollama"]))
    if kind == "local":
        base.update({"cost": max(base.get("cost", 80), 90)})
    scores = _size_adjust(base, _size_of(name))
    return ScoreInfo(
        scores=scores,
        source="估算",
        evidence=f"未收录该模型的公开跑分，按厂商（{provider or '未知'}）同档位基线"
                 f"{'结合参数量' if _size_of(name) else ''}估算，可在设置里手动覆盖。",
        version=name,
    )


def combo(entries: list[dict]) -> dict:
    """组合得分：各维度取池内最高分（能力取长补短），再算加权总分。

    entries 每项需含 scores 与 name。
    """
    if not entries:
        return {"scores": _blank(), "total": 0, "best": {}, "count": 0}
    best: dict[str, dict] = {}
    for dim in DIM_KEYS:
        top = max(entries, key=lambda e: e.get("scores", {}).get(dim, 0))
        best[dim] = {
            "value": int(top.get("scores", {}).get(dim, 0)),
            "model": top.get("name", ""),
            "entry_id": top.get("id", ""),
        }
    scores = {d: best[d]["value"] for d in DIM_KEYS}
    return {"scores": scores, "total": total_of(scores), "best": best, "count": len(entries)}


def weakest(scores: dict, n: int = 2) -> list[str]:
    """组合里最弱的几个维度，用于提示"这个组合还缺什么"。"""
    return [k for k, _ in sorted(scores.items(), key=lambda kv: kv[1])[:n]]


def pick_best(entries: list[dict], dimension: str) -> dict | None:
    """在给定池子里挑该维度最强的模型。"""
    if not entries:
        return None
    return max(entries, key=lambda e: e.get("scores", {}).get(dimension, 0))
