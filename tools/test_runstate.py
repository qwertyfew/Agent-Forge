"""运行状态与界面渲染入口的自测护栏。

运行：python tools/test_runstate.py
退出码 0 = 全过。

背景（2026-10-27 的三次事故，都是同一类问题）：
1. 停止后「执行中」角标不灭——重算角标的函数没在轮询里被调用；
2. 追问被当成新任务——send() 里一行 resetChat() 与续接语义冲突；
3. 停止点了没反应——事件流断了就没人收尾。

对应三道护栏：
  A. 运行状态的唯一事实源（agentforge/runstate.py）——状态机语义必须稳定；
  B. 前端运行状态 UI 只有 renderRunUI() 一个渲染入口——不允许再冒出对
     syncTopRun / syncComposer / renderRunBar 的零散调用（静态检查）；
  C. 续接不允许再出现「回看即清空线程」的写法（静态检查）。
"""
from __future__ import annotations

import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.runtime import runcontrol                      # noqa: E402
from agentforge.runtime.runstate import (                      # noqa: E402
    QUEUED, RUNNING, STOPPING, RunState,
)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + "  " + name + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------- A 状态机
def test_state_machine() -> None:
    st = RunState()
    ev = st.register("t1")
    check("登记后进入 running", st.get_status("t1") == RUNNING, st.get_status("t1"))
    check("登记后算在跑", st.running_ids() == ["t1"], str(st.running_ids()))

    st.set_status("t1", QUEUED)
    check("排队也算在跑（占着算力位）", st.running_ids() == ["t1"], str(st.running_ids()))

    st.set_status("t1", RUNNING)
    check("请求停止成功", st.request_stop("t1") is True)
    check("停止标志已立", ev.is_set() and st.is_stopping("t1"))
    check("请求停止后进入 stopping", st.get_status("t1") == STOPPING, st.get_status("t1"))
    check("stopping 不再算「在跑」（与旧口径一致）",
          st.running_ids() == [], str(st.running_ids()))
    check("stopping 仍算未结束（资源还没放）", st.is_active("t1"))

    # 终态不该被改回 stopping
    st.set_status("t1", "done")
    st.request_stop("t1")
    check("终态不被停止请求改写", st.get_status("t1") == "done", st.get_status("t1"))

    check("停止未知任务返回 False", st.request_stop("nope") is False)

    # 已立起的标志必须换新：否则重试的同名任务第一轮就误判「要停止」
    ev2 = st.register("t1")
    check("重复登记会换新标志", ev2 is not ev and not ev2.is_set())

    st.release("t1")
    check("release 后不再算在跑", st.running_ids() == [], str(st.running_ids()))
    check("release 后状态为空", st.get_status("t1") == "", st.get_status("t1"))


def test_current_task_thread_local() -> None:
    st = RunState()
    st.set_current_task("main")
    got = {}

    def worker(tid: str):
        st.set_current_task(tid)
        got[tid] = st.current_task()

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("主线程当前任务不受子线程影响", st.current_task() == "main", st.current_task())
    check("子线程各拿到自己的", got == {"w0": "w0", "w1": "w1", "w2": "w2"}, str(got))
    st.clear_current_task()
    check("清空当前任务", st.current_task() == "")


def test_facade_parity() -> None:
    """runcontrol 门面必须与底层行为一致（老调用方全靠它）。"""
    runcontrol.register("fx")
    check("门面：登记后在跑", runcontrol.running_ids().count("fx") == 1)
    check("门面：请求停止", runcontrol.request_stop("fx") is True)
    check("门面：is_stopping", runcontrol.is_stopping("fx") is True)
    check("门面：停止后不列入 running_ids", "fx" not in runcontrol.running_ids())
    runcontrol.release("fx")
    check("门面：release 后 is_stopping 为假", runcontrol.is_stopping("fx") is False)
    runcontrol.set_current_task("fx")
    check("门面：current_task", runcontrol.current_task() == "fx")
    runcontrol.clear_current_task()
    check("门面：清空当前任务", runcontrol.current_task() == "")


# ---------------------------------------------------------------- B/C 前端静态护栏
def _ui_source() -> str:
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def test_single_render_entry() -> None:
    src = _ui_source()
    # 三个渲染函数只允许在 renderRunUI() 里被直接调用（定义行不算）
    body_lines = []
    for m in re.finditer(r"^(.*)$", src, re.M):
        line = m.group(1)
        if re.search(r"\bfunction\s+(syncTopRun|syncComposer|renderRunBar)\s*\(", line):
            continue                                  # 定义行跳过
        if re.search(r"\b(syncTopRun|syncComposer|renderRunBar)\(\)", line):
            body_lines.append(line.strip())
    inside = [l for l in body_lines if l in ("syncTopRun();", "syncComposer();", "renderRunBar();")]
    check("三个渲染函数只在 renderRunUI 内被调用",
          len(body_lines) == len(inside) == 3,
          f"外部残留调用 {len(body_lines) - len(inside)} 处：{body_lines[:5]}")
    check("renderRunUI 已定义", "function renderRunUI(){" in src)


def test_continuation_guard() -> None:
    src = _ui_source()
    check("追问续接：回看时锚定当前任务（不再无条件 resetChat）",
          "if(viewingHistory && openTaskId){" in src,
          "缺少 viewingHistory && openTaskId 分支")
    # 续接判据只看 threadId：不允许再依赖 taskData 的即时状态（刷新后竞态）
    m = re.search(r"const contOf\s*=\s*([^;]+);", src)
    check("contOf 只依赖 threadId（不依赖 taskData）",
          bool(m) and "taskData" not in m.group(1), m.group(1)[:80] if m else "未找到 contOf")
    check("threadId 落到 sessionStorage", "af_threadId" in src)
    check("刷新后恢复线程", "function restoreThread()" in src)


def main() -> int:
    test_state_machine()
    test_current_task_thread_local()
    test_facade_parity()
    test_single_render_entry()
    test_continuation_guard()
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
