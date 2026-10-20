# -*- coding: utf-8 -*-
"""interact.ask_user 超时降级 + ask 预算 的自测。

运行：python tools/test_interact.py
全部断言内建，退出码 0 = 全过。**不联网、不弹窗**——全部走线程本地模式。

用例取自 rerun50 实证（用户裁定 2026-10-20）：无人值守评测跑一半 ask_user
弹窗挂死 4.5 分钟，根因是 webui 无条件 `interact.begin(interactive=True)`，
而派发脚本发起方从没声明「无用户在场」。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import interact  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def test_no_user_declared_skips_popup() -> None:
    """无人值守（begin(no_user=True)）→ ask_user 不弹窗、不阻塞，直接降级。"""
    interact.begin(interactive=True, no_user=True)
    try:
        t0 = time.time()
        out = interact.ask("您说的 X 是指什么？", ["A", "B"])
        dt = time.time() - t0
    finally:
        interact.end()
    check("no_user 不阻塞（立刻返回，不等弹窗）", dt < 0.5, f"{dt:.2f}s")
    check("降级提示写明「无用户在场」", "无用户在场" in out or "无用户作答" in out, out)
    check("降级提示要求自己假设并注明", "假设" in out, out)


def test_interactive_timeout_short() -> None:
    """交互模式有用户时也该有超时上限——弹窗不该比任务本身还长。"""
    interact.begin(interactive=True, no_user=False)
    try:
        # 用极短 timeout 直接调 ask（绕过 DEFAULT_TIMEOUT）
        t0 = time.time()
        out = interact.ask("在吗？", ["是", "否"], timeout=0.3)
        dt = time.time() - t0
    finally:
        interact.end()
    check("超时短于任务本身（不会挂死）", dt < 2.0, f"{dt:.2f}s")
    check("超时提示写明超时并要求假设", "超时" in out and "假设" in out, out)
    check("默认超时不是旧的 600s（已收紧）",
          interact.DEFAULT_TIMEOUT <= 180, str(interact.DEFAULT_TIMEOUT))


def test_ask_budget() -> None:
    """ask 预算：用完 N 次后自动降级，防止反复弹窗把活儿丢给用户。"""
    interact.begin(interactive=True, no_user=False, ask_budget=2)
    try:
        a1 = interact.ask("第一问？", ["是", "否"], timeout=0.2)
        a2 = interact.ask("第二问？", ["是", "否"], timeout=0.2)
        a3 = interact.ask("第三问？", ["是", "否"], timeout=0.2)   # 应被预算拦下
        a4 = interact.ask("第四问？", ["是", "否"], timeout=0.2)   # 同样
    finally:
        interact.end()
    check("前 N 次照常（超时降级）", "假设" in a1 and "假设" in a2, f"{a1!r} | {a2!r}")
    check("第 N+1 次被预算拦下", "澄清次数已用完" in a3, a3)
    check("预算拦下时也不阻塞", "澄清次数已用完" in a4, a4)
    check("预算拦下时要求停止再问", "不要再调 ask_user" in a3, a3)


def test_budget_zero_skips_all() -> None:
    """ask_budget=0 → 全部直接降级（最严的无人值守口径）。"""
    interact.begin(interactive=True, no_user=False, ask_budget=0)
    try:
        out = interact.ask("在吗？", ["是", "否"], timeout=0.2)
    finally:
        interact.end()
    check("预算 0 时一次都不弹窗", "澄清次数已用完" in out, out)


def test_scripted_answers_still_work() -> None:
    """评测脚本模式（ask_answers）不受影响——按序弹答案。"""
    interact.begin(answers=["第一个答案", "第二个答案"])
    try:
        a1 = interact.ask("Q1?", ["a", "b"])
        a2 = interact.ask("Q2?", ["a", "b"])
        a3 = interact.ask("Q3?", ["a", "b"])   # 脚本用完 → 无用户作答
    finally:
        interact.end()
    check("脚本答案按序返回", a1 == "第一个答案" and a2 == "第二个答案", f"{a1!r} {a2!r}")
    check("脚本用完后降级（无用户作答）", "无用户作答" in a3, a3)


def test_auto_answer_wins() -> None:
    """任务现场自动答复优先级最高（file 下落类追问）。"""
    interact.begin(interactive=True, no_user=True)
    interact.set_auto_answer(lambda q, o: "[系统自动答复] 文件已生成，直接读取即可。")
    try:
        out = interact.ask("找不到文件 x.md，它在哪？", ["路径A", "路径B"])
    finally:
        interact.clear_auto_answer()
        interact.end()
    check("自动答复优先于 no_user 降级", "系统自动答复" in out, out)


def test_end_clears_state() -> None:
    """end() 后再 ask → 退回默认无用户降级（不串到同线程的下一次运行）。"""
    interact.begin(interactive=True, no_user=False, ask_budget=5)
    interact.end()
    out = interact.ask("在吗？", ["是", "否"])
    check("end() 后清掉模式（默认降级、不弹窗）",
          "无用户作答" in out or "无用户在场" in out, out)


def test_concurrent_pending_not_deadlock() -> None:
    """多线程并发 ask 不互相死锁（线程本地预算各自计）。"""
    results: dict[str, str] = {}

    def worker(name: str, no_user: bool):
        interact.begin(interactive=True, no_user=no_user)
        try:
            results[name] = interact.ask(f"{name} 问？", ["a", "b"], timeout=0.2)
        finally:
            interact.end()

    t1 = threading.Thread(target=worker, args=("unattended", True))
    t2 = threading.Thread(target=worker, args=("attended", False))
    t1.start(); t2.start()
    t1.join(timeout=3); t2.join(timeout=3)
    check("两个线程都返回了", set(results) == {"unattended", "attended"}, repr(results))
    check("无人值守线程降级", "无用户" in results.get("unattended", ""), results.get("unattended", ""))
    check("有用户线程超时降级", "超时" in results.get("attended", ""), results.get("attended", ""))


def main() -> None:
    test_no_user_declared_skips_popup()
    test_interactive_timeout_short()
    test_ask_budget()
    test_budget_zero_skips_all()
    test_scripted_answers_still_work()
    test_auto_answer_wins()
    test_end_clears_state()
    test_concurrent_pending_not_deadlock()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
