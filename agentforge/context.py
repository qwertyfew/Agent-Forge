"""上下文压缩：超限时自动瘦身，也可以手动触发。

两级策略，先便宜后昂贵：

    L1 截断   把较早的工具返回压成一两行摘要，不动对话结构
    L2 摘要   把较早的整个对话轮次交给模型重写成一段要点

压缩的难点在于不能把消息切坏：assistant 带 tool_calls 之后必须跟着对应的
tool 消息，否则 Ollama 会报协议错误。所以分割点一律选在"轮次边界"上——
即下一条 user 消息开始的地方。
"""

from __future__ import annotations

from dataclasses import dataclass

SUMMARY_PROMPT = """请把下面这段较早的对话压缩成一段要点，供后续继续工作使用。

要求：
- 用中文，控制在 {max_chars} 字以内，只写结论不写过程
- 必须保留：已完成的操作、改动过的文件路径、关键结论、用户明确的要求
- 必须去掉：中间推理、重复的工具返回、失败的尝试

对话内容：
{chunk}
"""


@dataclass
class CompactResult:
    messages: list[dict]
    strategy: str          # none | truncate | summarize
    before: int            # 压缩前字符数
    after: int             # 压缩后字符数
    note: str = ""         # 给人看的一句话说明

    @property
    def changed(self) -> bool:
        return self.strategy != "none"


def measure(messages: list[dict]) -> int:
    """粗略估算上下文体积（字符数）。中文一个字约占 1~2 token，够用作阈值判断。"""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):  # 多模态内容块
            for block in c:
                if isinstance(block, dict):
                    total += len(str(block.get("text") or ""))
        tc = m.get("tool_calls")
        if tc:
            total += len(str(tc))
    return total


def _est_tokens(text: str) -> float:
    """粗估 token 数：中日韩 ≈ 1 token/字，其余 ≈ 3.8 字符/token。"""
    cjk = 0
    for ch in text:
        c = ord(ch)
        if (0x3400 <= c <= 0x9FFF) or (0xF900 <= c <= 0xFAFF) or (0x3040 <= c <= 0x30FF):
            cjk += 1
    return cjk * 1.05 + (len(text) - cjk) / 3.8


