"""打开工作区里的文件/文件夹，并（Windows 上）把它的窗口抬到前台。

**为什么需要这个模块**：`os.startfile("D:\\...\\agent-forge")` 只是**请求**资源管理器
打开这个目录。窗口确实开了（前端点一下就有，`/api/open-folder` 也返回 ok），但它
**不会跑到前台**——Windows 有一条「前台锁」：后台进程不许抢焦点，否则任何程序都能
用弹窗盖住你正在看的东西（钓鱼就是这么干的）。

于是现象是：**点了一次好像没反应，得再点一次任务栏上的文件资源管理器窗口**。
实测（10-13）：点击侧栏的工作区路径后，Edge 一直是前台窗口，而资源管理器窗口
（`CabinetWClass`，标题 `agent-forge - 文件资源管理器`）就静静停在后面。

**怎么绕过去**：`AttachThreadInput` 把自己挂到当前前台线程的输入队列上，借它的资格
再调 `SetForegroundWindow`。同一探针实测有效：调用后前台窗口从 Edge 变成了那个
资源管理器窗口。这是「用户刚点了页面上的东西、我们就把他要的窗口抬上来」，
不是后台偷偷抢焦点——**只有用户点击触发的 `/api/open-folder` 会走到这里**。

这些 ctypes 调用只在 Windows 上有意义；其它平台交给 open / xdg-open，返回 False。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from pathlib import Path

_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"

# 资源管理器的两个窗口类。CabinetWClass 是常规文件夹窗口，ExploreWClass 是老版本残留。
_EXPLORER_CLASSES = ("CabinetWClass", "ExploreWClass")

# 等窗口出现/导航完成的时间。文件夹场景通常第一次轮询就命中（窗口早就开在那儿了），
# 撑满这个上限只发生在「窗口还没建出来 / 标题还没更新」的时候。
WAIT_SECONDS = 1.6
POLL_STEP = 0.12

SW_RESTORE = 9

if _IS_WIN:  # pragma: no cover - 平台分支，非 Windows 下不定义
    _u32 = ctypes.windll.user32
    _k32 = ctypes.windll.kernel32
    _ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


# ---------------------------------------------------------------- 打开
def open_path(target: Path) -> bool:
    """用系统默认程序打开 target，并在 Windows 上把它对应的窗口抬到前台。

    返回「是否真的抬到了前台」。打不开（没有关联程序等）会抛 OSError，交给调用方兜底。
    非 Windows 平台只负责打开，返回 False。
    """
    target = Path(target)
    if not _IS_WIN:
        opener = "open" if _IS_MAC else "xdg-open"
        subprocess.Popen([opener, str(target)])  # noqa: S603
        return False
    os.startfile(str(target))  # noqa: S606 - Windows 系统默认程序
    return _bring_to_front(target)


def select_in_explorer(target: Path) -> None:
    """兜底：在资源管理器里把 target 定位出来（选中它），而不是打开它。"""
    if _IS_WIN:
        subprocess.Popen(["explorer", "/select,", str(Path(target))])  # noqa: S603
    else:
        open_path(target)


# ---------------------------------------------------------------- 抬到前台
def _bring_to_front(target: Path) -> bool:
    """轮询找到 target 对应的窗口并抬到前台；找不到就返回 False（行为退回改之前）。"""
    is_dir = target.is_dir()
    leaf = target.name
    full = str(target)
    deadline = time.monotonic() + WAIT_SECONDS

    while True:
        for hwnd, cls, title in _windows():
            if is_dir:
                if cls not in _EXPLORER_CLASSES:
                    continue
                if not _looks_like_folder_title(title, leaf, full):
                    continue
            else:
                # 文件交给它的默认程序：只认标题里**带完整文件名**的窗口，
                # 免得把恰好同名的别的窗口（比如浏览器页签）抬上来。
                if not leaf or leaf.lower() not in (title or "").lower():
                    continue
            if _raise_to_front(hwnd):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_STEP)


def _looks_like_folder_title(title: str, leaf: str, full: str) -> bool:
    """这个窗口标题是不是「目标文件夹」？

    资源管理器的标题通常是 `<文件夹名> - 文件资源管理器`；开了「标题栏显示完整路径」
    时开头是完整路径；个别版本只给文件夹名。都接受，但只认开头那一段——
    否则 `agent-forge-backup` 也会被当成 `agent-forge`。
    """
    if not title:
        return False
    head = title.split(" - ", 1)[0].strip().lower()
    return head in (leaf.lower(), full.lower())


def _raise_to_front(hwnd: int) -> bool:
    """把 hwnd 抬到前台并给焦点。返回 SetForegroundWindow 是否成功。"""
    _u32.ShowWindow(hwnd, SW_RESTORE)          # 最小化过就先还原
    this_thread = _k32.GetCurrentThreadId()
    fg_thread = _u32.GetWindowThreadProcessId(_u32.GetForegroundWindow(), None)
    target_thread = _u32.GetWindowThreadProcessId(hwnd, None)

    attached = []
    for th in (fg_thread, target_thread):
        if th and th != this_thread and _u32.AttachThreadInput(th, this_thread, True):
            attached.append(th)
    try:
        _u32.BringWindowToTop(hwnd)
        ok = bool(_u32.SetForegroundWindow(hwnd))
        _u32.SetActiveWindow(hwnd)
        _u32.SetFocus(hwnd)
    finally:
        for th in attached:                     # 借来的资格要还回去
            _u32.AttachThreadInput(th, this_thread, False)
    return ok


def _windows() -> list[tuple[int, str, str]]:
    """当前所有可见顶层窗口的 (hwnd, 类名, 标题)。"""
    found: list[tuple[int, str, str]] = []

    def _cb(hwnd, _lparam):
        if _u32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            _u32.GetWindowTextW(hwnd, title, 512)
            cls = ctypes.create_unicode_buffer(256)
            _u32.GetClassNameW(hwnd, cls, 256)
            found.append((hwnd, cls.value, title.value))
        return True

    _u32.EnumWindows(_ENUMPROC(_cb), 0)
    return found
