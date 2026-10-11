from __future__ import annotations

import json
import time
from pathlib import Path


def _safe_name(name: str) -> str:
    keep = [c for c in name if c.isalnum() or c in "-_"]
    return "".join(keep)[:60] or "session"


class Session:
    """对话历史的持久化：JSONL，一行一条消息。"""

    def __init__(self, dirpath: Path, name: str | None = None):
        self.dir = Path(dirpath)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.name = _safe_name(name or time.strftime("s%Y%m%d-%H%M%S"))
        self.path = self.dir / f"{self.name}.jsonl"
        self.messages: list[dict] = []

    @classmethod
    def latest(cls, dirpath: Path) -> "Session | None":
        d = Path(dirpath)
        if not d.exists():
            return None
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return None
        s = cls(d, files[0].stem)
        s.load()
        return s

    def load(self) -> "Session":
        self.messages = []
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self.messages.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return self

    def append(self, message: dict) -> None:
        self.messages.append(message)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def clear(self) -> None:
        self.messages = []
        if self.path.exists():
            self.path.unlink()

    @staticmethod
    def list_all(dirpath: Path) -> list[str]:
        d = Path(dirpath)
        if not d.exists():
            return []
        return sorted((p.stem for p in d.glob("*.jsonl")), reverse=True)
