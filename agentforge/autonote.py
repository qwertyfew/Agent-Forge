"""任务结束后自动沉淀记忆。

以前只有模型主动调用 `remember` 工具才会留下记忆，小模型几乎不会主动调，
于是「一堆任务跑完，记忆文件还是空的」。这里补上自动生成：任务收尾时按两级
抽取「以后还用得上的信息」。

1. 规则抽取（立即返回、不花算力、不依赖模型）
   用户话里带「记住 / 以后 / 下次 / 每次 / 一律 / 不要 / 偏好…」这类信号，
   就把那句话原样收进记忆——用户明说要记住的东西绝不能漏。
2. 模型抽取（可选，后台线程里跑，用池子里最便宜的本地模型）
   把这一轮对话压成 0~2 条结论。失败、超时、模型胡说都只是「没抽到」。

两条结果都要过一遍去重：和现有记忆重复的句子不再写入。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 用户明确表达「这条要记住」的信号词
SIGNALS = (
    "记住", "记一下", "记下", "牢记",
    "以后", "今后", "下次", "每次", "后续",
    "一律", "统一", "默认", "总是", "始终",
    "不要", "别用", "禁止", "必须", "一定要",
    "偏好", "我喜欢", "我习惯", "我的要求", "我的习惯", "请注意",
)

NOTE_MIN = 6        # 太短的一句话没有信息量
NOTE_MAX = 300      # 单条上限（MemoryStore 自己还会再截断）
MAX_RULES = 3       # 规则抽取最多条数
MAX_MODEL = 2       # 模型抽取最多条数

_SENT_SPLIT = re.compile(r"[。！？!?\n；;]+")

# 多步任务的编排提示词（orchestrator 的 STEP_FIRST/STEP_NEXT/PROGRESS_TEMPLATE）
# 是作为 user 消息进历史的，里头带着「不要 / 必须」这类词，会被当成用户要求抓下来
# ——实测记进过「请只完成这一步，不要越界去做后面的步骤」这种纯过程指令。
# 这些句子对以后的任务毫无价值，每轮还要跟着系统提示重发一遍，必须挡掉。
_NOISE = re.compile(
    r"本步|这一步|越界|不要重复|已完成的操作|凭空猜测|"
    r"按步骤|逐步完成|做完后用|落脚点|在此基础上继续|只完成"
)


def _noisy(text: str) -> bool:
    """这句话是不是任务编排的过程指令（而不是值得长期保留的结论）。"""
    return bool(_NOISE.search(str(text or "")))

MODEL_PROMPT = """下面是一段智能体任务的对话记录。请只提炼**以后还用得上的结论**，例如：
用户偏好、项目约定、环境事实、踩过的坑。

规则：
- 每行一条，以「- 」开头，不超过 40 字，不要写过程、不要写这次的临时结果
- 不要记录任务步骤指令（如「只完成本步」「不要重复已完成的操作」），那些不是长期结论
- 最多 2 条；如果没有值得长期保留的信息，只输出 NONE
- 用中文，直接输出，不要任何解释
"""


def _norm(text: str) -> str:
    """归一化用于去重比较：去掉空白、标点差异、大小写差异。"""
    s = "".join(str(text or "").split())
    return s.strip("，。！？!?；;：:,.").lower()


def _bigrams(text: str) -> set[str]:
    s = "".join(ch for ch in text if ch.strip() and ch not in "，。！？!?；;：:,.-·、（）()《》\"'")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _similar(a: str, b: str, threshold: float = 0.55) -> bool:
    """两句话是不是在说同一件事。

    规则抽取和模型抽取常给出同义的两种说法（「记住：注释一律用中文」与
    「用户偏好：代码注释一律使用中文」），只按字面去重会留下两条冗余记忆。
    这里用字符二元组的**包含度**（交集 / 较短一方的规模）判断：短句基本被
    长句覆盖就算重复。实测同义改写落在 0.58 以上，无关句子在 0.45 以下。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 8:
        return False
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return False
    return len(A & B) / min(len(A), len(B)) >= threshold


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(str(text or "")) if s.strip()]


