#!/usr/bin/env python3
"""中途补充（Steering）收件箱离线测试：入箱资格、按消费者投递、seen 语义、
清箱、与 runcontrol 的联动；以及 loop/orchestrator 的接线存在性。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agentforge import runcontrol, steering  # noqa: E402
import agentforge.loop as loop_mod  # noqa: E402
import agentforge.orchestrator as orch_mod  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


print("== 入箱资格：没在跑的任务拒收 ==")
check("空 id 拒收", steering.submit("", "补") is False)
check("空文本拒收", steering.submit("t9", "  ") is False)
check("未注册任务拒收", steering.submit("t9", "补一条") is False)

print("== 与 runcontrol 联动：注册即视为在跑 ==")
ev = runcontrol.register("t1")
try:
    check("运行中任务收下", steering.submit("t1", "改成先读 README") is True)
    check("pending 可见", steering.pending("t1") == ["改成先读 README"])

    print("== take：seen 语义（多消费者各见一次，同消费者不重复） ==")
    a = steering.take("t1", "step0")
    check("step0 取到", a == ["改成先读 README"], repr(a))
    check("step0 再取为空", steering.take("t1", "step0") == [])
    b = steering.take("t1", "step1")
    check("step1 也取到（互不抢占）", b == ["改成先读 README"], repr(b))
    c = steering.take("t1", "step2")
    check("step2 也取到", c == ["改成先读 README"], repr(c))
    check("全部投递完 pending 清空", steering.pending("t1") == [])

    print("== 多条保序 + 文本截断 ==")
    steering.submit("t1", "第一")
    steering.submit("t1", "第二")
    got = steering.take("t1", "step0")
    check("保序", got == ["第一", "第二"], repr(got))
    steering.submit("t1", "长" * 3000)
    got2 = steering.take("t1", "step0")
    check("超长截到 2000", len(got2[0]) == 2000, str(len(got2[0])))
finally:
    runcontrol.release("t1")
    steering.clear("t1")

print("== 清箱 ==")
runcontrol.register("t2")
try:
    steering.submit("t2", "残句")
finally:
    runcontrol.release("t2")
steering.clear("t2")
check("clear 后 take 为空", steering.take("t2", "step0") == [])

print("== 接线存在性 ==")
src_loop = inspect.getsource(loop_mod.Agent.run)
check("Agent.run 有 steer_check 参数", "steer_check" in inspect.signature(loop_mod.Agent.run).parameters)
check("run 循环里有 steered 事件", '"steered"' in src_loop)
src_orch = inspect.getsource(orch_mod)
check("steering 入 run_task 单模型路径", 'steering.take(task_id, f"step{step_idx}")' in src_orch)

print(f"\n结果：{PASS} 过 / {FAIL} 挂")
sys.exit(1 if FAIL else 0)
