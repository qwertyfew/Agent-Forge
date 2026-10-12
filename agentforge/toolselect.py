"""工具按需下发（progressive tool disclosure）。

问题：35 个工具的 JSON schema 实测 ≈ 4,100 tokens，而它是**每一轮请求**都要
重发一遍的固定开销。对 64K 窗口的小模型来说这直接吃掉 6.5%，而其中绝大多数
工具当轮根本用不上（只在写小说时才用 novel_*，只在运维时才用 ssh_*）。

三级渐进式下发，和 skill 的思路一致：

    Tier 1 索引   系统提示里只列**工具名**（约 150 tokens），说明"遇到没有的
                  工具，先用 find_tools 查"
    Tier 2 下发   核心工具 + 按任务关键词匹配到的工具，带完整 schema
    Tier 3 按需   find_tools("git 提交") 现取 schema，取到后粘住后续几轮

失效安全：没被下发的工具不代表不能调 —— 在 Ollama 路径下模型若真调用了未下发
的工具名，registry 仍然能执行（只是参数得靠它自己猜）。find_tools 保证模型
始终知道"有哪些能力存在、怎么取用法"，这才是关键。

关掉这套（config: tools.lazy=false）就退回"每轮全量下发"的老行为。
"""

from __future__ import annotations

import json
import math
from typing import Iterable

from .skills import tokenize
from .tools.base import SAFE, make_tool

# 无论什么任务都值得常驻的工具：读写文件、跑命令、找文件、查记忆、记笔记、取技能用法。
# 少一个都会让基本流程退化成"先查一次工具再干活"，得不偿失。
DEFAULT_CORE: tuple[str, ...] = (
    "list_dir",
    "find_files",
    "read_file",
    "write_file",
    "edit_file",
    "search_code",
    "run_command",
    "remember",
    "task_notes",
    "find_tools",
    "list_skills",
    "use_skill",
    "read_skill_resource",
)


def _param_tokens(schema: dict) -> set[str]:
    """把参数表里的名字与描述也当成关键词来源。

    例：browser_screenshot 的参数里有 url / path，"截图网页"这类中文提问
    靠描述匹配，而 "screenshot url"这种英文提问就得靠参数名。
    """
    params = (schema.get("function") or {}).get("parameters") or {}
    out: set[str] = set()
    for name, spec in (params.get("properties") or {}).items():
        out |= tokenize(name.replace("_", " "))
        if isinstance(spec, dict):
            out |= tokenize(str(spec.get("description") or ""))
            enum = spec.get("enum")
            if isinstance(enum, list):
                out |= tokenize(" ".join(str(x) for x in enum))
    return out


