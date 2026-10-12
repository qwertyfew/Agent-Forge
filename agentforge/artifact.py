"""任务产出文件追踪。

一个任务跑完，磁盘上可能留下三类东西，其中「中间文件 / 过渡文件 / 结果文件」
都归到这里管：

  1. 工具明确写的文件  —— write_file / edit_file / delete_file，能精确知道是
                          新建、修改还是删除
  2. 命令副作用产生的文件 —— run_command 跑脚本、pip、构建命令时落到磁盘上的，
                          靠「任务开始前后各拍一张工作区索引，两次相减」反推
  3. 记忆文件          —— 单独由 memory.MemoryStore 管理，不在这里

本模块只做「发现与描述」，不负责删除。删除在 webui 里按用户勾选执行，
且强制限制在工作区内。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 扫描时直接跳过的目录：要么是依赖/缓存，要么是工具自身的运行痕迹，
# 它们每天都在变，纳进来只会让文件清单变成噪音。
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", ".next",
    ".nuxt", "dist", "build", "target", ".cache", ".gradle", ".tox",
    ".run", ".sessions", ".agentforge", ".tmp_research",
}

MAX_DEPTH = 8
MAX_INDEX = 4000         # 索引最多记多少文件，防异常仓库拖慢任务收尾

# 只有这几种工具会直接改文件；其余工具靠目录扫描兜底
FILE_TOOLS = {"write_file": "write", "edit_file": "edit", "delete_file": "delete"}


def norm(path: str | Path) -> str:
    """跨平台的路径归一化，用来去重（Windows 大小写不敏感）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def rel_to_root(root: str | Path, path: str | Path) -> str | None:
    """返回相对工作区的路径；不在工作区内则返回 None。"""
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return None


def classify_tool_write(tool_name: str, args: dict, output: str) -> dict | None:
    """把一次写类工具的调用结果翻译成文件记录；不适合记录时返回 None。"""
    kind = FILE_TOOLS.get(tool_name)
    if kind is None:
        return None
    raw = (args or {}).get("path")
    if not raw:
        return None
    text = str(output or "")
    if kind == "delete":
        action = "deleted" if "已删除" in text else None
    elif kind == "edit":
        action = "modified" if "已修改" in text else None
    else:  # write_file
        if "已新建" in text:
            action = "created"
        elif "已覆盖" in text:
            action = "modified"
        else:
            action = None
    if action is None:
        # 工具报错了（参数不对、文件不存在…），不要记成产出
        return None
    return {"path": str(raw), "action": action, "via": "tool", "tool": tool_name}


def snapshot(root: str | Path, cap: int = MAX_INDEX) -> dict[str, tuple[str, float, int]]:
    """给工作区拍一张轻量索引：{归一化路径: (真实路径, mtime, 大小)}。

    任务开始前拍一张、结束后拍一张，两次相减就知道这期间磁盘上多了什么、
    改了什么。只做 stat 不读内容，几百个文件也就几十毫秒。
    """
    base = Path(root)
    index: dict[str, tuple[str, float, int]] = {}
    if not base.is_dir():
        return index
    base_depth = len(base.parts)
    stack: list[Path] = [base]
    while stack and len(index) < cap:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for ent in entries:
            try:
                if ent.is_dir(follow_symlinks=False):
                    if ent.name in SKIP_DIRS or ent.name.startswith("."):
                        continue
                    if len(Path(ent.path).parts) - base_depth >= MAX_DEPTH:
                        continue
                    stack.append(Path(ent.path))
                    continue
                if not ent.is_file(follow_symlinks=False):
                    continue
                # 隐藏文件不算产出：台账 .tasks.json、偏好 .prefs.json 这类
                # 工具自身文件每天都在变，纳进来纯属噪音。任务显式写了隐藏
                # 文件时，工具钩子那边照样会记下来。
                if ent.name.startswith("."):
                    continue
                st = ent.stat()
            except OSError:
                continue
            index[norm(ent.path)] = (ent.path, float(st.st_mtime), int(st.st_size))
            if len(index) >= cap:
                break
    return index


def diff_snapshot(
    before: dict[str, tuple[str, float, int]],
    after: dict[str, tuple[str, float, int]],
) -> list[dict]:
    """对比两次索引，找出任务期间新增/改动的文件。"""
    out: list[dict] = []
    for key, (path, mtime, size) in after.items():
        old = before.get(key)
        if old is None:
            out.append({"path": path, "action": "created", "via": "scan",
                        "tool": "", "size": size})
        elif abs(old[1] - mtime) > 1e-6:
            out.append({"path": path, "action": "modified", "via": "scan",
                        "tool": "", "size": size})
    return out


def merge(records: list[dict]) -> list[dict]:
    """合并精确记录与扫描结果：同一路径只留一条，精确的（tool）优先。

    扫描结果里 `action` 只是「在任务期间被改动过」的推断，而工具记录是确定的，
    所以以工具记录为准。
    """
    out: dict[str, dict] = {}
    for r in records:
        if not r:
            continue
        key = norm(r["path"])
        old = out.get(key)
        if old is None:
            out[key] = dict(r)
            continue
        if old.get("via") != "tool" and r.get("via") == "tool":
            out[key] = dict(r)
        elif old.get("action") == "modified" and r.get("action") == "created":
            # 先被扫到「改动」、后由工具确认是「新建」，取更准的那个
            old["action"] = "created"
    return list(out.values())


# 中间/过渡文件特征（清空任务时只删这类、保留最终产出）：
#   - 名字带临时语义：tmp/temp/draft/scratch/debug/cache 开头或独立成分
#     （要求后面跟边界，避免误伤 template / temporary 里的 "temp"）
#   - 名字带中文临时语义：草稿/中间/过渡/临时/备份
#   - 明显的临时扩展名：.tmp/.bak/.orig/.part/.swp/.log
# 命令副作用（via=scan）也算中间文件：用户没有点名要它，多半是脚本/构建的副产品。
_INTERMEDIATE_NAME = re.compile(
    r"(^|[/\\])(tmp|temp|drafts?|scratch|debug|cache)([/\\\-_.\d ]|$)"
    r"|[/\\][^/\\]*?(?:草稿|中间|过渡|临时|备份)[^/\\]*$"
    r"|\.(tmp|bak|orig|part|swp|log)$",
    re.I,
)


def is_intermediate(rec: dict) -> bool:
    """这条文件记录是不是「过渡 / 中间文件」（清空任务时默认清理的那类）。"""
    if (rec.get("via") or "scan") == "scan":
        return True
    name = str(rec.get("rel") or rec.get("path") or "").replace("\\", "/")
    return bool(_INTERMEDIATE_NAME.search("/" + name))


def describe(records: list[dict], root: str | Path) -> list[dict]:
    """转成给界面用的形态：相对路径 + 动作 + 是否存在。"""
    out = []
    for r in records or []:
        p = Path(r.get("path") or "")
        rel = rel_to_root(root, p) if p else None
        out.append(
            {
                "path": str(p),
                "rel": rel if rel is not None else str(p),
                "action": r.get("action") or "modified",
                "via": r.get("via") or "scan",
                "tool": r.get("tool") or "",
                "exists": p.exists(),
                "outside": rel is None,
            }
        )
    return out


__all__ = [
    "classify_tool_write",
    "snapshot",
    "diff_snapshot",
    "merge",
    "describe",
    "is_intermediate",
    "rel_to_root",
    "norm",
    "SKIP_DIRS",
]
