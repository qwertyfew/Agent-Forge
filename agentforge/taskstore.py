"""对话任务台账。

Web UI 里每发送一次消息，就在台账里登记一条任务：开始记 running，
模型跑完写 done / error，被用户停掉写 stopped。侧栏的「任务管理」读它，
展示正在执行的任务与历史任务（含耗时、工具次数、预估费用），
点进去还能回看那次对话的完整记录。

除了对话，一条任务还会带上它在磁盘上留下的产出（新建/修改/删除的
本地文件，含中间文件与结果文件）。清空任务时前端可以勾选「一并清理
这些文件」——清单就是从这里读的。

持久化在项目根目录的 .tasks.json（已加入 .gitignore），进程重启后
历史任务仍在。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .planner import short_title

MAX_TASKS = 120                # 台账最多保留多少条任务（含正在跑的）
MAX_HISTORY_CHARS = 150_000    # 单条任务保存的对话历史总体积上限
TOOL_KEEP_CHARS = 4_000        # 单条工具输出在台账里的截断长度
MSG_KEEP_CHARS = 16_000        # 单条消息的截断长度


def _now() -> float:
    return time.time()


def _trim_history(history: list[dict]) -> list[dict]:
    """裁剪待存历史：保留消息结构，压缩超长内容，控制总体积。

    台账是给人回看的，不需要逐字节还原——工具输出往往是几十 KB 的日志，
    原样存下来会把 .tasks.json 撑到几十兆。
    """
    out: list[dict] = []
    total = 0
    for m in history or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        cap = TOOL_KEEP_CHARS if m.get("role") == "tool" else MSG_KEEP_CHARS
        if len(content) > cap:
            content = content[:cap] + f"\n…（已截断 {len(content) - cap} 字）"
        item = {"role": m.get("role", ""), "content": content}
        if m.get("tool_calls"):
            item["tool_calls"] = m["tool_calls"]
        total += len(content)
        if total > MAX_HISTORY_CHARS:
            item["content"] += "\n…（后续内容超出上限，未保存）"
            out.append(item)
            break
        out.append(item)
    return out


def _norm_files(files: list[dict], cap: int = 400) -> list[dict]:
    """产出文件清单落库前的整形：去掉无关字段、去重、限量。

    台账是给人看的，同一条任务里同一个文件被写十次只该出现一次，
    动作取最「重」的那个（created > modified > deleted 的优先级不适用，
    这里直接以最后一次动作为准，因为那才是文件现在的状态）。
    """
    out: dict[str, dict] = {}
    for f in files or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip()
        if not path:
            continue
        action = str(f.get("action") or "modified")
        if action not in ("created", "modified", "deleted"):
            action = "modified"
        key = path.lower()
        rec = {
            "path": path,
            "action": action,
            "via": str(f.get("via") or "tool"),
            "tool": str(f.get("tool") or ""),
        }
        old = out.get(key)
        # 已经记为「新建」的文件后来被改名/再写，不该退回「修改」
        if old and old["action"] == "created" and action == "modified":
            rec["action"] = "created"
        out[key] = rec
    return list(out.values())[:cap]


@dataclass
class Task:
    id: str
    title: str
    # 用户的原始指令（完整，不截断、不压空白）。
    # title 是它的派生物（压空白 + 截到 90 字），只能当侧栏标签用；界面上的
    # 「提问气泡」必须用原文。这条字段的意义在于**任务一开始就有**——history
    # 要到任务结束才写，中间这段时间别的端（网页、CLI、脚本）想画这条任务的
    # 提问，除了它没有别的真相可用。
    prompt: str = ""
    status: str = "running"            # running | done | error | stopped
    created_at: float = field(default_factory=_now)
    ended_at: float | None = None
    models: list[str] = field(default_factory=list)
    tools: int = 0                     # 调用过多少次工具
    steps: int = 0                     # 编排步数
    summary: str = ""                  # 最终回复摘要
    error: str = ""
    history: list[dict] = field(default_factory=list)
    # ---- 用量与费用（预估，本地模型恒为 0）----
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    local_only: bool = True            # 这次任务是否全程只用了本地模型
    progress: str = ""                 # 当前进度的一句话描述（给侧栏显示）
    permission: str = "full"           # full | default，这次任务用的权限模式
    # 这次任务在磁盘上留下的东西：新建/修改/删除的文件（含中间文件、结果文件）。
    # 条目形如 {"path","action","via","tool"}，清空任务时可据此一并清理。
    files: list[dict] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return (self.ended_at or _now()) - self.created_at

    def as_dict(self, with_history: bool = False) -> dict:
        d = asdict(self)
        if not with_history:
            d.pop("history", None)
        else:
            d["has_history"] = bool(self.history)
        d["duration"] = round(self.duration, 1)
        return d


class TaskStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._tasks: list[Task] = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> list[Task]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = raw.get("tasks") if isinstance(raw, dict) else None
        tasks: list[Task] = []
        for item in items or []:
            try:
                t = Task(**item)
            except TypeError:
                continue
            # 进程重启后不可能还有真在跑的任务。留着 running 会让界面
            # 误以为有任务在执行：按钮一直停在「暂停」、落地页判定失效
            # （输入框贴顶）。启动时统一归正为中断。
            if t.status == "running":
                t.status = "error"
                t.error = "服务重启时该任务仍在执行，已标记为中断"
                t.ended_at = t.ended_at or _now()
            tasks.append(t)
        return tasks

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps({"tasks": [asdict(t) for t in self._tasks]}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            pass  # 台账写不进去不该拖垮任务本身

    # ---------- 写 ----------
    def begin(self, message: str, model: str = "", permission: str = "full") -> str:
        """登记一条新任务。message 是用户原话，原样记进 prompt；标题只是它的摘要。"""
        tid = "t" + time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:4]
        task = Task(
            id=tid,
            prompt=message or "",
            title=short_title(message, 90) or "未命名任务",
        )
        task.permission = "default" if permission == "default" else "full"
        if model:
            task.models = [model]
        with self._lock:
            self._tasks.insert(0, task)
            del self._tasks[MAX_TASKS:]
            self._save()
        return tid

    def _find(self, tid: str) -> Task | None:
        return next((t for t in self._tasks if t.id == tid), None)

    def touch(
        self,
        tid: str,
        *,
        model: str = "",
        tool: bool = False,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: float = 0.0,
        free: bool | None = None,
        progress: str = "",
    ) -> None:
        """执行过程中的轻量更新（新增模型、工具计数、用量、当前进度）。"""
        with self._lock:
            task = self._find(tid)
            if task is None:
                return
            if model and model not in task.models:
                task.models.append(model)
            if tool:
                task.tools += 1
            if prompt_tokens:
                task.prompt_tokens += int(prompt_tokens)
            if completion_tokens:
                task.completion_tokens += int(completion_tokens)
            if cost:
                task.cost = round(task.cost + float(cost), 6)
            if free is False:
                task.local_only = False
            if progress:
                task.progress = progress[:120]
            self._save()

    def finish(
        self,
        tid: str,
        status: str = "done",
        *,
        summary: str = "",
        error: str = "",
        history: list[dict] | None = None,
        steps: int = 0,
        files: list[dict] | None = None,
    ) -> None:
        with self._lock:
            task = self._find(tid)
            if task is None:
                return
            task.status = status
            task.ended_at = _now()
            task.progress = ""
            if summary:
                task.summary = summary
            if error:
                task.error = error
            if steps:
                task.steps = steps
            if history is not None:
                task.history = _trim_history(history)
            if files:
                task.files = _norm_files(files)
            self._save()

    # ---------- 任务产出文件 ----------
    def files_of(self, tids: list[str] | None = None) -> list[dict]:
        """汇总若干任务记录的产出文件（去重后按路径排序）。

        只按「记录里写了什么」汇总，不判断文件现在还在不在——判断存在与否是
        界面/清理时的活，台账只负责如实记账。
        """
        wanted = set(tids) if tids is not None else None
        merged: dict[str, dict] = {}
        with self._lock:
            for t in self._tasks:
                if wanted is not None and t.id not in wanted:
                    continue
                for f in t.files or []:
                    key = str(f.get("path") or "").lower()
                    if not key or key in merged:
                        continue
                    item = dict(f)
                    item["task"] = t.id
                    item["task_title"] = t.title
                    item["at"] = t.created_at
                    merged[key] = item
        return sorted(merged.values(), key=lambda x: str(x.get("path") or "").lower())

    def drop_files(self, tids: list[str] | None = None) -> int:
        """清掉任务记录里的文件清单（文件已删除后调用，避免清单变成幽灵）。"""
        wanted = set(tids) if tids is not None else None
        n = 0
        with self._lock:
            for t in self._tasks:
                if wanted is not None and t.id not in wanted:
                    continue
                n += len(t.files or [])
                t.files = []
            if n:
                self._save()
        return n

    def drop_file_paths(self, paths: list[str]) -> int:
        """按路径精确清掉文件记录（其余记录保留）。

        清空任务只删中间文件、保留最终产出时用：被删的文件从清单里摘掉
        （免得清单变成幽灵），保留的产出继续跟着任务走。
        """
        import os as _os

        def _key(p: str) -> str:
            try:
                return _os.path.normcase(_os.path.abspath(str(p or "")))
            except OSError:
                return str(p or "")

        wanted = {_key(p) for p in paths}
        if not wanted:
            return 0
        n = 0
        with self._lock:
            for t in self._tasks:
                files = t.files or []
                kept = [f for f in files if _key(f.get("path") or "") not in wanted]
                if len(kept) != len(files):
                    n += len(files) - len(kept)
                    t.files = kept
            if n:
                self._save()
        return n

    def finished(self) -> list[Task]:
        """已结束（非 running）的任务，按时间倒序。"""
        with self._lock:
            return [t for t in self._tasks if t.status != "running"]

    def mark_orphaned(self, note: str = "服务重启，该任务被中断") -> int:
        """服务启动时调用：台账里还挂着 running 的任务其实是孤儿。

        跑它的那个进程已经没了，状态永远不会再更新。不处理的话界面上会
        永远显示「执行中」、计时一直涨——所以统一落成 stopped。
        """
        with self._lock:
            n = 0
            for t in self._tasks:
                if t.status == "running":
                    t.status = "stopped"
                    t.ended_at = _now()
                    t.progress = ""
                    if not t.error:
                        t.error = note
                    n += 1
            if n:
                self._save()
            return n

    def clear(self, keep_running: bool = True) -> int:
        """清空历史任务。正在跑的任务默认保留，否则前端会看到它凭空消失。"""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if keep_running and t.status == "running"]
            self._save()
            return before - len(self._tasks)

    # ---------- 读 ----------
    def list(self, limit: int = 80) -> list[dict]:
        with self._lock:
            return [t.as_dict() for t in self._tasks[:limit]]

    def get(self, tid: str) -> dict | None:
        with self._lock:
            task = self._find(tid)
            return task.as_dict(with_history=True) if task else None

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks if t.status == "running")


__all__ = ["Task", "TaskStore"]