class ToolSelector:
    """按任务文本挑工具，并把「本轮该下发哪些 schema」算出来。"""

    def __init__(self, registry, core: Iterable[str] | None = None,
                 limit: int = 16, min_score: float = 3.0, lazy: bool = True):
        self.reg = registry
        self.lazy = bool(lazy)
        self.limit = max(4, int(limit))
        self.min_score = float(min_score)
        # IDF 表：命中"少见词"（合并、截图、上传）比命中"大众词"（文件、存在）
        # 更能说明相关。按词频把重合度加权，阈值才好定。
        self._df: dict[str, int] = {}
        self._df_n = -1
        self._uni: dict[str, set[str]] = {}      # 单字关键词（只在兜底检索里用）
        wanted = list(core) if core else list(DEFAULT_CORE)
        # 只保留真实存在的（用户可能删了某个工具 / 插件没装上）
        self.core = [n for n in wanted if registry.get(n)]
        if "find_tools" in registry.names() and "find_tools" not in self.core:
            self.core.append("find_tools")
        self._kw: dict[str, set[str]] = {}
        self._first: dict[str, str] = {}

    # ---------- 关键词 ----------
    def _keywords(self, name: str) -> set[str]:
        cached = self._kw.get(name)
        if cached is not None:
            return cached
        tool = self.reg.get(name)
        if tool is None:
            return set()
        schema = tool.schema()
        kw = tokenize(name.replace("_", " "))
        kw |= tokenize(tool.description or "")
        kw |= _param_tokens(schema)
        self._kw[name] = kw
        self._first[name] = (tool.description or "").strip().splitlines()[0][:80] if tool.description else ""
        return kw

    def _one_liner(self, name: str) -> str:
        if name not in self._first:
            self._keywords(name)
        return self._first.get(name, "")

    # ---------- 相关度打分（IDF 加权）----------
    def _idf_table(self) -> None:
        names = self.reg.names()
        if self._df_n == len(names):
            return
        df: dict[str, int] = {}
        for n in names:
            for t in self._keywords(n):
                df[t] = df.get(t, 0) + 1
        self._df = df
        self._df_n = len(names)

    def _unigram_kw(self, name: str) -> set[str]:
        """工具描述里的中文单字集合（兜底检索专用）。"""
        got = self._uni.get(name)
        if got is not None:
            return got
        tool = self.reg.get(name)
        text = ((tool.description or "") + (tool.name or "")) if tool else ""
        self._uni[name] = {ch for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF}
        return self._uni[name]

    def score(self, q: set[str], name: str) -> float:
        """q 与某工具关键词的重合度，按词的稀有度加权。"""
        if not q:
            return 0.0
        self._idf_table()
        n = self._df_n or 1
        total = 0.0
        for t in q & self._keywords(name):
            total += math.log((n + 1) / (self._df.get(t, 0) + 1)) + 0.2
        return total

    def ranked(self, query: str, min_score: float | None = None,
               exclude: Iterable[str] = ()) -> list[tuple[float, str]]:
        q = tokenize(query or "")
        if not q:
            return []
        th = self.min_score if min_score is None else float(min_score)
        skip = set(exclude)
        rows = []
        for n in self.reg.names():
            if n in skip:
                continue
            s = self.score(q, n)
            if s >= th:
                rows.append((round(s, 2), n))
        rows.sort(key=lambda x: (-x[0], x[1]))
        return rows

    def best_effort(self, query: str, limit: int = 3,
                    exclude: Iterable[str] = ()) -> list[tuple[float, str]]:
        """放宽版检索：不设阈值的兜底。

        中文二字词切分天生会漏——"合并书稿"和"合成全书"一个二元组都不重合。
        所以兜底时把单字也算进来（权重压低），宁可给个"可能相关"的，也不要
        回一句"没找到"让模型卡住。
        """
        text = query or ""
        q = tokenize(text) | {ch for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF}
        if not q:
            return []
        self._idf_table()
        n_tools = self._df_n or 1
        skip = set(exclude)
        rows = []
        for n in self.reg.names():
            if n in skip:
                continue
            kw = self._keywords(n) | self._unigram_kw(n)
            s = 0.0
            for t in q & kw:
                w = 0.45 if len(t) == 1 else 1.0
                s += w * (math.log((n_tools + 1) / (self._df.get(t, 0) + 1)) + 0.2)
            if s > 0:
                rows.append((round(s, 2), n))
        rows.sort(key=lambda x: (-x[0], x[1]))
        return rows[:limit]

    # ---------- Tier 1：索引 ----------
    def index_text(self, exclude: Iterable[str] = ()) -> str:
        """系统提示里那段"没有下发说明的工具清单"，按前缀分组压缩体积。

        git_add / git_commit / git_push → `git(add/commit/push)`
        实测 35 个工具压缩后只要一百多 token，比逐个列一行描述省得多。
        """
        if not self.lazy:
            return ""              # 全量下发模式下没有"未展开"的工具
        skip = set(self.core) | set(exclude)
        names = sorted(n for n in self.reg.names() if n not in skip)
        if not names:
            return ""
        grouped: dict[str, list[str]] = {}
        singles: list[str] = []
        for n in names:
            head, _, tail = n.partition("_")
            if tail and any(m != n and m.startswith(head + "_") for m in names):
                grouped.setdefault(head, []).append(tail)
            else:
                singles.append(n)
        parts = [f"{k}({'/'.join(v)})" for k, v in grouped.items()]
        parts += singles
        return "、".join(parts)

    # ---------- Tier 2/3：下发 ----------
    def core_schemas(self) -> list[dict]:
        """常驻部分（后端估算固定开销要用）。lazy 关闭时就是全量。"""
        if not self.lazy:
            return self.reg.schemas()
        out = []
        for n in self.core:
            t = self.reg.get(n)
            if t is not None:
                out.append(t.schema())
        return out

    def select(self, texts: Iterable[str], sticky: Iterable[str] = ()) -> list[dict]:
        """挑出本轮要下发的工具 schema。

        texts  用来做关键词匹配的文本（用户这轮的提问 + 最近几轮对话）
        sticky 一定要带上的工具名（本轮已经调用过的、模型主动 find_tools 要过的）
        """
        if not self.lazy:
            return self.reg.schemas()
        picked: list[str] = list(self.core)
        for n in sticky:
            if n and n not in picked and self.reg.get(n):
                picked.append(n)

        q: set[str] = set()
        for t in texts:
            q |= tokenize(t or "")
        if q:
            scored: list[tuple[float, str]] = []
            for n in self.reg.names():
                if n in picked:
                    continue
                s = self.score(q, n)
                if s >= self.min_score:
                    scored.append((s, n))
            # 分高的先来；同分按名字排序保证结果稳定（利于 KV 前缀缓存命中）
            scored.sort(key=lambda x: (-x[0], x[1]))
            room = max(0, self.limit - len(picked))
            picked += [n for _, n in scored[:room]]

        out = []
        for n in picked:
            tool = self.reg.get(n)
            if tool is not None:
                out.append(tool.schema())
        return out

    def match(self, query: str, limit: int = 6, min_score: float | None = None,
              exclude: Iterable[str] = ()) -> list[tuple[float, str]]:
        """find_tools 的检索：返回 (得分, 工具名)，按分排序。"""
        return self.ranked(query, min_score=min_score, exclude=exclude)[:limit]

    def describe_many(self, names: list[str]) -> str:
        """给 find_tools 用：把若干工具的完整用法摊开成文本。"""
        blocks = []
        for n in names:
            tool = self.reg.get(n)
            if tool is None:
                continue
            params = json.dumps(tool.parameters or {}, ensure_ascii=False, separators=(",", ":"))
            blocks.append(f"### {n}\n{tool.description.strip()}\n参数：{params}")
        return "\n\n".join(blocks)


