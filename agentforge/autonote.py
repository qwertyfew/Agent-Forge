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

每条记忆还会带上一个**层标签**（L1 事实 / L2 约定 / L3 偏好，见 memory.py）：
模型抽取时让它显式标出层，规则抽取则由 classify_layer 就地判定。层标签决定
后续任务注入系统提示时的检索深度——具体技术任务优先给 L1，常规对话优先给 L3/L2。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .memory import classify_layer, normalize_layer

# 强信号：一旦出现，基本可以断定是「用户在对你说以后该怎么办」，直接收。
SIGNALS = (
    "记住", "记一下", "记下", "牢记",
    "以后", "今后", "下次", "每次", "后续",
    "一律", "统一", "总是", "始终",
    "偏好", "我喜欢", "我习惯", "我的要求", "我的习惯",
)

# 弱信号：任务书里同样高频（「必须先用 read_file 读完全文」「不要把配角当主角」），
# 单凭它判定，会把用户的**任务要求**原样记成**长期偏好**——实测就是这样把
# 小说任务的三条「注意事项」收进记忆的。只有整句不像任务书时才认。
WEAK_SIGNALS = ("不要", "别用", "禁止", "必须", "一定要", "默认", "请注意")

# 任务书的特征。命中即认为这句话是「这次的任务要求」而不是「以后的长期偏好」。
#   - 分条编号（注意事项 1./2./3.）
#   - 任务类小标题词（任务/产出/纪律/注意事项…）
#   - 指向具体产出物（指定路径/该文件/素材/大纲/报告）
#   - 绝对路径或具体文件名（写作.txt、D:\...）
_BRIEF = re.compile(
    r"^\s*[\(（]?\d+[\)）.、)]"
    r"|任务|注意事项|产出|纪律|交付|请忽略|交接|已确认"
    r"|指定路径|该文件|这个文件|本文|素材|大纲|报告|全文"
    r"|[A-Za-z]:\\|/[\w.-]+/|\S+\.(txt|md|py|json|ya?ml|csv|docx?|xlsx?)\b"
    # 工具名与「我已经把东西准备好了」式的交代（实测漏过「…直接用 read_file 读，不要联网：」）
    r"|read_file|write_file|edit_file|run_command|fetch_url|search_code|list_dir"
    r"|联网|工作区里|目录里|参考文件"
    # 以冒号收尾 = 后面还要跟一列东西，是交代清单的口气，不是偏好的口气
    r"|[:：]\s*$"
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


# 模型抽取的后置过滤：MODEL_PROMPT 里写了「不要写这次的临时结果」，小模型照样犯——
# 实测把「小说《翡翠王朝》素材（D:\...\写作.txt）：唯一主线是三国穿越线…」这种
# **本次任务的结论**写进了长期记忆。判据是「这条只对那一次任务成立」：
#   - 带绝对路径 / 具体文件名 → 指向某次任务的产物
#   - 带「本次/这次/该任务/已落盘」→ 自述式的一次性结论
#   - 带「约 N 万字」「第 N 行」→ 具体素材的统计，不是可复用偏好
_TASKISH = re.compile(
    r"本次|这次|该任务|本任务|已落盘|已写入|已保存|交接"
    r"|[A-Za-z]:\\|/[\w.-]+/|\S+\.(txt|md|py|json|ya?ml|csv|docx?|xlsx?)\b"
    r"|约\s*\d+\s*[万千]字|第\s*\d+\s*行"
)

# 模型输出里的层前缀：[L1] / （L2） / 【L3】 / L1: / L1- ，大小写都认。
# 前缀只是给分层用的标记，落盘前要摘掉，不能让「[L2] 」混进记忆正文。
_LAYER_PREFIX = re.compile(r"^[\[\(（【]?\s*(L[123])\s*[\]\)）】]?\s*[:：\-–—.、]?\s*", re.I)


def _noisy(text: str) -> bool:
    """这句话是不是任务编排的过程指令（而不是值得长期保留的结论）。"""
    return bool(_NOISE.search(str(text or "")))


def _taskish(text: str) -> bool:
    """这条是不是「只对本次任务成立」的结论（不该进长期记忆）。"""
    return bool(_TASKISH.search(str(text or "")))

MODEL_PROMPT = """下面是一段智能体任务的对话记录。请只提炼**以后还用得上的结论**，例如：
用户偏好、项目约定、环境事实、踩过的坑。

每条都要标出它属于哪一层（写在该行最前面）：
- [L1] 原子事实：可精确召回的环境/路径/版本/端口/依赖等，例如「本地 Ollama 端口 11434」
- [L2] 项目约定：项目规则、工作方式、流程约定，例如「改动前先跑一遍测试」
- [L3] 用户偏好：稳定的个人偏好与习惯，例如「注释一律用中文」

规则：
- 每行一条，以「- [层] 」开头（如「- [L1] ...」），正文不超过 40 字，不要写过程、不要写这次的临时结果
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
    """规则抽取：从用户消息里挑出「要求记住」的句子。

    强信号直接收；弱信号（必须/不要这类任务书里也高发的词）还要再过一道
    「像不像任务书」的判断——不分强弱的话，用户每次贴任务要求都会往长期记忆里
    灌一条过程指令，记忆文件很快就被这次任务的内容淹掉。
    """
    out: list[str] = []
    for m in messages:
        if (m or {}).get("role") != "user":
            continue
        for s in _sentences(m.get("content") or ""):
            if not (NOTE_MIN <= len(s) <= NOTE_MAX):
                continue
            if _noisy(s):
                continue
            if any(k in s for k in SIGNALS):
                pass                      # 强信号：无条件收
            elif any(k in s for k in WEAK_SIGNALS) and not _BRIEF.search(s):
                pass                      # 弱信号：确认不像任务书才收
            else:
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


def parse_model_notes_typed(raw: str) -> list[tuple[str, str]]:
    """解析模型输出，返回 [(layer, text)]；layer 为空串表示模型没标层。

    支持「- [L1] xxx」「1. L2: xxx」或纯行，过滤明显是废话的行。
    层前缀先摘掉再判长度——「[L1] 端口 11434」真正的内容只有「端口 11434」。
    """
    out: list[tuple[str, str]] = []
    seen: list[str] = []
    for line in str(raw or "").splitlines():
        s = line.strip().lstrip("-*·").strip()
        s = re.sub(r"^\d+[.、)]\s*", "", s)
        layer = ""
        m = _LAYER_PREFIX.match(s)
        if m:
            layer = m.group(1).upper()
            s = s[m.end():].strip()
        if not s or s.upper() == "NONE":
            continue
        if len(s) < NOTE_MIN:
            continue
        if _noisy(s) or _taskish(s):
            continue
        if s in seen:
            continue
        seen.append(s)
        out.append((layer, s[:NOTE_MAX]))
        if len(out) >= MAX_MODEL:
            break
    return out


def parse_model_notes(raw: str) -> list[str]:
    """只要文本、不要层（旧调用方的兼容入口）。"""
    return [t for _, t in parse_model_notes_typed(raw)]


def model_notes(hub, spec, messages: list[dict]) -> list[tuple[str, str]]:
    """用指定模型抽一次记忆；任何异常都吞掉，返回 [(layer, text)]（可能为空）。"""
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
        return parse_model_notes_typed(getattr(result, "content", "") or "")
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
    """把规则抽取的结果写进记忆，返回新写入的条目（已去重）。

    层标签由本地规则判（classify_layer）——用户明说要记住的话，语义一眼能看出
    是偏好还是环境事实，不需要模型参与。
    """
    added: list[dict] = []
    for note in rule_notes(messages):
        if dupe_of(store, note):
            continue
        entry = store.append(note, task_id, layer=classify_layer(note))
        if entry:
            added.append(entry)
    return added


def capture_model(store, hub, spec, messages: list[dict], task_id: str = "") -> list[dict]:
    """后台线程里跑的模型抽取：同样去重后再落盘。

    优先用模型自己标出的层（[L1]/[L2]/[L3]）；没标或标了非法值，再用本地
    classify_layer 兜底，保证每条记忆都有层。
    """
    if spec is None:
        return []
    notes = model_notes(hub, spec, messages)
    if not notes:
        return []
    added: list[dict] = []
    for layer, note in notes:
        if dupe_of(store, note):
            continue
        entry = store.append(note, task_id, layer=normalize_layer(layer, note))
        if entry:
            added.append(entry)
    return added


__all__ = [
    "capture", "capture_model", "dupe_of", "model_notes", "parse_model_notes",
    "parse_model_notes_typed", "pick_extractor", "rule_notes", "classify_layer",
]
