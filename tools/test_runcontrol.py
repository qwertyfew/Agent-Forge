"""停止标志的自测（runcontrol）。

锁的是这个坑：register 对同名任务复用已有的 Event——上一轮把标志立了、
release 又因异常路径漏调的话，重试的同名任务拿到的是已 set 的 Event，
第一轮就误判「要停止」直接退出。修复后：已 set 的旧标志作废，换新的。

运行：python tools/test_runcontrol.py
全部断言内建，退出码 0 = 全过。纯程序测试。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import runcontrol as rc      # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def main() -> None:
    tid = "test-rc-1"

    # 常规生命周期
    ev = rc.register(tid)
    check("注册后未停止", not rc.is_stopping(tid))
    check("request_stop 对在跑任务返回 True", rc.request_stop(tid) is True)
    check("置位后 is_stopping 为 True", rc.is_stopping(tid))
    check("request_stop 对不存在的任务返回 False", rc.request_stop("no-such") is False)
    rc.release(tid)
    check("release 后重新注册是干净标志", not rc.is_stopping(tid))

    # 回归本体：上一轮停止过、release 漏调 → 重试同名任务不得继承停止状态
    ev2 = rc.register(tid)
    ev2.set()                       # 模拟上一轮被停止
    ev3 = rc.register(tid)          # release 漏调，直接再次注册
    check("已 set 的旧标志不复用（换新）", ev3 is not ev2 and not ev3.is_set())
    check("新标志下 is_stopping 为 False", not rc.is_stopping(tid))
    rc.release(tid)

    # 未置位的标志照旧复用（原语义不丢）
    ev4 = rc.register(tid)
    ev5 = rc.register(tid)
    check("未置位时同名注册复用同一 Event", ev4 is ev5)
    rc.release(tid)

    # current_task 的线程隔离：子线程里设置不影响主线程（泳道修复的依据）
    rc.set_current_task("main-task")
    got: list[str] = []
    t = threading.Thread(target=lambda: got.append(rc.current_task()))
    t.start()
    t.join()
    check("子线程看不到主线程的任务归属", got == [""], repr(got))
    rc.set_current_task("sub-task")
    check("set/clear 基线", rc.current_task() == "sub-task")
    rc.clear_current_task()
    check("clear 后为空", rc.current_task() == "")

    print("\n全部通过。")


if __name__ == "__main__":
    main()
