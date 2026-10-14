"""命令黑名单闸门的自测（permissions.Guard.check_command）。

锁的是这个坑：黑名单自我定位是「两种权限模式下都拦的底线」，但匹配按原样
做子串查找——「del  /s」（双空格）、「del\t/s」（Tab）、「rm  -rf」这类空白
变体全部漏网。修复后匹配前先把连续空白归一成单个空格（黑名单条目同样归一）。

运行：python tools/test_permissions.py
全部断言内建，退出码 0 = 全过。纯程序测试，不需要模型在线。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config                  # noqa: E402
from agentforge.permissions import Guard, PermissionDenied  # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def make_guard() -> Guard:
    cfg = Config()
    cfg.command_blocklist = [
        "format", "del /s", "del /f", "rm -rf", "rmdir /s", "rd /s",
        "remove-item -recurse", "shutdown", "diskpart", "reg delete", "bcdedit",
    ]
    return Guard(cfg)


def main() -> None:
    g = make_guard()

    # 基线：白命令放行（原有行为不得被归一化误伤）
    for ok in ("python tools/x.py", "git status", "dir docs", "pytest -q"):
        try:
            g.check_command(ok)
            check(f"放行：{ok}", True)
        except PermissionDenied as e:
            check(f"放行：{ok}", False, str(e))

    # 原有行为：标准写法照旧拦截
    for bad in ("del /s docs", "rm -rf build", "rmdir /s /q docs",
                "rd /s /q docs", "remove-item -recurse docs"):
        try:
            g.check_command(bad)
            check(f"拦截：{bad}", False, "没有被拦下")
        except PermissionDenied:
            check(f"拦截：{bad}", True)

    # 回归本体：空白变体不得绕过（修复前这三条全部漏网）
    for bad in ("del  /s docs",          # 双空格
                "del\t/s docs",          # Tab
                "echo hi && del  /f x",  # 链式命令里的双空格
                "rm  -rf build",
                "rm\t-rf build",
                "git add . && rm   -rf .git"):
        try:
            g.check_command(bad)
            check(f"空白变体拦下：{bad!r}", False, "没有被拦下")
        except PermissionDenied:
            check(f"空白变体拦下：{bad!r}", True)

    # 黑名单条目自身带多余空白 / 大小写变体也要能对上（条目侧同样归一）
    cfg2 = Config()
    cfg2.command_blocklist = ["del  /s", "RM  -RF"]
    g2 = Guard(cfg2)
    for bad in ("del /s docs", "rm -rf build"):
        try:
            g2.check_command(bad)
            check(f"条目归一后仍拦：{bad}", False, "没有被拦下")
        except PermissionDenied:
            check(f"条目归一后仍拦：{bad}", True)

    # 前缀语义保持：黑名单词打头的命令照样拦（startswith 路径）
    try:
        g.check_command("del /s")
        check("前缀拦截：del /s", False, "没有被拦下")
    except PermissionDenied:
        check("前缀拦截：del /s", True)

    print("\n全部通过。")


if __name__ == "__main__":
    main()
