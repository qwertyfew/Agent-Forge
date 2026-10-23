# -*- coding: utf-8 -*-
"""任务操作记录：把「读过什么、写过什么、每次写的版本」落成文件，而不是留在内存里。

## 为什么要有这一层

用户 2026-10-20 的判断：证据不该靠模型在正文里自证（写行号），也不该靠提示词
和返工，而应该由 harness 在执行工具时顺手落盘。**操作了文件就一定有记录**——
这是文件系统天然能保证的事，比让模型复述一遍可靠得多。

此前的问题是：证据台账（`EvidenceLedger`）只活在内存里，一次 `run()` 结束就
归零，修正轮、重启、跨进程全部失忆。于是「引用必须核实过」这件事只能靠
prompt 反复提醒，或者靠收尾闸门把整篇打回重写。rerun27 就是被这个失忆
锁死的：三轮各读一遍同一批文件，每一轮都被判「从未读过」。

## 落盘结构

```
history/<task>/
  evidence.jsonl                     # append-only：每次读/写一行，带行区间与 sha
  versions/<path 扁平化>/0007-<ts>-<sha>.md   # 每次成功写入的全文快照（覆盖不可能）
  citations.jsonl                    # 每次写盘的引用核查结论（引用数/带行号数/未覆盖数）
```

- `evidence.jsonl` 是读侧事实源：写前门槛、引用核查、行号区间校验都读它。
- `versions/` 是写侧事实源：任何一次覆盖都能回到上一版，`fill_section` 的整节
  替换不再是「丢内容」，只是「多了个版本」。
- 三个文件都是 append-only，崩溃/中断最多丢最后一行，不会读出半截状态。

注意：这是**harness 自己写**的记录，不是给模型写的临时目录——模型不需要
任何权限去写它，也不会因为写它而污染交付物清点。
"""
from __future__ import annotations

import contextvars
import difflib
import hashlib
import json
import re
import time
from pathlib import Path

# 供工具层取用（fs.py 写盘时记版本，无需改每个工具签名）。
# 与 OUTDIR_CTX / WRITE_ALLOW 同款注入方式：_execute 里 set，工具内 get。
RECORDER_CTX: contextvars.ContextVar = contextvars.ContextVar("af_recorder", default=None)

