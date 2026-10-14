"""工作区长期记忆（只放用户偏好）。

任务跑完时，模型可以用 `remember` 工具把「用户偏好」写进来；
下一次任务开始前，这段记忆会被注入系统提示——于是它真的会影响后续任务，
而不是像对话历史那样只停留在那一条任务里。

## 为什么只放偏好

记忆里**只放偏好**（倾向 / 风格 / 习惯，如「回答先给结论」「代码注释用中文」
「产出文档一律简体中文」）。另外两类东西**不进来**：

- **事实与环境信息**（路径 / 端口 / 版本 / 依赖）有工具能自己查——
  查得到的东西不该抄一份到记忆里，抄下来只会过期、误导。
- **项目约定 / 工作方式 / 流程规则**该由**架构**强制（程序 + 常驻断言）。
  它们写成记忆只是「劝」：模型没读到、或读到了没照做，这件事就不发生。
  判据是「删掉这条记忆，测试会不会红」——会红，说明它本来就该待在架构里。

一句话：**设定靠架构，偏好靠记忆**。这份文件只留「允许偶尔不生效」的那一类。

文件落在 `<工作区>/.agentforge/memory.md`，一行一条：

    - [2026-10-11 18:20 · t1011ab12] 用户偏好：界面文案一律中文

行首标记里带着写入它的任务 id；旧格式（带 `· L2` 层标签）的行仍能解析——
层标签会被就地忽略，不需要迁移文件。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

MAX_NOTE_CHARS = 600          # 单条记忆的长度上限
PROMPT_MAX_CHARS = 2400       # 注入系统提示时的长度上限

# 行首标记：[时间 · 任务id(可空)]。兼容旧格式末尾的 `· L1/L2/L3` 层标签，读到时忽略。
_LINE = re.compile(
    r"^- \[(?P<ts>[^\]]*?) · (?P<task>[^\]]*?)(?: · L[123])?\]\s?(?P<text>.*)$"
)


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

        表头与说明行（# 标题、> 引用）不是记忆条目，管理页不该把它们列出来。
        旧格式行尾的 `· L1/L2/L3` 层标签会被就地丢弃（记忆不再分层）。
        """
        out: list[dict] = []
        for i, line in enumerate(self.read().splitlines()):
            s = line.rstrip()
            if not s.strip():
                continue
            if s.lstrip().startswith(("#", ">")):
                continue
            m = _LINE.match(s)
            if m:
                out.append(
                    {
                        "index": i,
                        "ts": m.group("ts").strip(),
                        "task": m.group("task").strip(),
                        "text": m.group("text").strip(),
                    }
                )
            else:
                out.append(
                    {
                        "index": i, "ts": "", "task": "",
                        "text": s.lstrip("- ").strip(),
                    }
                )
        return out

    def count(self, task_ids: list[str] | None = None) -> int:
        items = self.entries()
        if task_ids is None:
            return len(items)
        wanted = set(task_ids)
        return sum(1 for e in items if e["task"] in wanted)

    def prompt_block(self, max_chars: int = PROMPT_MAX_CHARS) -> str:
        """给系统提示用的记忆片段：**取最近的若干条偏好**，按时间正序排。

        记忆只放偏好、不再分层，所以这里不需要按任务类型挑层——一律给最近的
        若干条（越近的偏好越贴近当前工作）。超出预算时丢最旧的。
        """
        items = self.entries()
        if not items:
            return ""
        picked: list[str] = []
        used = 0
        for e in reversed(items):          # 最近优先
            line = f"- {e['text']}"
            if picked and used + len(line) > max_chars:
                break
            picked.append(line)
            used += len(line)
        picked.reverse()
        return "\n".join(picked)

    # ---------- 写 ----------
    def append(self, note: str, task_id: str = "") -> dict:
        text = _clean(note)
        if not text:
            return {}
        entry = {"ts": _now_text(), "task": task_id or "会话", "text": text}
        line = f"- [{entry['ts']} · {entry['task']}] {text}\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            else:
                header = (
                    "# AgentForge 工作区记忆\n\n"
                    "> 由任务自动写入；新任务开始时会读取，用来延续用户的长期偏好。\n"
                    "> 只放偏好（倾向 / 风格 / 习惯）；环境事实工具能现查、项目约定由架构保证，都不写在这里。\n\n"
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

    def drop_indices(self, indices: list[int]) -> int:
        """删掉指定的若干条（按 entries() 给出的行号）。

        清空任务弹窗支持逐条勾选记忆，勾中的行号从这里进来。行号是「当时看到的
        那一版文件」里的位置，期间文件可能被别的写入改过，所以先用当前 entries()
        做一次交集——认不出来的行号忽略，避免删错行。
        """
        wanted: set[int] = set()
        for raw in (indices or []):
            try:
                wanted.add(int(raw))
            except (TypeError, ValueError):
                continue
        if not wanted:
            return 0
        items = self.entries()
        hit = wanted & {e["index"] for e in items}
        if not hit:
            return 0
        keep = [e["index"] for e in items if e["index"] not in hit]
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


__all__ = ["MemoryStore", "PROMPT_MAX_CHARS"]
