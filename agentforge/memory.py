"""工作区长期记忆（分层版）。

任务跑完时，模型可以用 `remember` 工具把「以后还用得上的结论」写进来；
下一次任务开始前，这段记忆会被注入系统提示——于是它真的会影响后续任务，
而不是像对话历史那样只停留在那一条任务里。

## 为什么要分层

以前所有记忆平铺成一行一条，注入时只是「取最近的若干条」。任务一多，长上下文下
定位精度就会掉：一条三周前的环境事实和一条昨天的用户偏好被同等对待，模型只能靠
自己读全文去挑。参考 TencentDB Agent Memory 的 L0~L3 分层思路，这里做单用户本地
场景的实用简化——按**语义粒度**给每条记忆打一个层标签：

    L1 原子事实   环境/路径/版本/端口/依赖这类可精确召回的事实
    L2 项目约定   项目规则、工作方式、流程约定（默认层，也是兜底层）
    L3 用户偏好   稳定的个人偏好与习惯（画像）

注入时按任务类型决定检索深度：常规对话优先给 L3/L2 的偏好与约定，具体技术细节
任务优先给 L1 的原子事实（见 `SCOPE_WEIGHTS`）。这样不改任何基础设施、不加依赖，
就能把「该看什么」和「现在要干什么」对齐。

文件落在 `<工作区>/.agentforge/memory.md`，一行一条：

    - [2026-10-11 18:20 · t1011ab12 · L3] 用户要求界面文案一律中文
    - [2026-10-12 09:05 · t1012cd34 · L1] 本地 Ollama 端口是 11434

行首标记里带着写入它的任务 id 与层标签；旧格式（没有层标签）的行仍能解析，
读取时会就地推断一个层，不需要迁移文件。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

MAX_NOTE_CHARS = 600          # 单条记忆的长度上限
PROMPT_MAX_CHARS = 2400       # 注入系统提示时的长度上限（按层分配）

# 三个语义层。顺序即「粒度从细到粗」，也用于界面的展示顺序。
LAYERS = ("L1", "L2", "L3")
DEFAULT_LAYER = "L2"          # 判不准时归到项目约定——它是最泛的一类
LAYER_LABEL = {
    "L1": "事实与环境",        # 原子事实
    "L2": "项目约定",          # 场景/项目知识
    "L3": "用户偏好",          # 长期画像
}

# 按任务类型的检索深度（每层的预算占比，会在缺项时把余额让给后面的层）：
#   wide  —— 常规对话/规划：偏好与约定优先，事实只取最近几条
#   facts —— 具体技术细节任务：原子事实优先，多给可精确执行的信息
SCOPE_WEIGHTS = {
    "wide": {"L3": 0.40, "L2": 0.40, "L1": 0.20},
    "facts": {"L1": 0.55, "L2": 0.30, "L3": 0.15},
}
# 每层在注入时的呈现顺序（wide 先给画像，facts 先给事实）
SCOPE_ORDER = {
    "wide": ("L3", "L2", "L1"),
    "facts": ("L1", "L2", "L3"),
}

# 行首标记：[时间 · 任务id(可空) · 层(可空)]
_LINE = re.compile(
    r"^- \[(?P<ts>[^\]]*?) · (?P<task>[^\]]*?)(?: · (?P<layer>L[123]))?\]\s?(?P<text>.*)$"
)

# ---- 层的兜底推断（用于旧格式行与没给层的写入） ----
# L3 用户偏好：稳定的个人偏好/习惯/风格
_PREF = re.compile(
    r"用户偏好|偏好|我喜欢|我习惯|我的要求|我的习惯|我的风格|"
    r"一律|始终|总是|统一(?:用|为|成)|回复风格|风格|口吻|语气"
)
# L1 原子事实：可精确召回的环境/路径/版本/端口/依赖等
_FACT = re.compile(
    r"[A-Za-z]:\\|/[\w.-]+/"
    r"|\b\d+(?:\.\d+){1,3}\b"
    r"|端口|版本|路径|目录|环境变量|依赖|安装位置|地址|工作区"
    r"|https?://"
    r"|\S+\.(?:py|js|ts|tsx|jsx|json|ya?ml|toml|ini|cfg|md|txt|csv|sh|bat)\b",
    re.I,
)


def classify_layer(text: str) -> str:
    """给一条记忆判层。

    顺序有讲究：**偏好优先**。「用户偏好：代码注释一律用中文」既含「一律」（偏好）
    又可能含「代码」，但它是画像而不是环境事实；先判 L3 才不会把它丢进 L1。
    判不出偏好的再看像不像原子事实，都不像就归 L2（项目约定）。
    """
    s = str(text or "")
    if _PREF.search(s):
        return "L3"
    if _FACT.search(s):
        return "L1"
    return DEFAULT_LAYER


def normalize_layer(layer: str, text: str = "") -> str:
    """把外部传入的层名归一化；空值/非法值用 classify_layer 兜底。"""
    s = str(layer or "").strip().upper()
    if s in LAYERS:
        return s
    return classify_layer(text)


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _clean(text: str) -> str:
    """记忆是一行一条，换行与多余空格都压平，避免把文件结构搅乱。"""
    s = " ".join(str(text or "").split())
    if len(s) > MAX_NOTE_CHARS:
        s = s[:MAX_NOTE_CHARS] + "…"
    return s


class MemoryStore:
    """长期记忆的读写与统计。所有写操作都加锁，Web 端可能多线程并发。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    # ---------- 读 ----------
    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> str:
        if not self.path.exists():
            return ""
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def entries(self) -> list[dict]:
        """把文件解析成结构化条目；解析不了的行原样保留为 raw 条目。

        每条都带 `layer`：新格式直接读标记，旧格式（或标记非法）就地推断——
        这样老记忆文件不用迁移就能参与分层检索。
        """
        out: list[dict] = []
        for i, line in enumerate(self.read().splitlines()):
            s = line.rstrip()
            if not s.strip():
                continue
            # 表头与说明行（# 标题、> 引用）不是记忆条目，管理页不该把它们列出来
            if s.lstrip().startswith(("#", ">")):
                continue
            m = _LINE.match(s)
            if m:
                text = m.group("text").strip()
                out.append(
                    {
                        "index": i,
                        "ts": m.group("ts").strip(),
                        "task": m.group("task").strip(),
                        "layer": normalize_layer(m.group("layer") or "", text),
                        "text": text,
                    }
                )
            else:
                text = s.lstrip("- ").strip()
                out.append(
                    {
                        "index": i, "ts": "", "task": "",
                        "layer": classify_layer(text), "text": text,
                    }
                )
        return out

    def count(self, task_ids: list[str] | None = None) -> int:
        items = self.entries()
        if task_ids is None:
            return len(items)
        wanted = set(task_ids)
        return sum(1 for e in items if e["task"] in wanted)

    def layer_counts(self) -> dict[str, int]:
        """各层条数，供界面显示「偏好 N / 约定 N / 事实 N」。"""
        out = {L: 0 for L in LAYERS}
        for e in self.entries():
            L = e.get("layer") or DEFAULT_LAYER
            out[L] = out.get(L, 0) + 1
        return out

    def prompt_block(self, max_chars: int = PROMPT_MAX_CHARS, scope: str = "wide") -> str:
        """给系统提示用的记忆片段：**按层分组、按任务类型分配预算**。

        scope 决定先给谁、给多少（见 SCOPE_WEIGHTS）：
        常规任务先给偏好与约定，技术细节任务先给原子事实。层内取最近的若干条；
        某一层条数不够时，剩下的预算自动让给后面的层，不会浪费。
        组标题本身也是给模型的信号——它一眼就知道哪几条是硬约定、哪几条是环境事实。
        """
        items = self.entries()
        if not items:
            return ""
        weights = SCOPE_WEIGHTS.get(scope) or SCOPE_WEIGHTS["wide"]
        order = SCOPE_ORDER.get(scope) or SCOPE_ORDER["wide"]

        buckets: dict[str, list[dict]] = {L: [] for L in LAYERS}
        for e in items:
            L = e.get("layer") if e.get("layer") in buckets else DEFAULT_LAYER
            buckets[L].append(e)

        sections: list[str] = []
        remaining = max_chars
        for i, L in enumerate(order):
            rest_w = sum(weights.get(x, 0.0) for x in order[i:]) or 1.0
            quota = int(remaining * weights.get(L, 0.0) / rest_w)
            picked: list[str] = []
            used = 0
            for e in reversed(buckets[L]):        # 层内最近优先
                line = f"- {e['text']}"
                if picked and used + len(line) > quota:
                    break
                picked.append(line)
                used += len(line)
            if not picked:
                continue
            picked.reverse()
            sections.append(f"### {LAYER_LABEL[L]}\n" + "\n".join(picked))
            remaining = max(0, remaining - used)
        return "\n".join(sections)

    # ---------- 写 ----------
    def append(self, note: str, task_id: str = "", layer: str = "") -> dict:
        text = _clean(note)
        if not text:
            return {}
        L = normalize_layer(layer, text)
        entry = {"ts": _now_text(), "task": task_id or "会话", "layer": L, "text": text}
        line = f"- [{entry['ts']} · {entry['task']} · {L}] {text}\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            else:
                header = (
                    "# AgentForge 工作区记忆\n\n"
                    "> 由任务自动写入；新任务开始时会读取，用来延续既有结论与偏好。\n"
                    "> 每条带一个层标签：L1 事实与环境 / L2 项目约定 / L3 用户偏好。\n\n"
                )
                self.path.write_text(header + line, encoding="utf-8")
        return entry

    def _rewrite(self, keep: list[str]) -> int:
        """按「保留哪些原始行」重写文件，返回删掉的条目数。

        表头与说明行（# 标题、> 引用）不属于记忆条目，任何重写都必须原样保留，
        否则删一条记忆会把文件表头也带走。
        """
        raw_lines = self.read().splitlines()
        keep_set = set(keep)

        def is_header(line: str) -> bool:
            return (not line.strip()) or line.lstrip().startswith(("#", ">"))

        kept = [line for i, line in enumerate(raw_lines)
                if i in keep_set or is_header(line)]
        removed = len([ln for i, ln in enumerate(raw_lines)
                       if ln.strip() and i not in keep_set and not is_header(ln)])
        with self._lock:
            if not self.path.exists():
                return 0
            self.path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        return removed

    def drop_tasks(self, task_ids: list[str]) -> int:
        """删掉由这些任务写入的记忆条目。task_ids 为空表示不动。"""
        wanted = set(t for t in (task_ids or []) if t)
        if not wanted:
            return 0
        items = self.entries()
        keep = [e["index"] for e in items if e["task"] not in wanted]
        return self._rewrite(keep)

    def drop_index(self, index: int) -> int:
        """删掉单独一条（按 entries() 给出的行号）。"""
        items = self.entries()
        if not any(e["index"] == index for e in items):
            return 0
        keep = [e["index"] for e in items if e["index"] != index]
        return self._rewrite(keep)

    def clear(self) -> int:
        """清空全部记忆（连文件一起删掉，下次写入会重新生成表头）。"""
        with self._lock:
            n = self.count()
            if self.path.exists():
                try:
                    self.path.unlink()
                except OSError:
                    pass
            return n


__all__ = [
    "MemoryStore", "PROMPT_MAX_CHARS", "LAYERS", "LAYER_LABEL",
    "DEFAULT_LAYER", "SCOPE_WEIGHTS", "classify_layer", "normalize_layer",
]
