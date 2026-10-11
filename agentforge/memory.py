"""工作区长期记忆。

任务跑完时，模型可以用 `remember` 工具把「以后还用得上的结论」写进来；
下一次任务开始前，这段记忆会被注入系统提示——于是它真的会影响后续任务，
而不是像对话历史那样只停留在那一条任务里。

文件落在 `<工作区>/.agentforge/memory.md`，一行一条：

    - [2026-10-11 18:20 · t1011ab12] 用户要求界面文案一律中文

行首标记里带着写入它的任务 id，所以「清空某个任务的记忆」可以精确到条，
不需要把整个记忆文件一把清掉。
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

MAX_NOTE_CHARS = 600          # 单条记忆的长度上限
PROMPT_MAX_CHARS = 2400       # 注入系统提示时的长度上限（只取最近的部分）

_LINE = re.compile(r"^- \[(?P<ts>[^\]]*?) · (?P<task>[^\]]*?)\]\s?(?P<text>.*)$")


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
        """把文件解析成结构化条目；解析不了的行原样保留为 raw 条目。"""
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
                out.append(
                    {
                        "index": i,
                        "ts": m.group("ts").strip(),
                        "task": m.group("task").strip(),
                        "text": m.group("text").strip(),
                    }
                )
            else:
                out.append({"index": i, "ts": "", "task": "", "text": s.lstrip("- ").strip()})
        return out

    def count(self, task_ids: list[str] | None = None) -> int:
        items = self.entries()
        if task_ids is None:
            return len(items)
        wanted = set(task_ids)
        return sum(1 for e in items if e["task"] in wanted)

    def prompt_block(self, max_chars: int = PROMPT_MAX_CHARS) -> str:
        """给系统提示用的记忆片段：只取最近的若干条，避免把上下文挤爆。"""
        items = self.entries()
        if not items:
            return ""
        picked: list[str] = []
        used = 0
        for e in reversed(items):
            line = f"- {e['text']}"
            used += len(line)
            if used > max_chars and picked:
                break
            picked.append(line)
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
                header = "# AgentForge 工作区记忆\n\n> 由任务自动写入；新任务开始时会读取，用来延续既有结论与偏好。\n\n"
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


__all__ = ["MemoryStore", "PROMPT_MAX_CHARS"]
