"""任务结束后自动沉淀 skill。

记忆沉淀（autonote）管「以后还用得上的结论/偏好」，skill 沉淀管「以后同类任务
能照着做的方法论」。两者都走「任务收尾 → 两级抽取 → 落盘」的结构，但产物不同：
记忆是一条文本，skill 是一个符合 Agent Skills 标准的 SKILL.md（frontmatter +
正文），会被 SkillRegistry 自动发现、在后续同类任务命中时注入上下文。

设计原则（与用户「不要为单次任务打全局补丁」的要求一致）：

- 只沉淀**可复用的通用方法论**（如「长材料先清单后成文」），不沉淀某次任务的
  临时结论；模型抽取时明确要求「不是这次的临时结果」。
- 自动产出的 skill 统一落在 skills/learned/ 下，和用户手写的 skills/ 分开，便于
  整批查看、删除、清空，也不污染用户精心维护的技能目录。
- 每条都要和现有 skill 去重：同名或关键词高度重合就跳过，避免同一个方法论被
  反复写成多个文件。
- 写完后立刻 re-scan 注册表，让它能马上参与后续任务的自动匹配。

规则抽取（即时、不花算力）：用户明说「做成 skill / 记住这个做法 / 以后这类任务
用它」时，把那句话作为种子交给模型提炼；模型抽取（后台线程、最便宜本地模型）
则自主判断任务里是否浮现可复用方法。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .config import Config

# 用户明确表达「这条要做成 skill」的信号词
SIGNALS = (
    "做成skill", "做成技能", "沉淀为skill", "沉淀为技能", "固化为skill", "固化为技能",
    "形成skill", "形成技能", "总结成skill", "记住这个做法", "记住这个套路",
    "以后这类任务", "以后都用这个方法", "做成方法论", "沉淀方法论",
)

NOTE_MIN = 8
MAX_RULES = 2        # 规则抽取最多取 2 条显式请求作为种子
MAX_MODEL = 1        # 模型抽取每轮最多产 1 个 skill（多了容易灌水）

_SENT_SPLIT = re.compile(r"[。！？!?\n；;]+")

# 多步任务的编排提示词（STEP_FIRST/STEP_NEXT）作为 user 消息进历史，里头带着
# 「不要越界」「只完成本步」这类词，会被当成用户要求抓下来——必须挡掉，
# 否则会沉淀出「请只完成这一步」这种毫无价值的过程指令。
_NOISE = re.compile(
    r"本步|这一步|越界|不要重复|已完成的操作|凭空猜测|"
    r"按步骤|逐步完成|做完后用|落脚点|在此基础上继续|只完成|总体任务"
)

MODEL_PROMPT = """下面是一段智能体任务的对话记录。请判断其中是否沉淀出「可复用的通用方法论」——
即以后遇到同类任务时，能照着它一步步做的方法，而不是这次任务的临时结论。

判断标准：
- 值得沉淀：任务里形成了一套清晰的步骤 / 检查清单 / 避坑经验，能直接搬到同类任务上
- 不值得：纯本次结果、一次性数据、用户临时偏好（那类应进记忆而非 skill）

若没有可复用方法论，只输出 NONE。

若有，严格按以下格式输出（不要任何额外解释、不要代码块包裹）：

NAME: <skill 英文名，小写字母+中划线，3-20 字符，如 long-material>
DESC: <一句话中文描述，不超过 24 字>
KEYWORDS: <触发词，逗号分隔的中文词，4-8 个，如 整理,总结,大纲,素材,笔记>
---
<正文：用 Markdown 分小节写「为什么 / 流程 / 注意」，给出可照做的步骤；不要写这次任务的专有内容>

