"""会话事件日志（Journal）：让「模型看到什么」能被程序重建。

dsh 的原则是「模型可见即已记录」——每个模型请求都必须能从事件日志重建。
AgentForge 此前只有一个半吊子版本：`tasksession.py` 只在 leader 路径创建，
而 `Agent.run` 内部真正的模型输入（系统提示、逐轮消息、压缩结果）从不落盘。
一旦触发压缩，`context.Compressor` 直接改内存里的 messages，日志里没有痕迹——
事后无法回答「模型当轮到底看到的是原文还是摘要」，评测归因只能靠猜。

这里补一条最小但完整的账：

  run_start   本次 run 的固定输入：模型、任务原文、系统提示指纹
  system      系统提示变化时记录全文（warmup / skill 注入会让它变）
  message     逐条追加的模型可见消息（user / assistant / tool，含 tool_calls）
  compact     压缩事件：策略、前后体积、**压缩前/后的完整快照路径**

设计约束：
- 只追加，不改不删（与 tasksession、events.jsonl 同一套语义）；
- 旁路：任何 IO 失败都吞掉，绝不因为记账失败让任务挂掉；
- 单条内容超上限截断（标 `truncated` 与原始长度），避免一次 read 大文件
  把日志撑爆；截断只影响逐字重放，事件序列与快照永远是完整的；
- 快照单独落 JSON，事件里只存路径——日志本身的体积与上下文长度无关。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

# 单条消息内容的落盘上限。超过就截断（重放时能看出被截过）。
MAX_CHARS = 200_000

_SAFE_RX = re.compile(r"[^A-Za-z0-9_\-.]")


def _safe(sid: str) -> str:
    return _SAFE_RX.sub("_", str(sid or ""))[:80] or "run"


def new_sid(prefix: str = "run") -> str:
    """生成一个不会在同秒内撞车的会话 id。"""
    return f"{prefix}-{time.strftime('%m%d-%H%M%S')}-{os.getpid() % 1000:03d}-{int(time.time()*1000) % 1000:03d}"


class Journal:
    """一次 run 的事件日志。用 `Journal.open(cfg, sid)` 构造，失败返回 None。"""

    def __init__(self, base: Path) -> None:
        self.dir = Path(base)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self.sid = self.dir.name
        self._seq = 0
        # 每次都开后即关：run 可能在生成器里被提前放弃（用户停止、异常），
        # 长期持有句柄既会泄漏，在 Windows 上还会挡住临时目录清理。
        # 单次 open 的代价远小于一次模型调用，不值得为它冒这个险。
        self._ok = True

    @classmethod
    def open(cls, cfg, sid: str = "", prefix: str = "run") -> "Journal | None":
        """在 `<state>/journal/<sid>/` 下开一份日志。cfg.journal=False 时关闭。"""
        try:
            if not getattr(cfg, "journal", True):
                return None
            base = Path(cfg.state()) / "journal" / _safe(sid or new_sid(prefix))
            return cls(base)
        except Exception:  # noqa: BLE001  记账是旁路，不能拖垮任务
            return None

    # ---------- 写 ----------
    def append(self, etype: str, **fields) -> None:
        if not self._ok:
            return
        self._seq += 1
        rec = {"i": self._seq, "ts": round(time.time(), 3), "type": etype}
        for k, v in fields.items():
            if isinstance(v, str) and len(v) > MAX_CHARS:
                rec[k] = v[:MAX_CHARS]
                rec[k + "_truncated"] = True
                rec[k + "_len"] = len(v)
            else:
                rec[k] = v
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            self._ok = False

    def snapshot(self, name: str, payload) -> str:
        """把大块内容单独落盘，返回路径（失败返回空串）。"""
        try:
            p = self.dir / _safe(name + ".json")
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return str(p)
        except (OSError, TypeError, ValueError):
            return ""

    def message(self, msg: dict, source: str = "") -> None:
        """记录一条模型可见消息（原样存，保证 tool_calls 等结构不丢）。"""
        if not isinstance(msg, dict):
            return
        rec = dict(msg)
        if source:
            rec["_source"] = source
        self.append("message", role=rec.get("role"), msg=rec)

    def close(self) -> None:
        """兼容旧调用：句柄不再常驻，这里只标记停止写入。"""
        self._ok = False


# ---------------------------------------------------------------- 重放
def replay(path: str | Path) -> dict:
    """把事件日志折叠成「模型当时看到的上下文」。

    返回 {"system": 最后生效的系统提示, "messages": [...], "compacts": n}。
    压缩事件用快照整体替换 messages —— 与 dsh 的 Surface replacement 同义：
    模型之后看到的是摘要，原来的长历史仍完整留在此前的 message 事件里。
    """
    out = {"system": "", "messages": [], "compacts": 0, "events": 0}
    p = Path(path)
    if p.is_dir():
        p = p / "events.jsonl"
    if not p.exists():
        return out
    msgs: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        out["events"] += 1
        t = e.get("type")
        if t == "system":
            out["system"] = e.get("content") or ""
        elif t == "message":
            m = e.get("msg") or {}
            if isinstance(m, dict):
                msgs.append({k: v for k, v in m.items()
                             if not k.startswith("_") and not k.endswith("_truncated")})
        elif t == "compact":
            out["compacts"] += 1
            snap = e.get("after_snapshot") or ""
            if snap:
                try:
                    data = json.loads(Path(snap).read_text(encoding="utf-8"))
                    msgs = list(data.get("messages") or [])
                except (OSError, ValueError):
                    pass
    out["messages"] = msgs
    return out