def rule_notes(messages: Iterable[dict]) -> list[str]:
    """规则抽取：从用户消息里挑出「要求记住」的句子。"""
    out: list[str] = []
    for m in messages:
        if (m or {}).get("role") != "user":
            continue
        for s in _sentences(m.get("content") or ""):
            if not (NOTE_MIN <= len(s) <= NOTE_MAX):
                continue
            if _noisy(s):
                continue
            if not any(k in s for k in SIGNALS):
                continue
            if s not in out:
                out.append(s)
            if len(out) >= MAX_RULES:
                return out
    return out


def _transcript(messages: list[dict], per_msg: int = 700, limit: int = 8) -> str:
    """把对话压成一段短文本喂给模型：只取尾部若干条，每条截断。"""
    tail = [m for m in (messages or []) if (m or {}).get("role") in ("user", "assistant")]
    lines: list[str] = []
    for m in tail[-limit:]:
        role = "用户" if m["role"] == "user" else "助手"
        text = " ".join(str(m.get("content") or "").split())
        if not text:
            continue
        lines.append(f"{role}：{text[:per_msg]}")
    return "\n".join(lines)


def parse_model_notes(raw: str) -> list[str]:
    """解析模型输出：支持「- xxx」「1. xxx」或纯行，过滤明显是废话的行。"""
    out: list[str] = []
    for line in str(raw or "").splitlines():
        s = line.strip().lstrip("-*·").strip()
        s = re.sub(r"^\d+[.、)]\s*", "", s)
        if not s or s.upper() == "NONE":
            continue
        if len(s) < NOTE_MIN:
            continue
        if _noisy(s):
            continue
        if s in out:
            continue
        out.append(s[:NOTE_MAX])
        if len(out) >= MAX_MODEL:
            break
    return out


def model_notes(hub, spec, messages: list[dict]) -> list[str]:
    """用指定模型抽一次记忆；任何异常都吞掉，返回空列表。"""
    text = _transcript(messages)
    if not text:
        return []
    msgs = [
        {"role": "system", "content": MODEL_PROMPT},
        {"role": "user", "content": text},
    ]
    try:
        result = None
        for kind, payload in hub.chat_stream(spec, msgs, None, {"temperature": 0.2}):
            if kind == "result":
                result = payload
        return parse_model_notes(getattr(result, "content", "") or "")
    except Exception:  # noqa: BLE001
        return []


def pick_extractor(store, cfg) -> Any:
    """挑一个最便宜可用的**本地**模型做抽取；付费模型不参与，避免偷偷花钱。"""
    try:
        entries = [e for e in store.enabled() if getattr(e, "kind", "local") == "local"]
    except Exception:  # noqa: BLE001
        return None
    if not entries:
        return None
    best = max(entries, key=lambda e: ((e.scores or {}).get("cost", 0),
                                       (e.scores or {}).get("speed", 0)))
    key = getattr(best, "key", "")
    try:
        return cfg.model(key)
    except Exception:  # noqa: BLE001
        return None


def dupe_of(store, text: str) -> str:
    """这条内容是不是已经记过了？返回已有的那条原文，没有重复则返回空串。

    模型的 remember 工具、规则抽取、模型提炼三个入口都走这里判重，
    否则同一件事会被写进记忆好几次（实测小模型会连着调用 remember 三次）。
    """
    n = _norm(text)
    if not n:
        return ""
    for e in store.entries():
        old = _norm(e.get("text", ""))
        if _similar(n, old):
            return e.get("text", "")
    return ""


def capture(store, messages: list[dict], task_id: str = "") -> list[dict]:
    """把规则抽取的结果写进记忆，返回新写入的条目（已去重）。"""
    added: list[dict] = []
    for note in rule_notes(messages):
        if dupe_of(store, note):
            continue
        entry = store.append(note, task_id)
        if entry:
            added.append(entry)
    return added


def capture_model(store, hub, spec, messages: list[dict], task_id: str = "") -> list[dict]:
    """后台线程里跑的模型抽取：同样去重后再落盘。"""
    if spec is None:
        return []
    notes = model_notes(hub, spec, messages)
    if not notes:
        return []
    added: list[dict] = []
    for note in notes:
        if dupe_of(store, note):
            continue
        entry = store.append(note, task_id)
        if entry:
            added.append(entry)
    return added


__all__ = [
    "capture", "capture_model", "dupe_of", "model_notes", "parse_model_notes",
    "pick_extractor", "rule_notes",
]
