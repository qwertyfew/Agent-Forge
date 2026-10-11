"""Skill 系统：按 Agent Skills 开放标准实现的三级渐进式加载。

标准格式（agentskills.io）::

    my-skill/
    ├── SKILL.md          # YAML frontmatter(name/description) + 正文
    ├── scripts/          # 可执行代码
    ├── references/       # 按需加载的深度文档
    └── assets/           # 模板与静态资源

三级加载（progressive disclosure）:

    Tier 1 发现   启动时只加载 name + description，约 50~100 tokens/个
    Tier 2 激活   任务匹配时才读入 SKILL.md 正文
    Tier 3 资源   需要细节时才读 references/ 里的文件

这样装几十个 skill 也不会撑爆上下文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# frontmatter 分隔符：文件必须以 --- 开头
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass
class Skill:
    """一个已发现的 skill（Tier 1 状态，正文尚未读入）。"""

    name: str
    description: str
    dirpath: Path
    body: str = ""
    _loaded: bool = False
    _keywords: list[str] = field(default_factory=list)

    # ---- Tier 2：按需读入正文 ----
    def load(self) -> str:
        if not self._loaded:
            try:
                self.body = self.dirpath.joinpath("SKILL.md").read_text(
                    encoding="utf-8", errors="replace"
                )
                _, self.body = split_frontmatter(self.body)
            except OSError:
                self.body = ""
            self._loaded = True
        return self.body

    # ---- Tier 3：按需读入附带资源 ----
    def resources(self) -> list[str]:
        out: list[str] = []
        for sub in ("references", "scripts", "assets"):
            d = self.dirpath / sub
            if d.is_dir():
                for p in sorted(d.rglob("*")):
                    if p.is_file():
                        out.append(p.relative_to(self.dirpath).as_posix())
        return out

    def read_resource(self, rel: str) -> str:
        target = (self.dirpath / rel).resolve()
        root = self.dirpath.resolve()
        # 防目录穿越
        if not str(target).startswith(str(root)):
            raise ValueError(f"资源路径越界: {rel}")
        if not target.is_file():
            raise FileNotFoundError(f"资源不存在: {rel}")
        return target.read_text(encoding="utf-8", errors="replace")

    def instructions(self) -> str:
        """Tier 2 正文全量 + 可用资源清单。"""
        body = self.load()
        res = self.resources()
        head = f"[skill: {self.name}]\n"
        if res:
            head += "可用资源（需要时用 read_skill_resource 读取）：\n"
            head += "\n".join(f"  - {r}" for r in res[:30]) + "\n"
        return head + "\n" + body

    def brief(self, max_chars: int = 1600) -> str:
        """Tier 2 精简版：只注入正文开头若干个**完整小节**，附上取全文的指引。

        自动命中时用它，避免一个 8.7K tokens 的大 skill 把 64K 窗口直接吃掉 14%。
        按小节切而不是硬截断：半句话的指令比没有指令更坏。
        """
        body = self.load()
        head = f"[skill: {self.name}]\n"
        res = self.resources()
        if res:
            head += "可用资源（需要时用 read_skill_resource 读取）：\n"
            head += "\n".join(f"  - {r}" for r in res[:30]) + "\n"
        if len(body) <= max_chars:
            return head + "\n" + body
        cut = _cut_at_section(body, max_chars)
        return (
            head + "\n" + body[:cut].rstrip()
            + f"\n\n…（正文共 {len(body)} 字，此处只注入了前 {cut} 字。）"
            + f"需要完整步骤与细节时，调用 use_skill('{self.name}') 加载全文。"
        )


def _cut_at_section(body: str, limit: int) -> int:
    """在 limit 附近找一个标题边界切，避免把某个小节拦腰截断。

    先往后多看 40% 找最近的标题行；找不到就往回收，最少保留 limit 的一半。
    """
    window = int(limit * 1.4)
    best = 0
    for m in re.finditer(r"^#{1,4} .*$", body[:window], re.M):
        if m.start() <= limit:
            best = m.start()
        else:
            break
    if best <= 0:
        return max(int(limit * 0.5), min(limit, len(body)))
    return best


def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 YAML frontmatter 与正文。解析失败时返回空元数据。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    return meta, m.group(2)


_ASCII_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-\.]{1,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> set[str]:
    """英文按单词切，中文按 2-gram 切。

    中文没有空格，整块汉字当成一个词的话，"帮我写一个" 和 "写一个函数"
    会被判成完全不同——实际它们共享 "写一"。2-gram 能救回这类重叠。
    """
    out: set[str] = set()
    for m in _ASCII_RE.finditer(text or ""):
        out.add(m.group().lower())
    for m in _CJK_RE.finditer(text or ""):
        seg = m.group()
        if len(seg) == 1:
            out.add(seg)
        else:
            out.update(seg[i : i + 2] for i in range(len(seg) - 1))
    return out


class SkillRegistry:
    """扫描若干目录，维护 skill 清单并按任务匹配。"""

    def __init__(self, dirs: list[Path]):
        self.dirs = [Path(d) for d in dirs]
        self.skills: dict[str, Skill] = {}
        self.scan()

    def scan(self) -> None:
        self.skills.clear()
        for base in self.dirs:
            if not base.is_dir():
                continue
            for md in sorted(base.rglob("SKILL.md")):
                self._register(md)

    def _register(self, md: Path) -> None:
        try:
            raw = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        # 过深的嵌套多半是示例或依赖，跳过
        depth = len(md.parent.relative_to(md.parent.parent).parts) if md.parent.parent else 0
        if depth > 1:
            return
        meta, _ = split_frontmatter(raw)
        name = str(meta.get("name") or md.parent.name).strip()
        desc = str(meta.get("description") or "").strip()
        if not desc:
            desc = md.parent.name
        sk = Skill(name=name, description=desc, dirpath=md.parent)
        # 英文 description 对中文提问天然召回率低，所以额外支持
        # frontmatter 里的 keywords（可写中文触发词）作为补充信号
        extra = meta.get("keywords") or meta.get("keywords_zh") or []
        if isinstance(extra, str):
            extra = [extra]
        sk._keywords = sorted(
            tokenize(f"{name} {desc}") | tokenize(" ".join(str(x) for x in extra))
        )
        # 同名以先扫到的为准（前面的目录优先级更高）
        self.skills.setdefault(name, sk)

    # ---------- Tier 1 ----------
    def catalog(self) -> str:
        """给系统提示用的清单：只有名字和一句话描述。"""
        if not self.skills:
            return ""
        lines = ["可用 skill（判断相关时用 use_skill 加载，不要凭猜测直接动手）："]
        for s in self.skills.values():
            d = s.description.replace("\n", " ")
            if len(d) > 160:
                d = d[:157] + "..."
            lines.append(f"  - {s.name}: {d}")
        return "\n".join(lines)

    def names(self) -> list[str]:
        return list(self.skills)

    # ---------- 匹配 ----------
    def match(self, user_text: str, top_k: int = 3) -> list[tuple[Skill, int]]:
        """按关键词重合度给 skill 打分。命中太少就返回空，避免乱加载。"""
        q = tokenize(user_text)
        if not q:
            return []
        scored: list[tuple[Skill, int]] = []
        for s in self.skills.values():
            if not s._keywords:
                continue
            hit = len(q & set(s._keywords))
            if hit:
                scored.append((s, hit))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def auto_hint(self, user_text: str, min_score: int = 2,
                  max_chars: int = 1600) -> str:
        """自动提示：匹配度够高时，注入该 skill 正文的**精简版**。

        注入摘要而不是全文——最长的一个 skill 正文 8.7K tokens，命中即吃掉
        小窗口模型 14% 的上下文。摘要里带上 use_skill 的指引，模型真需要
        完整步骤时会自己取。返回还带上被命中的 skill 名，方便上层上报事件。
        """
        hits = [(s, sc) for s, sc in self.match(user_text) if sc >= min_score]
        if not hits:
            return ""
        s, _ = hits[0]           # 自动加载只取最匹配的一个，避免撑爆上下文
        return s.brief(max_chars)


def default_dirs(root: Path, extra: list[str] | None = None) -> list[Path]:
    """skill 搜索目录：项目级优先，其次用户级。"""
    dirs = [root / "skills"]
    home = Path.home() / ".agentforge" / "skills"
    dirs.append(home)
    for e in extra or []:
        dirs.append(Path(e))
    return dirs
