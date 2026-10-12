"""任务级只读工具调用共享缓存。

背景（REA 二测暴露）：每个子任务是独立的 Agent 实例，各自维护私有
call_cache，于是 t2/t3/t4 开工前各 read 一遍 HANDOFF.md、各 list_dir 一遍
docs——同参数只读调用在整任务范围里反复执行，纯烧时间。

这份缓存由编排层创建、在「一次用户请求」的范围里共享（所有泳道的子任务
+ 收尾修正轮）：
- 命中：直接回放上次结果并明说「第 N 次重复」，不再真执行；
- 失效：任何非只读工具（write_file / edit_file / run_command / delete_file…）
  执行后全量作废——防止「读了→改了→再读」拿到改之前的旧内容；
- 线程安全：并行泳道在不同线程里跑，读写都过锁。
"""

from __future__ import annotations

import threading

MAX_ENTRIES = 300         # 超出后淘汰最早写入的一条
MAX_VALUE_CHARS = 20_000  # 超长输出不缓存，防内存膨胀


class ReadCache:
    """同签名只读调用的共享缓存。key 是「工具名|参数指纹」，value 是 (输出, 回放次数)。"""

    def __init__(self) -> None:
        self._d: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        self.hits = 0  # 诊断用：整任务共回放了多少次

    def get(self, sig: str) -> tuple[str, int] | None:
        """命中返回 (上次输出, 第几次重复)；未命中返回 None。计数持久化。"""
        with self._lock:
            v = self._d.get(sig)
            if v is None:
                return None
            self.hits += 1
            self._d[sig] = (v[0], v[1] + 1)   # 回放次数要记住，下次再 +1
            return v[0], v[1] + 1

    def put(self, sig: str, out: str) -> None:
        """写入一份只读结果。超大输出直接放弃缓存。"""
        if not isinstance(out, str) or len(out) > MAX_VALUE_CHARS:
            return
        with self._lock:
            if sig in self._d:
                return                      # 首见为准，不覆盖
            self._d[sig] = (out, 1)
            if len(self._d) > MAX_ENTRIES:  # 淘汰最早写入的一条（dict 保插入序）
                self._d.pop(next(iter(self._d)), None)

    def invalidate_all(self) -> None:
        """任何非只读工具执行后调用：文件系统可能变了，全部作废。"""
        with self._lock:
            self._d.clear()
