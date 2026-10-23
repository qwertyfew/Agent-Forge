"""流式观测哨自测（P17）。

现场：`fixb02` / `fixb05` 的 q1 都记成 `480.0s / reply 空 / 0 事件`，而
`OllamaClient.chat_stream` 会把增量**缓冲**到整轮成功才放出——生成过程中
上层一个 delta 都看不到。于是「超时」成了唯一的现场记录，修法只能猜。

这个套件钉住：观测哨记得准（行数/字符/尾部/阶段）、不改变控制流、
并且真的被接进了 Ollama 流式路径（不是只写了个没用上的模块）。

运行：python tools/test_streamprobe.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests                                            # noqa: E402

from agentforge import llm, streamprobe                    # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


class _Resp:
    """假响应：iter_lines 按给定行吐出。"""

    status_code = 200

    def __init__(self, lines, exc=None):
        self._lines = list(lines)
        self._exc = exc

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = False):
        for ln in self._lines:
            yield ln
        if self._exc is not None:
            raise self._exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_records_lines_and_tail() -> None:
    streamprobe.reset()
    h = streamprobe.begin("m1", "ollama")
    streamprobe.tick(h, chars=3, text="abc")
    streamprobe.tick(h, chars=2, text="de")
    streamprobe.finish(h, done=True, eval_count=7, done_reason="stop")
    s = streamprobe.snapshot(1)[0]
    check("行数与字符数按次累加", s["lines"] == 2 and s["chars"] == 5,
          f"{s['lines']}/{s['chars']}")
    check("尾部文本按到达顺序拼接", s["tail"] == "abcde", s["tail"])
    check("done 与 done_reason 落进记录",
          s["done"] and s["done_reason"] == "stop" and s["eval_count"] == 7)
    check("收尾后不再算活跃", s["alive"] is False)
    check("首行/末行时刻都记了",
          s["first_line_s"] is not None and s["last_line_s"] is not None)


def test_tail_is_capped() -> None:
    streamprobe.reset()
    h = streamprobe.begin("m2")
    streamprobe.tick(h, chars=1000, text="x" * 1000)
    s = streamprobe.snapshot(1)[0]
    check("尾部只留最后一段（不无限增长）", len(s["tail"]) <= 300, str(len(s["tail"])))
    check("字符数仍按实际累加（不只算尾部）", s["chars"] == 1000, str(s["chars"]))


def test_phase_first_write_wins() -> None:
    streamprobe.reset()
    h = streamprobe.begin("m3")
    streamprobe.phase(h, "lock_wait")
    first = h["phases"]["lock_wait"]
    streamprobe.phase(h, "lock_wait")
    check("同一阶段只记首次（不被后续覆盖）",
          h["phases"]["lock_wait"] == first)
    # 只记了 lock_wait、没有 lock_got = 正在等锁：summary 必须能说出来
    check("等锁没拿到时摘要写「等锁」", "等锁" in streamprobe.summary_line(1),
          streamprobe.summary_line(1))
    streamprobe.phase(h, "lock_got")
    check("拿到锁后摘要不再是等锁", "等锁" not in streamprobe.summary_line(1),
          streamprobe.summary_line(1))


def test_finish_is_idempotent() -> None:
    streamprobe.reset()
    h = streamprobe.begin("m4")
    streamprobe.finish(h, error="ModelIdleTimeout: 卡住")
    streamprobe.finish(h)                      # finally 兜底那次
    s = streamprobe.snapshot(1)[0]
    check("finally 兜底不抹掉已有错误", "ModelIdleTimeout" in s["error"], s["error"])
    check("兜底也不重开活跃标记", s["alive"] is False)


def test_snapshot_keeps_order_and_limit() -> None:
    streamprobe.reset()
    for i in range(5):
        streamprobe.begin(f"m{i}")
    snaps = streamprobe.snapshot(2)
    check("取最近 N 条（不是最早 N 条）",
          [x["model"] for x in snaps] == ["m3", "m4"],
          str([x["model"] for x in snaps]))
    check("活跃条数统计正确", streamprobe.active_count() == 5,
          str(streamprobe.active_count()))
    streamprobe.reset()
    check("reset 清空", streamprobe.snapshot(10) == [])


def test_wired_into_ollama_stream() -> None:
    """真跑一遍 OllamaClient.chat_stream：记录必须被写进去，且事件不受影响。"""
    streamprobe.reset()
    lines = [
        json.dumps({"message": {"content": "你好"}}, ensure_ascii=False),
        json.dumps({"message": {"content": "，世界"}}, ensure_ascii=False),
        json.dumps({"message": {"content": ""}, "done": True,
                    "eval_count": 12, "done_reason": "stop",
                    "eval_duration": 1_000_000_000}),
    ]

    # 只换 post（不换整个 requests 模块）：_idle_lines 里 `except requests.Timeout`
    # 用的是模块名，整体替换会让异常类型解析失效。
    real_post = llm.requests.post
    llm.requests.post = lambda url, **kw: _Resp(lines)
    try:
        client = llm.OllamaClient("http://127.0.0.1:11434", idle_timeout=30)
        got = list(client.chat_stream("gemma4:12b", [{"role": "user", "content": "hi"}]))
    finally:
        llm.requests.post = real_post

    kinds = [k for k, _ in got]
    text = "".join(p for k, p in got if k == "delta")
    check("观测不改事件流（正文一字不差）", text == "你好，世界", text)
    check("最后仍产出 result", kinds[-1] == "result", str(kinds))
    s = streamprobe.snapshot(1)[0]
    check("行数 = 实际收到的 chunk 数", s["lines"] == 3, str(s["lines"]))
    check("字符数 = 正文总长", s["chars"] == 5, str(s["chars"]))
    check("尾部是最后一段正文", s["tail"].endswith("，世界"), s["tail"])
    check("done 与 eval_count 被记下",
          s["done"] and s["eval_count"] == 12 and s["done_reason"] == "stop")
    check("模型名记的是真模型", s["model"] == "gemma4:12b", s["model"])
    check("成功路径不留错误", s["error"] == "", s["error"])


def test_wired_when_stream_stalls() -> None:
    """卡在流里（空闲超时）时，记录必须留下「收到几行、最后一行何时、错误是什么」。"""
    streamprobe.reset()
    lines = [json.dumps({"message": {"content": "前半段"}}, ensure_ascii=False)]
    exc = requests.Timeout("Read timed out. (read timeout=30)")

    real_post = llm.requests.post
    llm.requests.post = lambda url, **kw: _Resp(lines, exc=exc)
    try:
        client = llm.OllamaClient("http://127.0.0.1:11434", idle_timeout=30)
        try:
            list(client.chat_stream("gemma4:12b", [{"role": "user", "content": "hi"}]))
        except llm.ModelIdleTimeout:
            pass
        else:  # pragma: no cover
            check("卡住时抛 ModelIdleTimeout", False, "没有抛异常")
    finally:
        llm.requests.post = real_post

    s = streamprobe.snapshot(1)[0]
    check("卡住前收到的内容被记下（不是 0 字）", s["chars"] == 3, str(s["chars"]))
    check("错误被记下且指明是空闲超时", "ModelIdleTimeout" in s["error"], s["error"])
    check("卡住的流不再算活跃", s["alive"] is False)
    check("摘要能看出「收到过内容」",
          "3字" in streamprobe.summary_line(1), streamprobe.summary_line(1))


def main() -> None:
    test_records_lines_and_tail()
    test_tail_is_capped()
    test_phase_first_write_wins()
    test_finish_is_idempotent()
    test_snapshot_keeps_order_and_limit()
    test_wired_into_ollama_stream()
    test_wired_when_stream_stalls()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
