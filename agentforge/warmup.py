"""任务开局预热注入（第 58 步）。

问题：每个员工 agent 开局对工作区一无所知——目录结构、README、哪个文件在哪，
全靠自己第一轮摸黑 list_dir / read_file 探出来。一次探索就是 1~3 轮工具调用，
一个任务 N 个员工就重复 N 遍；小模型还常在开局摸错方向。

方案：任务开局一次性扫描工作区，生成一份「项目速览」块：
    1. 目录树（两层，剪掉依赖/产物等噪音目录）
    2. README 摘要（项目是干什么的）
    3. 关键词命中文件清单（按任务原文的词切出 token，对相对路径打分取 Top N）
注入**员工 agent 的系统提示**而不是用户消息——`Agent._compact` 只压历史、
永不碰系统提示，预热块和任务笔记一样处在「免压缩区」，开局给的地图到收尾
都完整在场，不存在「压缩掉预热」的问题。

边界：只扫 workspace_root 以内；只看路径与文件名，不读文件内容（README
除外），整个构建在几十毫秒量级，不增加模型调用。内容体量由 warmup.max_chars
封顶（默认 3500 字 ≈ 1~2K token，占 gemma 256K 窗口不到 1%）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .evidence import anchor_lines

# 树与关键词扫描都跳过的目录：依赖、缓存、运行期状态、历史产出
PRUNE_DIRS = {
    ".git", ".agentforge", ".sessions", "__pycache__", "node_modules",
    ".workbuddy", ".run", ".venv", "venv", "dist", "build", ".idea",
    ".vscode", ".pytest_cache", ".mypy_cache", "debug", "temp", "tmp",
    "history",
}

_ASCII_RX = re.compile(r"[a-zA-Z0-9_\-\.]{2,}")
_CJK_RX = re.compile(r"[\u4e00-\u9fff]+")

# 任务原文里的高频泛词：命中它们等于没命中，不参与打分
_STOP_TOKENS = {
    "帮我", "请帮", "一下", "这个", "那个", "任务", "项目", "文件", "代码",
    "修改", "实现", "检查", "看看", "然后", "需要", "进行", "使用", "可以",
    "the", "and", "for", "with", "this", "that", "from", "into", "文件",
}


def _task_tokens(text: str) -> set[str]:
    """把任务原文切成匹配 token：ASCII 词（含 .py 这类带点的）+ 中文二字组。"""
    toks: set[str] = set()
    for m in _ASCII_RX.finditer(text):
        t = m.group(0).lower().strip(".-")
        if len(t) >= 2 and t not in _STOP_TOKENS:
            toks.add(t)
    for m in _CJK_RX.finditer(text):
        s = m.group(0)
        for i in range(max(1, len(s) - 1)):
            bg = s[i:i + 2]
            if bg not in _STOP_TOKENS:
                toks.add(bg)
    return toks


def _walk(root: Path, *, max_entries: int = 4000):
    """带剪枝的迭代遍历，产出 (相对路径, 是否目录)。条数封顶防失控。"""
    n = 0
    for cur, dirs, files in root.walk(top_down=True):
        dirs[:] = sorted(d for d in dirs if d not in PRUNE_DIRS
                         and not d.startswith("."))
        rel = cur.relative_to(root)
        if rel != Path("."):
            yield rel, True
            n += 1
        for f in sorted(files):
            if f.startswith("."):
                continue
            yield rel / f, False
            n += 1
        if n >= max_entries:
            return


def _tree_lines(root: Path, max_lines: int = 60) -> list[str]:
    """两层目录树：目录带条目数，文件只列名字。"""
    lines: list[str] = []
    try:
        top = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return lines
    for p in top:
        if len(lines) >= max_lines:
            lines.append("  …（更多条目已省略）")
            break
        name = p.name
        if p.is_dir():
            if name in PRUNE_DIRS or name.startswith("."):
                continue
            try:
                n = sum(1 for _ in p.iterdir())
            except OSError:
                n = 0
            lines.append(f"  {name}/  ({n})")
            if n <= 24:                     # 小目录顺手展开一层
                try:
                    for q in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                        if len(lines) >= max_lines:
                            break
                        if q.name in PRUNE_DIRS or q.name.startswith("."):
                            continue
                        lines.append(f"    {q.name}{'/' if q.is_dir() else ''}")
                except OSError:
                    pass
        else:
            if name.startswith("."):
                continue
            lines.append(f"  {name}")
    return lines


def _module_lines(root: Path, toks: set[str] | None = None, *,
                  max_dirs: int = 6, max_names: int = 48) -> list[str]:
    """源码模块清单：顶层目录 → 该目录下的 .py 文件名（一行一个目录）。

    为什么单独列：_tree_lines 对条目超过 24 的目录只写「agentforge/ (30)」不展开，
    模型就凭印象猜出 `agentforge/core`、`agentforge/skills` 这种不存在的路径
    （rerun22 实测：list_dir 已明确回「路径不存在」，它仍把该路径写进交付物）。
    一行文件名比展开整棵子树便宜得多，却能直接消灭这类幻觉——判断「某能力在不在」
    之前，至少知道真实模块有哪些。

    排序按任务相关度：模块多到要截断时，先保住 context/permissions/tasksession
    这些任务真正会问到的，而不是按字典序随机砍掉尾部。
    """
    lines: list[str] = []
    toks = toks or set()

    def _rank(name: str) -> tuple[int, str]:
        stem = name[:-3].lower() if name.endswith(".py") else name.lower()
        hit = sum(1 for t in toks if t and t in stem)
        return (-hit, name)

    try:
        tops = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return lines
    for p in tops:
        if len(lines) >= max_dirs:
            break
        if p.name in PRUNE_DIRS or p.name.startswith("."):
            continue
        try:
            names = sorted((q.name for q in p.iterdir()
                            if q.is_file() and q.suffix == ".py"
                            # 测试脚本不算「模块」：tools/ 下 60 个 test_* 会把
                            # 预算吃光，却对判断「能力在不在」毫无帮助。
                            and not q.name.startswith(("test_", "bench"))), key=_rank)
        except OSError:
            continue
        if not names:
            continue
        shown = names[:max_names]
        tail = f" …(共 {len(names)} 个)" if len(names) > max_names else ""
        lines.append(f"  {p.name}/: " + " ".join(shown) + tail)
    # 根目录下的 .py（入口 / 单文件脚本）
    try:
        root_py = sorted((q.name for q in root.iterdir()
                          if q.is_file() and q.suffix == ".py"), key=_rank)
    except OSError:
        root_py = []
    if root_py and len(lines) < max_dirs:
        lines.append("  (根目录): " + " ".join(root_py[:max_names]))
    return lines


def _external_lines(root: Path, toks: set[str], *, max_repos: int = 6,
                    max_children: int = 26) -> list[str]:
    """external/ 下被分析仓库的真实清单：仓库名 + 一级子目录。

    动机（rerun23 实测）：`external/learn-claude-code` 的子目录是
    `s01_agent_loop`、`s08_context_compact` 这种下划线风格，模型顺着风格把
    仓库名也写成 `learn_claude_code`，被引用核查连拒 4 次，每次都要整篇重写。
    目录结构块只写到 `external/` 一层不展开，等于逼模型猜仓库名拼写。
    这里把真实拼写摊开给它抄；任务没提外部仓库时只列仓库名，省预算。
    """
    ext = root / "external"
    if not ext.is_dir():
        return []
    toks = toks or set()
    deep = any(t in toks for t in ("external", "外部", "仓库", "repo", "逆向", "第三方"))
    try:
        repos = sorted(p for p in ext.iterdir()
                       if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    lines: list[str] = []
    for repo in repos[:max_repos]:
        if not deep:
            lines.append(f"  external/{repo.name}/")
            continue
        try:
            kids = sorted(q.name for q in repo.iterdir()
                          if q.is_dir() and not q.name.startswith("."))
        except OSError:
            kids = []
        shown = " ".join(kids[:max_children]) if kids else "(无子目录)"
        tail = f" …(共 {len(kids)} 个目录)" if len(kids) > max_children else ""
        lines.append(f"  external/{repo.name}/: {shown}{tail}")
    if len(repos) > max_repos:
        lines.append(f"  …external/ 下另有 {len(repos) - max_repos} 个仓库")
    return lines


# 任务点名了具体文件（带扩展名，或含路径分隔符）：读/改这个文件的任务不需要
# 整张地图——它已经知道要哪个文件，给目录树只是白占「免压缩区」。
_NAMED_FILE_RX = re.compile(
    r"[\w\-./\\]+\.(?:py|md|markdown|json|ya?ml|toml|ini|cfg|txt|js|mjs|ts|tsx|jsx"
    r"|html?|css|scss|sh|bat|ps1|csv|log|xlsx?|docx?|pptx?)\b", re.I)
# 「要动代码」的信号：改/修/实现/重构/测试/报错……
_CODE_SIGNALS = ("改", "修", "实现", "重构", "新增", "添加", "删除", "优化", "替换",
                 "函数", "模块", "接口", "报错", "测试", "跑一下", "refactor", "fix",
                 "implement", "add ", "remove", "update", "bug", "test")
# 「只是在问/在读」的信号
_QA_SIGNALS = ("是什么", "为什么", "怎么", "解释", "总结", "说明", "介绍", "讲讲",
               "告诉我", "几段话", "三句话", "一句话", "读一下", "看一下", "分析",
               "what", "why", "how", "explain", "summar")


def _profile(task_text: str) -> dict:
    """按任务类型决定预热块给哪几段（纯关键词，确定性，可单测）。

    动机（§6.4 上下文体检）：预热块占系统提示 47.8%，而其中绝大多数对
    「读一个 README 并回答三句话」这类任务毫无用处——它却处在免压缩区，
    每一轮都要重发。裁剪的判据只用任务原文里的词，不问模型。

    三档：
      read_named  点名了文件 → 不给地图（它知道要哪个），只给该文件的锚点
      qa          纯问/纯读 → 给超短目录 + README，不给模块清单
      code        要动代码/默认 → 保持原样（树 + 模块 + 外部仓库 + 锚点）
    """
    text = task_text or ""
    low = text.lower()
    named = bool(_NAMED_FILE_RX.search(text))
    code = sum(1 for w in _CODE_SIGNALS if w in low)
    qa = sum(1 for w in _QA_SIGNALS if w in low)

    if named and code == 0:
        # 点名文件 + 没有改造意图 = 读它/答关于它的问题。
        # README 摘要也去掉：任务说读 README.md，再给一份 README 摘要纯属重复。
        return {"intent": "read_named", "tree": 0, "modules": False,
                "external": False, "readme": False, "files": 4}
    if named and code:
        # 要改这个文件：给少量地图（改完可能要连带改别的），但不必给全量
        return {"intent": "edit_named", "tree": 20, "modules": True,
                "external": False, "readme": False, "files": 8}
    if qa and code == 0:
        return {"intent": "qa", "tree": 12, "modules": False,
                "external": False, "readme": True, "files": 4}
    return {"intent": "code", "tree": 60, "modules": True,
            "external": True, "readme": True, "files": None}


def _readme_digest(root: Path, limit: int = 600) -> str:
    """README 开头若干字（按行边界截断），去掉图片与链接尾巴。"""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = root / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
            text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)      # 图片
            text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接留文字
            cut = text[:limit]
            if len(text) > limit:
                cut = cut[:cut.rfind("\n")] if "\n" in cut else cut
                cut += " …"
            return cut.strip()
    return ""


def build_block(cfg, task_text: str) -> tuple[str, dict]:
    """生成预热注入块。返回 (块文本, 统计)；开关关闭或工作区不可读返回 ("", stats)。"""
    stats = {"chars": 0, "tree_entries": 0, "hit_files": 0, "modules": 0}
    if not getattr(cfg, "warmup_enabled", True):
        return "", stats
    root = Path(cfg.workspace_root)
    if not root.is_dir():
        return "", stats

    max_chars = max(500, int(getattr(cfg, "warmup_max_chars", 3500)))
    max_files = max(0, int(getattr(cfg, "warmup_max_files", 12)))
    prof = _profile(task_text)
    stats["intent"] = prof["intent"]
    if prof.get("files") is not None:
        max_files = min(max_files, int(prof["files"]))

    parts: list[str] = ["【项目速览 · 任务开局注入】"
                        "（路径均相对工作区根目录，需要细节直接 read_file / list_dir）"]

    tree = _tree_lines(root, max_lines=int(prof["tree"])) if prof["tree"] else []
    stats["tree_entries"] = len(tree)
    if tree:
        parts.append("【目录结构】\n" + "\n".join(tree))

    # 放在目录结构之后、README 之前：它比 README 摘要更能防「路径幻觉」。
    # 但只发给「要动代码」的任务：纯阅读/纯问答给模块清单等于白占位置。
    if prof["modules"]:
        mods = _module_lines(root, _task_tokens(task_text))
        stats["modules"] = len(mods)
        if mods:
            parts.append("【源码模块清单】（判断「某能力在不在」前先看这里，别猜路径）\n"
                         + "\n".join(mods))

    # 被分析的外部仓库清单：只列仓库名 + 一级子目录。
    # 为什么必须显式给：external/learn-claude-code 的子目录是 s01_agent_loop、
    # s08_context_compact 这种下划线风格，模型会把仓库名也套成 learn_claude_code
    # （rerun23 被引用核查连拒 4 次都是这个错，每次都要重写整篇交付物）。
    # 目录结构块只写到 external/ 不展开，等于让模型凭印象拼仓库名。
    if prof["external"]:
        exts = _external_lines(root, _task_tokens(task_text))
        if exts:
            stats["external"] = len(exts)
            parts.append("【外部仓库清单】（这些是真实目录名，引用时必须原样照抄拼写）\n"
                         + "\n".join(exts))

    if prof["readme"]:
        digest = _readme_digest(root)
        if digest:
            parts.append("【README 摘要】\n" + digest)

    if max_files:
        toks = _task_tokens(task_text)
        scored: list[tuple[int, str]] = []
        for rel, is_dir in _walk(root):
            if is_dir:
                continue
            s = str(rel).lower()
            hit = sum(1 for t in toks if t in s)
            if hit:
                scored.append((hit, str(rel)))
        scored.sort(key=lambda x: (-x[0], x[1]))
        if scored:
            # rerun38/40 实证：被证据闸门拒绝的主要原因是模型不知道已读文件里
            # 哪些行可锚定（class/def/标题），只能凭印象写裸路径，被拒后再重读。
            # 把坐标在开局就给到：每个候选文件带行数 + 前 3 个可锚定行，
            # 「写 → 拒 → 补读」的往返压成「一次写对」。坐标是程序从磁盘抽的，
            # 不是模型编的——引用时仍须落在 read_file 读过的区间内。
            hits: list[str] = []
            for h, p in scored[:max_files]:
                fp = root / p
                try:
                    if not fp.is_file() or fp.stat().st_size > 400_000:
                        hits.append(f"  {p}（命中 {h} 处）")
                        continue
                    n_lines = sum(1 for _ in fp.open("r", encoding="utf-8",
                                                     errors="replace"))
                    anchors = anchor_lines(fp, None, max_n=3)
                except OSError:
                    hits.append(f"  {p}（命中 {h} 处）")
                    continue
                if anchors:
                    hits.append(f"  {p}（{n_lines} 行；锚点 {'；'.join(anchors)}）")
                else:
                    hits.append(f"  {p}（{n_lines} 行，命中 {h} 处）")
            stats["hit_files"] = min(len(scored), max_files)
            note = f"（共 {len(scored)} 个文件命中，取前 {stats['hit_files']} 个）" \
                if len(scored) > max_files else ""
            parts.append("【与任务相关的文件 · 证据候选】"
                         "引用格式 `path:起-止`，行号取锚点附近你读过的内容；"
                         "read_file 回执尾部也会给锚点" + note + "\n"
                         + "\n".join(hits))

    if len(parts) <= 1:
        return "", stats                    # 只有标题，工作区近乎是空的

    block = "\n\n".join(parts)
    if len(block) > max_chars:
        block = block[:max_chars].rsplit("\n", 1)[0] + "\n  …（预热内容已按上限截断）"
    stats["chars"] = len(block)
    return block, stats


def build_once(cfg, task_text: str, *, ttl: float = 600.0):
    """带缓存的一次性构建：同一任务内多员工共享一份（工作区秒级不变）。

    缓存挂在 cfg 对象上（跟随配置生命周期），ttl 过期或任务原文变化才重建。
    """
    key = "_warmup_cache"
    cache = getattr(cfg, key, None)
    now = time.time()
    if cache and now - cache[0] < ttl and cache[1] == task_text:
        return cache[2], cache[3]
    block, stats = build_block(cfg, task_text)
    setattr(cfg, key, (now, task_text, block, stats))
    return block, stats
