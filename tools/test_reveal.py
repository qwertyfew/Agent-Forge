"""「打开工作区路径并把窗口抬到前台」的自测（agentforge/reveal.py）。

运行：python tools/test_reveal.py
全部断言内建，退出码 0 = 全过。

锁住的是一件事：**点一次就要看到窗口**。这不是样式问题——`os.startfile` 会把资源
管理器打开，但 Windows 的前台锁让窗口停在后面，用户得再点一次任务栏。这里用的办法是
轮询窗口列表 + `AttachThreadInput` 借前台线程资格抬窗口。

窗口枚举与抬窗调用都做了替身：测试不需要真的开资源管理器、也不会抢走你的焦点
（真正抬窗口那一下是人工实测确认的，见 README）。非 Windows 平台上与窗口相关的
断言自动 SKIP。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import reveal  # noqa: E402

IS_WIN = sys.platform == "win32"


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


def skip(name: str) -> None:
    print(f"[SKIP] {name}  —— 仅 Windows 有意义")


class FakeDesktop:
    """替身：把「当前有哪些可见窗口」和「抬窗动作」都握在手里。"""

    def __init__(self, windows):
        self.windows = windows
        self.raised = []

    def install(self, monkeypatch_ok=True):
        reveal._windows = lambda: list(self.windows)          # noqa: SLF001
        reveal._raise_to_front = self._raise                  # noqa: SLF001

    def _raise(self, hwnd):
        self.raised.append(hwnd)
        return hwnd in {h for h, _c, _t in self.windows}


CAB = "CabinetWClass"


def main() -> None:
    # ---------- 文件夹标题匹配（纯函数，任何平台都能跑） ----------
    leaf, full = "agent-forge", r"D:\AI_develop\project\agent-forge"
    check("T1 标题就是文件夹名", reveal._looks_like_folder_title(leaf, leaf, full))
    check("T2 中文资源管理器后缀", reveal._looks_like_folder_title(f"{leaf} - 文件资源管理器", leaf, full))
    check("T3 开了「标题栏显示完整路径」",
          reveal._looks_like_folder_title(f"{full} - 文件资源管理器", leaf, full))
    check("T4 大小写不敏感",
          reveal._looks_like_folder_title("Agent-Forge - 文件资源管理器", leaf, full))
    check("T5 别的文件夹不认",
          not reveal._looks_like_folder_title("novel - 文件资源管理器", leaf, full))
    check("T6 前缀相同但更长的名字不认（只比开头那一段）",
          not reveal._looks_like_folder_title("agent-forge-backup - 文件资源管理器", leaf, full))
    check("T7 空标题不认", not reveal._looks_like_folder_title("", leaf, full))
    check("T8 标题里的路径只认完全相等",
          not reveal._looks_like_folder_title(r"D:\AI_develop\project\agent-forge-old - 文件资源管理器", leaf, full))

    if not IS_WIN:
        skip("T9 起 全部窗口相关断言（含 open_path 调用顺序）")
        print("\n全部断言通过（部分跳过）。")
        return

    # ---------- 文件夹：命中就直接抬，且只抬一次 ----------
    desk = FakeDesktop([(1968236, CAB, "agent-forge - 文件资源管理器")])
    desk.install()
    reveal.WAIT_SECONDS, reveal.POLL_STEP = 0.05, 0.01
    got = reveal._bring_to_front(Path(full))
    check("T9 文件夹：标题命中即抬窗并返回 True", got is True, repr(got))
    check("T10 只抬一次（命中就返回，不重复抢焦点）", desk.raised == [1968236], repr(desk.raised))

    # ---------- 非资源管理器窗口不会被误抬 ----------
    desk = FakeDesktop([(1, "Chrome_WidgetWin_1", "agent-forge - 文件资源管理器")])
    desk.install()
    check("T11 别的程序哪怕标题像也不抬", reveal._bring_to_front(Path(full)) is False)
    check("T12 而且一次都没调用抬窗", desk.raised == [], repr(desk.raised))

    # ---------- 找不到窗口：老老实实超时返回 False ----------
    desk = FakeDesktop([(1, CAB, "novel - 文件资源管理器")])
    desk.install()
    t0 = time.monotonic()
    got = reveal._bring_to_front(Path(full))
    check("T13 找不到目标窗口时返回 False（行为退回改之前）", got is False)
    check("T14 且确实等满了上限才放弃，不是没找就返回",
          time.monotonic() - t0 >= reveal.WAIT_SECONDS * 0.8, f"{time.monotonic() - t0:.3f}s")

    # ---------- 窗口是后来才出现的（新建/导航中）也要等到 ----------
    late = FakeDesktop([(1, CAB, "novel - 文件资源管理器")])
    late.install()
    seq = [[(1, CAB, "novel - 文件资源管理器")],                                    # 第一轮：还没出来
           [(1, CAB, "novel - 文件资源管理器"), (777, CAB, "agent-forge - 文件资源管理器")]]
    polls = {"n": 0}

    def late_windows():
        i = min(polls["n"], len(seq) - 1)
        polls["n"] += 1
        return list(seq[i])

    reveal._windows = late_windows  # noqa: SLF001
    late.windows = list(seq[-1])    # 抬窗替身按最终窗口列表判定
    check("T15 窗口晚一步出现也能等到", reveal._bring_to_front(Path(full)) is True)
    check("T16 第一轮没命中就再轮询一次，命中的是新出现的那个窗口",
          late.raised == [777] and polls["n"] >= 2, f"raised={late.raised} polls={polls['n']}")

    # ---------- 文件：只认标题里带完整文件名的窗口 ----------
    doc = Path(full) / "README.md"
    desk = FakeDesktop([(5, "Chrome_WidgetWin_1", "AgentForge - 用户配置 1 - Microsoft Edge")])
    desk.install()
    check("T17 文件：标题里没有完整文件名就不动（浏览器是同名前缀也不抬）",
          reveal._bring_to_front(doc) is False and desk.raised == [])
    desk = FakeDesktop([(9, "Notepad", "readme.md - 记事本")])
    desk.install()
    check("T18 文件：标题含完整文件名（大小写不同）即抬",
          reveal._bring_to_front(doc) is True and desk.raised == [9], repr(desk.raised))

    # ---------- open_path：先打开、再抬窗，返回抬窗结果 ----------
    calls = []
    reveal.os.startfile = lambda p: calls.append(("open", p))  # type: ignore[assignment]
    desk = FakeDesktop([(42, CAB, "agent-forge - 文件资源管理器")])
    desk.install()
    got = reveal.open_path(Path(full))
    check("T19 open_path 先调系统默认程序打开", calls == [("open", full)], repr(calls))
    check("T20 open_path 返回抬窗结果", got is True and desk.raised == [42], repr(desk.raised))

    print("\n全部断言通过。")


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    except AssertionError:
        print("\n存在失败断言。")
        sys.exit(1)
    print(f"耗时 {time.time() - t0:.1f}s")
