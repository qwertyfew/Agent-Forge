"""只读调用共享缓存的自测：ReadCache 的命中/回放计数/失效/容量/线程安全。

运行：python tools/test_readcache.py
全部断言内建，退出码 0 = 全过。纯程序测试，不需要模型在线。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.readcache import ReadCache  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def main() -> None:
    # ---------- 基本命中与回放计数 ----------
    rc = ReadCache()
    check("C1 未命中返回 None", rc.get("read_file|a") is None)
    rc.put("read_file|a", "内容A")
    hit = rc.get("read_file|a")
    check("C2 首次命中：返回内容且标记第 2 次（第 1 次是真执行）",
          hit == ("内容A", 2), repr(hit))
    hit = rc.get("read_file|a")
    check("C3 再次命中：计数递增", hit == ("内容A", 3), repr(hit))
    check("C4 命中计数器累计", rc.hits == 2, str(rc.hits))

    # ---------- put 幂等：首见为准，不覆盖 ----------
    rc.put("read_file|a", "被覆盖的新内容")
    check("C5 重复 put 不覆盖首见结果", rc.get("read_file|a")[0] == "内容A")

    # ---------- 失效 ----------
    rc.put("list_dir|docs", "目录列表")
    rc.invalidate_all()
    check("C6 失效后全部未命中",
          rc.get("read_file|a") is None and rc.get("list_dir|docs") is None)
    rc.put("read_file|a", "新任务的同名调用")
    check("C7 失效后可重新写入",
          rc.get("read_file|a") == ("新任务的同名调用", 2))

    # ---------- 超大输出不缓存 ----------
    rc.put("read_file|big", "x" * 20_001)
    check("C8 超长输出放弃缓存", rc.get("read_file|big") is None)

    # ---------- 容量上限：淘汰最早写入 ----------
    rc2 = ReadCache()
    for i in range(305):
        rc2.put(f"sig{i:03d}", f"out{i}")
    check("C9 超容量后最早的被淘汰", rc2.get("sig000") is None)
    check("C10 最新的仍在", rc2.get("sig304") == ("out304", 2))

    # ---------- 线程安全：并行泳道并发读写不炸、不丢 ----------
    rc3 = ReadCache()
    errors: list[str] = []

    def worker(k: int) -> None:
        try:
            for i in range(200):
                sig = f"tool{k}|arg{i}"
                rc3.put(sig, f"out-{k}-{i}")
                rc3.get(sig)
                if i % 50 == 0:
                    rc3.invalidate_all()   # 模拟其它泳道执行了写工具
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("C11 六线程并发读写零异常", not errors, "; ".join(errors[:3]))

    print("\n全部断言通过。")


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    except AssertionError:
        print("\n存在失败断言。")
        sys.exit(1)
    print(f"耗时 {time.time() - t0:.1f}s")
