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
from datetime import datetime
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

# 本次任务的产物名（文件名/绝对路径）。生成 skill 时这些词一律不许出现在正文里：
# skill 是给**以后同类任务**用的方法论，掺进一次性的文件名/路径，既没用又会让它
# 在无关任务上误触发（实测沉淀出的「长素材整理规程」里写着这次小说的具体踩坑）。
_ARTIFACT = re.compile(
    r"[A-Za-z0-9_\u4e00-\u9fff]+\.(?:txt|md|py|json|ya?ml|csv|docx?|xlsx?|png|jpg|jpeg)\b"
)
_ABS_PATH = re.compile(r"[A-Za-z]:\\[^\s，。；,;]*|(?:/[\w.-]+){2,}")


def task_artifacts(messages: Iterable[dict]) -> set[str]:
    """从任务原文里挑出「本次任务的产物」——文件名与绝对路径，全部小写。"""
    out: set[str] = set()
    for m in messages or []:
        if (m or {}).get("role") != "user":
            continue
        text = str(m.get("content") or "")
        out |= {x.lower() for x in _ARTIFACT.findall(text)}
        out |= {x.lower() for x in _ABS_PATH.findall(text)}
    return {x for x in out if x}


def task_residue(skill: dict, artifacts: set[str]) -> str:
    """skill 里是否混进了本次任务的专有内容？返回命中的词，干净则返回空串。"""
    blob = " ".join(
        [str(skill.get("description") or ""), str(skill.get("body") or "")]
        + [str(skill.get("when_to_use") or ""), str(skill.get("when_not_to_use") or "")]
        + [str(k) for k in (skill.get("keywords") or [])]
    )
    low = blob.lower()
    for a in artifacts:
        if a and a in low:
            return a
    m = _ABS_PATH.search(blob)
    return m.group(0) if m else ""


