"""流式生成观测哨（P17）。

为什么需要它：评测量的是「这条任务跑成什么样」，但**卡死的那一次什么都没留下**。
`fixb02` / `fixb05` 的 q1 都记成 `480.0s / reply 空 / 0 事件`——没有 token 数、
没有最后一段输出、也没有「最后一次收到字节是几秒前」。能写的只有「超时」，
于是修法只能猜（是生成退化成无限输出？是 Ollama 排队？还是卡在本地串行锁？）。

想补观测，绕不开一个结构性事实：`OllamaClient.chat_stream` 会把整轮增量**缓冲**
起来，确认成功才放给调用方（重试时不重复投递半截内容）。因此生成过程中上层
一个 delta 都看不到，`tools/bench_tasks.py` 的事件统计必然是 0——这不是
「模型没出字」，而是「出了字没人记账」。

于是这里做旁路：每个活跃流一条记录，谁在生成、模型是谁、什么阶段（等锁／已连上／
收到首行／收完）、收了多少行多少字符、最后一次收到字节是何时、尾部文本长什么样。
超时分支直接读 `snapshot()` 写进结果——被墙钟砍掉的路径从此有证词。

不改流式行为、不改重试策略、不进结果文件以外的地方；模块级存放是因为
「被砍掉」这件事发生时，调用栈已经回不来了。
"""

from __future__ import annotations

import threading
import time

_MAX_KEEP = 40      # 最近多少次生成（重试各自一条，够看清一轮里的反复）
_TAIL_CHARS = 280   # 尾部留多少字符：够看出「它在重复写同一段」还是「在正常收尾」

_lock = threading.Lock()
_streams: list[dict] = []
_seq = [0]


def begin(model: str, kind: str = "ollama", *, attempt: int = 1,
          note: str = "") -> dict:
    """开一条记录。返回的句柄交给 tick/phase/finish。

    刻意不抛异常、不做 IO：这是观测，不能因为观测失败拖垮生成。
    """
    with _lock:
        _seq[0] += 1
        h = {
            "id": _seq[0],
            "model": str(model or ""),
            "kind": str(kind or ""),
            "attempt": int(attempt),
            "note": str(note or ""),
            "t0": time.time(),
            "started": time.strftime("%H:%M:%S"),
            "lines": 0,
            "chars": 0,
            "first_line_s": None,   # 距离开流多少秒收到第一行
            "last_line_s": None,    # 最后一行离开流多少秒
            "phases": {},           # 阶段名 → 距离开流多少秒
            "tail": "",
            "done": False,
            "done_reason": "",
            "eval_count": 0,
            "error": "",
            "alive": True,
            "thread": threading.current_thread().name,
        }
        _streams.append(h)
        if len(_streams) > _MAX_KEEP:
            del _streams[: len(_streams) - _MAX_KEEP]
        return h


def phase(h: dict | None, name: str) -> None:
    """记一个阶段时间点（首次写入为准）。

    `lock_wait` / `lock_got` 两段能把「排队等本地串行锁」与「模型真在生成」
    分开——16GB 显存上两条泳道撞锁时，前者能占掉几百秒而 token 数一直是 0。
    """
    if not h:
        return
    with _lock:
        h["phases"].setdefault(str(name), round(time.time() - h["t0"], 3))


def tick(h: dict | None, chars: int = 0, text: str = "") -> None:
    """收到一行：累加行数/字符数、记时间、维护尾部文本。"""
    if not h:
        return
    now = time.time()
    with _lock:
        h["lines"] += 1
        el = round(now - h["t0"], 3)
        if h["first_line_s"] is None:
            h["first_line_s"] = el
        h["last_line_s"] = el
        if chars:
            h["chars"] += int(chars)
        if text:
            tail = h["tail"] + text
            h["tail"] = tail[-_TAIL_CHARS:]


def finish(h: dict | None, *, done: bool = False, done_reason: str = "",
           eval_count: int = 0, error: str = "") -> None:
    """收尾。可重复调用（finally 兜底那次不覆盖已有信息）。"""
    if not h:
        return
    with _lock:
        if done:
            h["done"] = True
            h["done_reason"] = str(done_reason or "")
        if eval_count:
            h["eval_count"] = int(eval_count)
        if error and not h["error"]:
            h["error"] = str(error)[:300]
        h["alive"] = False


def snapshot(limit: int = 6) -> list[dict]:
    """最近若干条记录（浅拷贝，含仍活跃的）。给超时分支读。"""
    with _lock:
        items = _streams[-max(1, int(limit)):]
        now = time.time()
        out = []
        for h in items:
            d = {k: v for k, v in h.items()}
            d["phases"] = dict(h["phases"])
            d["age_s"] = round(now - h["t0"], 1)
            d["idle_s"] = (None if h["last_line_s"] is None
                           else round(now - h["t0"] - h["last_line_s"], 1))
            out.append(d)
        return out


def summary_line(limit: int = 3) -> str:
    """一行摘要：写进超时错误文案，让台账第一眼就能看出属于哪一类。"""
    items = snapshot(limit)
    if not items:
        return "无流记录"
    parts = []
    for h in items:
        ph = h["phases"]
        stage = ("等锁" if "lock_wait" in ph and "lock_got" not in ph else
                 "已连" if "resp" in ph else "请求中")
        parts.append(
            f"{h['model']}#{h['attempt']} {stage} {h['age_s']:.0f}s "
            f"{h['lines']}行/{h['chars']}字"
            + (f" 空闲{h['idle_s']:.0f}s" if h["idle_s"] is not None else "")
            + (" 已完成" if h["done"] else ""))
    return "；".join(parts)


def reset() -> None:
    """清空（测试与长驻进程每轮开跑前用）。"""
    with _lock:
        _streams.clear()


def active_count() -> int:
    with _lock:
        return sum(1 for h in _streams if h["alive"])


__all__ = ["begin", "phase", "tick", "finish", "snapshot", "summary_line",
           "reset", "active_count"]