_FLAT_RX = re.compile(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+")


def _flat(rel: str) -> str:
    """把相对路径压成一层目录名：evals/x.md → evals__x.md。"""
    return _FLAT_RX.sub("__", rel.replace("\\", "/").strip("/")) or "file"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def diff_stat(old: str, new: str) -> tuple[int, int]:
    """(新增行, 删除行)——只要一个数字，不存全量 diff。"""
    if old == new:
        return 0, 0
    a, b = old.splitlines(), new.splitlines()
    add = rem = 0
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            rem += 1
    return add, rem


class Recorder:
    """一个任务一份：append-only 记录 + 版本快照。线程安全靠 O_APPEND 单行写。"""

    def __init__(self, root: Path | str, rel_root: Path | str | None = None) -> None:
        self.root = Path(root)
        self.rel_root = Path(rel_root) if rel_root else None
        self.root.mkdir(parents=True, exist_ok=True)
        self._seq: dict[str, int] = {}
        self._whole: dict[str, int] = {}      # 整写次数（乐观并发用，与版本号分开）

    # ---------- 基础 ----------
    def _rel(self, p: Path | str) -> str:
        p = Path(p)
        if self.rel_root is not None:
            try:
                return p.resolve().relative_to(self.rel_root.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        return p.as_posix()

    def _append(self, name: str, row: dict) -> None:
        row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **row}
        try:
            with open(self.root / name, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 记录失败不该让主流程崩

    # ---------- 读 ----------
    def read(self, path: Path | str, ranges: list[tuple[int, int]] | None = None,
             total_lines: int | None = None, chars: int = 0, run: str = "") -> None:
        self._append("evidence.jsonl", {
            "kind": "read", "run": run, "path": self._rel(path),
            "ranges": [[int(a), int(b)] for a, b in (ranges or [])],
            "lines": total_lines, "chars": int(chars),
        })

    def reads(self) -> dict[str, dict]:
        """从 evidence.jsonl 重建「读过哪些文件、读到了哪些行」。"""
        out: dict[str, dict] = {}
        f = self.root / "evidence.jsonl"
        if not f.exists():
            return out
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return out
        for raw in lines:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "read":
                continue
            key = str(row.get("path") or "").replace("\\", "/").lower()
            if not key:
                continue
            rec = out.setdefault(key, {"ranges": [], "lines": row.get("lines"), "reads": 0})
            rec["reads"] += 1
            if row.get("lines"):
                rec["lines"] = row.get("lines")
            for a, b in (row.get("ranges") or []):
                try:
                    rec["ranges"].append((int(a), int(b)))
                except (TypeError, ValueError):
                    continue
        return out

    def ranges_of(self, path: Path | str) -> list[tuple[int, int]]:
        rec = self.reads().get(self._rel(path).lower()) or {}
        return list(rec.get("ranges") or [])

    # ---------- 写 ----------
    def whole_writes(self, path: Path | str) -> int:
        """本任务对该路径**整写**（write_file 整篇覆盖）的次数。

        只数整写：`fill_section` / `write_skeleton` 也各留一个版本，但它们
        是**增量**（一节一行），拿版本数当整写次数会误判——occprobe 里
        w5 写了 1 次 + 填了 3 节，版本号已经到 4，第 2 次整写就被 OCC 拦了。

        用它做写盘侧的乐观并发（见 `agentforge/tools/fs.py` 的 `_OCC_AFTER`）。
        """
        return self._whole.get(self._rel(path).lower(), 0)

    def note_whole_write(self, path: Path | str) -> int:
        """记一次整写，返回这是第几次。"""
        k = self._rel(path).lower()
        n = self._whole.get(k, 0) + 1
        self._whole[k] = n
        return n

    def version(self, path: Path | str, content: str, old: str = "") -> tuple[str, int, int]:
        """给这次写盘留一份全文快照，返回 (相对路径, 新增行, 删除行)。

        版本号按文件递增（0001、0002…），文件名带时间戳与内容 sha：同一秒内
        连写两次也不会互相覆盖，靠文件名本身保证唯一。
        """
        rel = self._rel(path)
        d = self.root / "versions" / _flat(rel)
        d.mkdir(parents=True, exist_ok=True)
        key = rel.lower()
        n = self._seq.get(key)
        if n is None:
            n = len(list(d.glob("*.md"))) + len(list(d.glob("*.txt"))) + len(list(d.glob("*.py"))) \
                + len(list(d.glob("*.json"))) + len(list(d.glob("*.yaml"))) + len(list(d.glob("*.yml")))
        n += 1
        self._seq[key] = n
        suffix = Path(rel).suffix or ".txt"
        name = f"{n:04d}-{time.strftime('%H%M%S')}-{_sha(content)}{suffix}"
        try:
            (d / name).write_text(str(content), encoding="utf-8", newline="")
        except OSError:
            return "", 0, 0
        add, rem = diff_stat(old, str(content))
        self._append("evidence.jsonl", {
            "kind": "write", "path": rel, "version": f"versions/{_flat(rel)}/{name}",
            "chars": len(str(content)), "add": add, "rem": rem,
            "first": not bool(old),
        })
        return f"versions/{_flat(rel)}/{name}", add, rem

    def writes(self) -> list[dict]:
        """本任务所有成功写盘的记录（按路径去重，保留最后一次）。

        这是「产出清单」的文件级事实源：即使某个执行路径忘了把写操作塞进
        file_log（rerun29 的修正轮就是这样，tasks.json 里 files=0），磁盘上
        的记录仍然是全的。任务收尾用它兜底合并进产出清单。
        """
        out: dict[str, dict] = {}
        f = self.root / "evidence.jsonl"
        if not f.exists():
            return []
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for raw in lines:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "write":
                continue
            key = str(row.get("path") or "").replace("\\", "/").lower()
            if not key:
                continue
            prev = out.get(key) or {}
            out[key] = {**row, "first": bool(prev.get("first")) or bool(row.get("first"))}
        return list(out.values())

    def wrote(self, path: Path | str, ok: bool, note: str = "") -> None:
        self._append("evidence.jsonl", {
            "kind": "write_result", "path": self._rel(path), "ok": bool(ok), "note": note[:200],
        })

    # ---------- 引用核查结论 ----------
    def citations(self, path: Path | str, total: int, with_line: int,
                  inside: int, outside: int, version: str = "") -> None:
        self._append("citations.jsonl", {
            "path": self._rel(path), "version": version,
            "refs": total, "with_line": with_line,
            "inside_read": inside, "outside_read": outside,
        })

    def citation_summary(self) -> dict:
        out = {"files": 0, "refs": 0, "with_line": 0, "inside_read": 0, "outside_read": 0}
        f = self.root / "citations.jsonl"
        if not f.exists():
            return out
        try:
            rows = [json.loads(x) for x in f.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
        except (OSError, json.JSONDecodeError):
            return out
        for r in rows:
            out["files"] += 1
            for k in ("refs", "with_line", "inside_read", "outside_read"):
                out[k] += int(r.get(k) or 0)
        return out