MODEL_PROMPT = """下面是一段智能体任务的对话记录。请判断其中是否沉淀出「可复用的通用方法论」——
即以后遇到同类任务时，能照着它一步步做的方法，而不是这次任务的临时结论。

判断标准：
- 值得沉淀：任务里形成了一套清晰的步骤 / 检查清单 / 避坑经验，能直接搬到同类任务上
- 不值得：纯本次结果、一次性数据、用户临时偏好（那类应进记忆而非 skill）

若没有可复用方法论，只输出 NONE。

若有，严格按以下格式输出（不要任何额外解释、不要代码块包裹）：

NAME: <skill 英文名，小写字母+中划线，3-20 字符，如 long-material>
DESC: <一句话中文描述，不超过 24 字>
VERSION: <版本号，如 1.0；第一次沉淀写 1.0>
WHEN: <什么场景该用这个 skill，一句话，不超过 30 字>
WHEN_NOT: <什么场景不该用，一句话；确实没有就写 NONE>
KEYWORDS: <触发词，逗号分隔的**通用概念词**，4-8 个，如 整理,总结,大纲,素材,笔记>
---
<正文：用 Markdown 分小节写「为什么 / 流程 / 注意」，给出可照做的步骤>

正文纪律（违反任何一条，整条 skill 会被丢弃）：
- 不许出现具体文件名、目录、绝对路径（如 outline.md、D:\\project\\xxx）
- 不许出现「本次 / 这次 / 刚才那次任务」这类指代，也不要复述这一次的踩坑经过
- 触发词只能是能泛化的概念词，不能是某个文件名
- WHEN / WHEN_NOT 写的是**场景特征**（如「几千字以上的长素材」），不是具体任务名

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


# 关键词里允许的字符：字母/数字/下划线/中划线/汉字。其余（逗号、冒号、括号…）
# 会破坏 frontmatter 的 YAML 流式列表，必须在落盘前剔掉。
_KW_OK = re.compile(r"^[\w\u4e00-\u9fff\-]+$")


def _field(text: str, key: str) -> str:
    """取一行 `KEY: 值` 的字段值。逐行锚定，避免正文里出现的同名词被误取。"""
    m = re.search(rf"^\s*{key}\s*[:：]\s*(.+)$", text, re.M | re.I)
    return m.group(1).strip() if m else ""


def _yaml_str(value: str) -> str:
    """把一段文本安全地写成 YAML 字符串（双引号包裹 + 转义），防止值里的
    冒号/引号破坏 frontmatter，导致整条 skill 的元数据解析失败。"""
    s = " ".join(str(value or "").split())
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_model_skill(raw: str) -> dict | None:
    """解析模型输出的 skill 草稿。

    返回 {name, description, version, when_to_use, when_not_to_use, keywords, body}；
    缺少 name 或正文时返回 None（宁可这次不沉淀，也不落一个残缺技能）。
    """
    if not raw or str(raw).strip().upper() == "NONE":
        return None
    text = str(raw)
    raw_name = _field(text, "NAME")
    name = _slug(raw_name) if raw_name else ""
    desc = _field(text, "DESC")
    version = _field(text, "VERSION") or "1.0"
    when_use = _field(text, "WHEN")
    when_not = _field(text, "WHEN_NOT")
    if when_not.strip().upper() == "NONE":
        when_not = ""
    keywords: list[str] = []
    kw_raw = _field(text, "KEYWORDS")
    if kw_raw:
        keywords = [k.strip() for k in re.split(r"[，,、\s]+", kw_raw) if k.strip()]
        # 触发词必须是能泛化的概念词。模型偶尔会把本次的文件名/路径/含标点的
        # 短语塞进来（如 outline.md、写作.txt），那种词只会让 skill 在无关任务上误命中。
        keywords = [k for k in keywords
                    if not _ARTIFACT.fullmatch(k) and not _ABS_PATH.search(k)
                    and _KW_OK.match(k)]
    # body = 头字段之后、--- 分隔之后的部分；没写 --- 就把开头的字段行全剥掉
    m = re.search(r"^---\s*$(.*)", text, re.S | re.M)
    if m:
        body = m.group(1).strip()
    else:
        body = re.sub(r"^(?:[A-Za-z_]+\s*[:：].*\n)+", "", text).strip()
    if not name or len(name) < 2:
        return None
    if not body:
        return None
    if not desc:
        desc = name
    return {
        "name": name,
        "description": desc,
        "version": version,
        "when_to_use": when_use,
        "when_not_to_use": when_not,
        "keywords": keywords,
        "body": body,
    }


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
    """写 SKILL.md（frontmatter + 正文），re-scan 注册表，返回新 skill 信息。

    frontmatter 里带上资产元数据：版本、触发边界（适用/不适用）、来源与提取时间。
    这些字段会被 SkillRegistry 解析，用于后续的匹配与展示。
    """
    name = skill["name"]
    folder = learned / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    kw = ",".join(skill.get("keywords") or [])
    ver = _yaml_str(skill.get("version") or "1.0")
    when_use = " ".join(str(skill.get("when_to_use") or "").split())
    when_not = " ".join(str(skill.get("when_not_to_use") or "").split())
    fields = [
        "---",
        f"name: {name}",
        f"description: {_yaml_str(skill.get('description', name))}",
        f"version: {ver}",
    ]
    if when_use:
        fields.append(f"when_to_use: {_yaml_str(when_use)}")
    if when_not:
        fields.append(f"when_not_to_use: {_yaml_str(when_not)}")
    fields += [
        f"keywords: [{kw}]",
        "auto_learned: true",
        f"learned_at: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "---",
    ]
    front = (
        "\n".join(fields) + "\n\n"
        f"> 自动沉淀于任务 {task_id or '?'}（AgentForge 自检机制）。"
        f"如不再适用可随时在「技能管理 → 提取得到的技能」里删除。\n\n"
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
    # 最后一道闸：正文/描述/触发词里混进本次任务的文件名或绝对路径，就整条丢弃。
    # 宁可这次没学到东西，也不要沉淀出一个带一次性细节、还会到处误触发的技能。
    residue = task_residue(skill, task_artifacts(messages))
    if residue:
        return []
    made = write_skill(registry, learned, skill, task_id)
    return [made] if made else []


__all__ = [
    "capture_model", "detect_explicit", "dupe_of", "learned_dir",
    "model_skill", "parse_model_skill", "pick_extractor", "task_artifacts",
    "task_residue", "write_skill",
]