def measure_tokens(messages: list[dict]) -> int:
    """按 token 估算同一批消息，口径与前端「上下文占比」一致。

    字符数阈值对中文会严重失真（6 万汉字 ≈ 6.3 万 token），中文长对话下
    可能在字符数还没到阈值时就已经把模型窗口撑爆、被 Ollama 从头部静默截断
    （最先被丢的正是系统提示）。所以再拿 token 数跟窗口比一次。
    """
    total = 0.0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += _est_tokens(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    total += _est_tokens(str(block.get("text") or ""))
        tc = m.get("tool_calls")
        if tc:
            total += _est_tokens(str(tc))
    return int(total)


def _turn_starts(messages: list[dict]) -> list[int]:
    """每条 user 消息的位置，即安全的轮次边界。"""
    return [i for i, m in enumerate(messages) if m.get("role") == "user"]


def _safe_cut(messages: list[dict], keep_chars: int) -> int:
    """找到切割位置：保证从 cut 往后至少保留 keep_chars 的内容，且 cut 是轮次边界。"""
    starts = _turn_starts(messages)
    if not starts:
        return 0
    acc = 0
    cut = 0
    for i in reversed(range(len(messages))):
        acc += len(str(messages[i].get("content") or ""))
        if acc >= keep_chars:
            # 落点必须往后退到最近的一个轮次起点
            cut = next((s for s in starts if s >= i), len(messages))
            break
    if cut <= 0:
        # 连一轮都保不住，就只留最后一轮
        cut = starts[-1]
    return cut


def truncate_tool_results(messages: list[dict], keep_recent: int) -> list[dict]:
    """L1：把较早的工具返回压成一行，保留最近 keep_recent 条完整结果。"""
    idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(idx) <= keep_recent:
        return messages
    stale = set(idx[: len(idx) - keep_recent])
    out = []
    for i, m in enumerate(messages):
        if i in stale:
            body = str(m.get("content") or "").replace("\n", " ")
            out.append(
                {"role": "tool", "content": f"[已压缩] {body[:160]}{' ...' if len(body) > 160 else ''}"}
            )
        else:
            out.append(m)
    return out


class Compressor:
    def __init__(self, cfg, client, hub=None):
        self.cfg = cfg
        self.client = client
        # 有 hub 就走多 provider 分发（总结可能落在本地或付费模型上）
        self.hub = hub

    @property
    def threshold(self) -> int:
        return int(getattr(self.cfg, "compact_threshold", 60_000))

    @property
    def keep_chars(self) -> int:
        """压缩后至少保留多少字符的近期对话。"""
        return int(getattr(self.cfg, "compact_keep_chars", 20_000))

    def window(self, model_key: str) -> int:
        """模型的上下文窗口（token）。取不到就返回 0，退回纯字符阈值判断。"""
        try:
            spec = self.cfg.model(model_key)
        except Exception:  # noqa: BLE001  模型被删了就按没有上限处理
            return 0
        return int(getattr(spec, "num_ctx", 0) or 0)

    def over_budget(self, messages: list[dict], model_key: str = "") -> bool:
        """该不该压缩：字符阈值到了，或者 token 数已经逼近模型窗口。

        token 这一路留 60% 的余量——剩下的 40% 要装系统提示、工具定义和
        模型这一轮生成的输出。不留余量的话请求会因为超窗口被静默截头。
        """
        if measure(messages) > self.threshold:
            return True
        win = self.window(model_key)
        if win:
            return measure_tokens(messages) > win * 0.6
        return False

    def need(self, messages: list[dict], model_key: str = "") -> bool:
        return self.over_budget(messages, model_key)

    def _summarize(self, chunk: list[dict], model_key: str) -> str:
        lines = []
        for m in chunk:
            role = m.get("role")
            text = str(m.get("content") or "")
            if not text.strip():
                continue
            if role == "tool":
                text = text[:400]
            if len(text) > 1200:
                text = text[:600] + " ... " + text[-400:]
            tag = {"user": "用户", "assistant": "助手", "tool": "工具返回"}.get(role, role)
            lines.append(f"{tag}: {text}")
        prompt = SUMMARY_PROMPT.format(
            max_chars=int(getattr(self.cfg, "compact_summary_chars", 1500)),
            chunk="\n".join(lines),
        )
        # 必须带上模型配置的 num_ctx：Ollama 默认窗口只有 2048，
        # 早期的摘要 prompt 往往有几万字，不指定会静默返回空内容。
        spec = self.cfg.model(model_key)
        options = dict(spec.options())
        options["temperature"] = 0.2
        try:
            if self.hub is not None:
                res = self.hub.chat(
                    spec,
                    [{"role": "user", "content": prompt}],
                    tools=None,
                    options=options,
                )
            else:
                res = self.client.chat(
                    model=spec.name,
                    messages=[{"role": "user", "content": prompt}],
                    tools=None,
                    options=options,
                )
            text = (res.content or "").strip()
        except Exception:  # noqa: BLE001
            return ""
        # 空输出意味着窗口不够或模型没听懂，宁可退回截断也不要塞一段废话
        return text if len(text) >= 20 else ""

    def compact(
        self,
        messages: list[dict],
        model_key: str,
        force: bool = False,
        allow_summarize: bool = True,
    ) -> CompactResult:
        """压缩历史。返回新列表与说明，原列表不被修改。"""
        before = measure(messages)
        if not force and not self.over_budget(messages, model_key):
            return CompactResult(messages, "none", before, before)

        # L1 先来一遍，便宜且立刻见效
        keep_recent = int(getattr(self.cfg, "keep_recent_tool_results", 4))
        step1 = truncate_tool_results(messages, keep_recent)
        after1 = measure(step1)
        if not force and not self.over_budget(step1, model_key):
            return CompactResult(
                step1, "truncate", before, after1,
                f"上下文 {before} → {after1} 字（截断较早工具返回）",
            )

        if not allow_summarize:
            return CompactResult(step1, "truncate", before, after1,
                                 f"上下文 {before} → {after1} 字（仅截断）")

        # L2 摘要：切掉早期轮次，留给模型重写
        cut = _safe_cut(step1, self.keep_chars)
        if cut <= 0 or cut >= len(step1):
            return CompactResult(step1, "truncate", before, after1,
                                 f"上下文 {before} → {after1} 字（内容太新，无法再摘要）")

        chunk, tail = step1[:cut], step1[cut:]
        # 摘要用通用模型，快且省显存；当前是代码模型时尤其划算
        skey = "general" if "general" in self.cfg.models else model_key
        summary = self._summarize(chunk, skey)
        if not summary:
            # 摘要没拿到就用截断的结果，至少比原样撑着强
            return CompactResult(
                step1, "truncate", before, after1,
                f"上下文 {before} → {after1} 字（摘要未生成，仅截断较早工具返回）",
            )
        merged = [
            {"role": "user", "content": f"[早期对话摘要]\n{summary}"},
            *tail,
        ]
        after = measure(merged)
        return CompactResult(
            merged, "summarize", before, after,
            f"上下文 {before} → {after} 字（已把前 {len(chunk)} 条消息重写为摘要）",
        )
