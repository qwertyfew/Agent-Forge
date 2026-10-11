"""模型计费表：把 token 用量折算成人民币，用于界面上显示「预估费用」。

两条原则：

1. **本地模型永远 0 元**。跑在本机 Ollama 上的模型只烧电，不进这张表。
2. **价格只是预估值**。各厂商调价频繁，这里给的是常见档位，和真实账单
   可能有出入——界面上统一标注「预估」。要校准直接改下面的表即可。

单价单位：人民币元 / 百万 token（¥ per 1M tokens），(输入价, 输出价)。
"""

from __future__ import annotations

# 全局兜底：表里查不到时用它，宁可给个偏高的数也不让界面显示 0 元
DEFAULT_PRICE: tuple[float, float] = (4.0, 16.0)

# 厂商兜底价（某个厂商没匹配到具体模型号时用）
PROVIDER_PRICE: dict[str, tuple[float, float]] = {
    "ollama": (0.0, 0.0),
    "openai": (15.0, 60.0),
    "anthropic": (25.0, 120.0),
    "deepseek": (2.0, 8.0),
    "moonshot": (4.0, 16.0),
    "zhipu": (4.0, 16.0),
    "aliyun": (4.0, 16.0),
    "siliconflow": (2.0, 8.0),
    "custom": (4.0, 16.0),
}

# 具体模型价（key 用小写；匹配时做「归一化后包含」判断，命中更长的优先）
MODEL_PRICE: dict[str, tuple[float, float]] = {
    # ---- DeepSeek 官方 ----
    "deepseek-v4-pro": (4.0, 16.0),
    "deepseek-v4-flash": (0.5, 2.0),
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
    # ---- 硅基流动 ----
    "deepseek-ai/deepseek-v4-flash": (1.0, 3.0),
    "zai-org/glm-5.3": (4.0, 16.0),
    "moonshotai/kimi-k2.7-code": (4.0, 16.0),
    "qwen/qwen3.8-27b": (2.0, 8.0),
    # ---- OpenAI ----
    "gpt-5.5": (15.0, 60.0),
    "gpt-5.4-mini": (2.0, 8.0),
    # ---- Anthropic ----
    "claude-opus-4-5": (110.0, 550.0),
    "claude-sonnet-4-5": (22.0, 110.0),
    # ---- 月之暗面 / 智谱 / 通义 ----
    "kimi-k2-0905-preview": (4.0, 16.0),
    "glm-5": (4.0, 16.0),
    "glm-4.6": (2.0, 8.0),
    "qwen3-max": (4.0, 16.0),
    "qwen-plus": (2.0, 8.0),
}

LOCAL_PRICE: tuple[float, float] = (0.0, 0.0)


def price_of(provider: str = "", model_id: str = "", kind: str = "") -> tuple[float, float]:
    """查某个模型的单价 (输入价, 输出价)，单位 ¥/1M tokens。

    kind == "local"（或 provider == "ollama"）直接返回 0，本地模型不产生费用。
    """
    if kind == "local" or (provider or "").lower() == "ollama":
        return LOCAL_PRICE
    mid = (model_id or "").strip().lower()
    if mid:
        if mid in MODEL_PRICE:
            return MODEL_PRICE[mid]
        # 归一化匹配：厂商侧可能带日期后缀或命名空间，命中更长的优先
        hits = [(k, v) for k, v in MODEL_PRICE.items() if k in mid or mid in k]
        if hits:
            hits.sort(key=lambda kv: -len(kv[0]))
            return hits[0][1]
    return PROVIDER_PRICE.get((provider or "").lower(), DEFAULT_PRICE)


def is_free(provider: str = "", kind: str = "") -> bool:
    """这个模型是否不花钱（本地模型）。"""
    return kind == "local" or (provider or "").lower() == "ollama"


def cost_of(
    prompt_tokens: int,
    completion_tokens: int,
    provider: str = "",
    model_id: str = "",
    kind: str = "",
) -> float:
    """按 token 用量算钱（元）。本地模型恒为 0。"""
    if is_free(provider, kind):
        return 0.0
    pin, pout = price_of(provider, model_id, kind)
    return (
        max(0, int(prompt_tokens or 0)) / 1_000_000 * pin
        + max(0, int(completion_tokens or 0)) / 1_000_000 * pout
    )


def fmt_cost(cost: float) -> str:
    """把金额格式化成界面上的短字符串。"""
    c = float(cost or 0)
    if c <= 0:
        return "¥0"
    if c < 0.01:
        return f"¥{c:.4f}"
    if c < 1:
        return f"¥{c:.3f}"
    return f"¥{c:.2f}"


def price_note(provider: str, model_id: str, kind: str = "") -> str:
    """给人看的一行单价说明，例如「输入 ¥4 / 输出 ¥16 每百万 token」。"""
    if is_free(provider, kind):
        return "本地模型，不产生费用"
    pin, pout = price_of(provider, model_id, kind)
    return f"预估单价 输入 ¥{pin:g} / 输出 ¥{pout:g}（每百万 token）"


__all__ = [
    "DEFAULT_PRICE", "PROVIDER_PRICE", "MODEL_PRICE", "LOCAL_PRICE",
    "price_of", "cost_of", "is_free", "fmt_cost", "price_note",
]