{focus}
"""


def _noisy(text: str) -> bool:
    return bool(_NOISE.search(str(text or "")))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(str(text or "")) if s.strip()]


def learned_dir(cfg: Config) -> Path:
    """自动沉淀的 skill 统一目录：和用户手写的 skills/ 隔离。"""
    if cfg.skill_dirs:
        base = Path(cfg.skill_dirs[0])
    else:
        base = Path(cfg.workspace_root) / "skills"
    d = base / "learned"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def detect_explicit(messages: Iterable[dict]) -> list[str]:
    """规则抽取：找出用户明说要做成 skill 的句子（作为模型提炼的种子）。"""
    out: list[str] = []
    for m in messages:
        if (m or {}).get("role") != "user":
            continue
        for s in _sentences(m.get("content") or ""):
            if len(s) < NOTE_MIN:
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


def _transcript(messages: list[dict], per_msg: int = 900, limit: int = 10) -> str:
    """把对话压成一段短文本喂给模型：取尾部若干轮，每轮截断。"""
    tail = [m for m in (messages or []) if (m or {}).get("role") in ("user", "assistant")]
    lines: list[str] = []
    for m in tail[-limit:]:
        role = "用户" if m["role"] == "user" else "助手"
        text = " ".join(str(m.get("content") or "").split())
        if not text:
            continue
        lines.append(f"{role}：{text[:per_msg]}")
    return "\n".join(lines)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "skill"


def parse_model_skill(raw: str) -> dict | None:
    """解析模型输出的 skill 草稿，返回 {name, description, keywords, body} 或 None。"""
    if not raw or str(raw).strip().upper() == "NONE":
        return None
    text = str(raw)
    name = desc = None
    keywords: list[str] = []
    body = text
    m = re.search(r"NAME:\s*(.+)", text)
    if m:
        name = _slug(m.group(1).strip().split("\n")[0])
    m = re.search(r"DESC:\s*(.+)", text)
    if m:
        desc = m.group(1).strip().split("\n")[0].strip()
    m = re.search(r"KEYWORDS:\s*(.+)", text)
    if m:
        kw = m.group(1).strip().split("\n")[0]
        keywords = [k.strip() for k in re.split(r"[，,、\s]+", kw) if k.strip()]
    # body = NAME/DESC/KEYWORDS 之后、--- 分隔之后的部分
    m = re.search(r"---\s*\n(.*)", text, re.S)
    if m:
        body = m.group(1).strip()
    elif name or desc:
        # 没写 --- 分隔：去掉前三行头
        body = re.sub(r"^(NAME:.*\n)?(DESC:.*\n)?(KEYWORDS:.*\n)?", "", text).strip()
    if not name or len(name) < 2:
        return None
    if not body:
        return None
    if not desc:
        desc = name
    return {"name": name, "description": desc, "keywords": keywords, "body": body}


def model_skill(hub, spec, messages: list[dict], seed: str = "") -> dict | None:
    """用指定模型判断/提炼一个 skill；任何异常都吞掉，返回 None。"""
    text = _transcript(messages)
    if not text:
        return None
    focus = ""
    if seed:
        focus = f"用户明确要求沉淀为 skill，请围绕这句话提炼方法论：{seed}"
    msgs = [
        {"role": "system", "content": MODEL_PROMPT.format(focus=focus)},
        {"role": "user", "content": text},
    ]
    try:
        result = None
        for kind, payload in hub.chat_stream(spec, msgs, None, {"temperature": 0.2}):
            if kind == "result":
                result = payload
        return parse_model_skill(getattr(result, "content", "") or "")
    except Exception:  # noqa: BLE001
        return None


def dupe_of(registry, skill: dict) -> str:
    """这条 skill 是否已存在？返回已存在的 skill 名，没有则返回空串。

    按「同名」或「关键词高度重合」判重：同一个方法论被反复写成多个文件既占地方
    又会在任务里重复命中、互相干扰。
    """
    name = (skill.get("name") or "").lower()
    kws = set(skill.get("keywords") or [])
    for sk in (registry.skills or {}).values():
        if sk.name.lower() == name:
            return sk.name
        if kws and sk._keywords:
            overlap = len(kws & set(sk._keywords))
            if overlap >= 3 and overlap >= max(1, len(kws) // 2):
                return sk.name
    return ""


def write_skill(registry, learned: Path, skill: dict, task_id: str = "") -> dict | None:
    """写 SKILL.md（frontmatter + 正文），re-scan 注册表，返回新 skill 信息。"""
    name = skill["name"]
    folder = learned / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    kw = ",".join(skill.get("keywords") or [])
    front = (
        f"---\n"
        f"name: {name}\n"
        f"description: {skill.get('description', name)}\n"
        f"keywords: [{kw}]\n"
        f"auto_learned: true\n"
        f"---\n\n"
        f"> 自动沉淀于任务 {task_id or '?'}（AgentForge 自检机制）。"
        f"如不再适用可随时在「技能管理 → 已学习」里删除。\n\n"
        f"{skill.get('body', '').strip()}\n"
    )
    path = folder / "SKILL.md"
    try:
        path.write_text(front, encoding="utf-8")
    except OSError:
        return None
    try:
        registry.scan()
    except Exception:  # noqa: BLE001
        pass
    return {"name": name, "path": str(path), "description": skill.get("description", name)}


def pick_extractor(store, cfg) -> Any:
    """挑最便宜可用的**本地**模型做抽取；付费模型不参与，避免偷偷花钱。"""
    try:
        entries = [e for e in store.usable() if getattr(e, "kind", "local") == "local"]
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


def capture_model(registry, hub, spec, messages: list[dict], task_id: str = "",
                  cfg: Config | None = None, learned: Path | None = None) -> list[dict]:
    """后台线程里跑的 skill 抽取：规则种子 + 模型提炼，去重后落盘。"""
    if spec is None or not messages:
        return []
    learned = learned or (learned_dir(cfg) if cfg else None)
    if learned is None:
        return []
    seeds = detect_explicit(messages)
    # 显式要求时强制提炼（即便模型自由判断觉得不值得）；否则让模型自主判断
    skill = None
    if seeds:
        for seed in seeds:
            skill = model_skill(hub, spec, messages, seed)
            if skill:
                break
    if skill is None:
        skill = model_skill(hub, spec, messages)
    if not skill:
        return []
    if dupe_of(registry, skill):
        return []
    made = write_skill(registry, learned, skill, task_id)
    return [made] if made else []


__all__ = [
    "capture_model", "detect_explicit", "dupe_of", "learned_dir",
    "model_skill", "parse_model_skill", "pick_extractor", "write_skill",
]