def build_select_tools(holder: dict, lazy: bool = True):
    """find_tools：模型主动索取某个工具的完整说明。

    holder 是 build_registry 里的可变引用，执行期才填入 selector，
    这样这个工具能和主循环共用同一份工具选择器（不用重复建缓存）。
    """

    def find_tools(query: str, limit: int = 5) -> str:
        sel: ToolSelector | None = holder.get("selector")
        reg = holder.get("registry")
        if sel is None or reg is None:
            return "[错误] 工具选择器尚未就绪"
        limit = max(1, min(int(limit or 5), 12))
        # 不允许把 find_tools 自己检索出来（它的描述里带着示例关键词，会一直自命中）
        picks = sel.match(query, limit=limit, exclude=("find_tools",))
        head = "已启用以下工具（后续轮次会持续下发完整说明，现在就可以直接调用）：\n"
        if not picks:
            # 放宽一档再试一次：模型给的关键词可能偏泛
            picks = sel.match(query, limit=limit, min_score=sel.min_score * 0.5,
                              exclude=("find_tools",))
        if not picks:
            picks = sel.best_effort(query, limit=min(limit, 3), exclude=("find_tools",))
            head = "没有精确匹配，下面这些可能相关（不确定，请核对描述后再用）：\n"
        if not picks:
            idx = sel.index_text()
            return ("没有匹配到工具。当前可用的工具清单（直接用名字调用即可）：\n"
                    + idx + "\n换一个关键词再试，或直接用上面的工具名。")
        names = [n for _, n in picks]
        # 粘住：取过用法的工具，后续几轮继续下发完整 schema
        pinned = getattr(reg, "pinned", None)
        if isinstance(pinned, set):
            for n in names:
                pinned.add(n)
        return head + sel.describe_many(names)

    return [
        make_tool(
            "find_tools",
            "查找某个工具 / 某项能力的用法。当你要做一件事、但在已下发的工具说明里"
            "找不到合适工具时，先用它按关键词查（例如 \"git 提交\"、\"网页截图\"、"
            "\"ssh 上传文件\"）。查询返回的工具即可直接调用。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词，写工具名或你要做的事"},
                    "limit": {"type": "integer", "description": "最多返回几个，默认 5"},
                },
                "required": ["query"],
            },
            find_tools,
            SAFE,
        )
    ]


__all__ = ["ToolSelector", "build_select_tools", "DEFAULT_CORE"]
