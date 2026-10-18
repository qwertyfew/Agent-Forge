"""任务产出文件追踪。

一个任务跑完，磁盘上可能留下三类东西，其中「中间文件 / 过渡文件 / 结果文件」
都归到这里管：

  1. 工具明确写的文件  —— write_file / edit_file / delete_file，能精确知道是
                          新建、修改还是删除
  2. 命令副作用产生的文件 —— run_command 跑脚本、pip、构建命令时落到磁盘上的，
                          靠「任务开始前后各拍一张工作区索引，两次相减」反推。
                          第 2 类是**推测**：索引相减只能看出「这段时间里它变了」，
                          看不出是谁改的。所以扫之前先过 may_have_written——
                          本任务一次工具都没调、或只调了只读工具时，直接不扫。
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

# 「只读」的风险等级字面量。刻意不 import agentforge.tools.base.SAFE：本模块要
# 保持零依赖（任何测试都能单独加载它），代价就是这一个字面量——tools/test_artifacts.py
# 里有一条断言盯着它与 tools.base.SAFE 同值，改了那边会红。
READONLY_RISK = "safe"


def may_have_written(tool_names, risk_of) -> bool:
    """本次任务有没有可能往磁盘上写过东西。

    「产出文件」里靠扫描相减出来的那一半，分不清「这个文件是本任务写的」和
    「本任务跑着的时候别人改了它」——只按 mtime 相减，等于把任务时间窗内任何
    进程的改动都算在任务头上。实测踩过：一个纯问答任务（一次工具都没调）被记上了
    `docs/evolution.md`，因为跑它的时候恰好有别的编辑器在写这个文件，界面上就成了
    「这个任务返回了一份它没生成的文档」。

    所以先问一句本任务到底动没动手：
      - 一次工具都没调（纯问答）            → 不可能有产出，压根不用扫
      - 只调了只读工具（list_dir/read_file…）→ 同上
      - 调过写类 / 命令类工具                → 扫（脚本副作用只能这样兜）

    risk_of(name) 返回该工具的风险等级；未知工具返回 None，一律当「可能写」——
    宁可多扫一遍把推测交给用户核对，也不要漏掉脚本真正写出来的东西。
    """
    for name in tool_names or ():
        if not name:
            continue
        if risk_of(name) != READONLY_RISK:
            return True
    return False


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
    # certain=True：这条是工具亲口说「我写了这个文件」，是确证而不是推测
    return {"path": str(raw), "action": action, "via": "tool", "tool": tool_name,
            "certain": True}


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
    """对比两次索引，找出任务期间新增/改动的文件。

    结果一律带 `certain: False`：这是**推测**——只知道「这段时间里它变了」，
    不知道是谁改的。调用方必须先过 `may_have_written` 再决定要不要扫。
    """
    out: list[dict] = []
    for key, (path, mtime, size) in after.items():
        old = before.get(key)
        if old is None:
            out.append({"path": path, "action": "created", "via": "scan",
                        "tool": "", "size": size, "certain": False})
        elif abs(old[1] - mtime) > 1e-6:
            out.append({"path": path, "action": "modified", "via": "scan",
                        "tool": "", "size": size, "certain": False})
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
    """这条文件记录是不是「过渡 / 中间文件」（清空任务时默认清理的那类）。

    只按**名字 / 路径模式**判定（tmp / 草稿 / cache / 命令副产物 .log/.bak/.tmp…），
    不再把 via=scan 的文件一刀切当中间文件。

    为什么不能「scan 即中间」：一次「克隆仓库 / 下载并解压」任务会让整个仓库以
    via=scan 落进台账（扫描相减只能看出「这段时间它出现了」，看不出源头）。
    若 scan 一律算中间文件，清空时就会把用户克隆进工作区的 learn-claude-code /
    mini-swe-agent 整个仓库当垃圾删掉——而这些是任务真正的成果，不该被顺手清掉。
    改成按名字判定后：真正的 tmp/draft/cache/日志/备份仍被识别为中间文件；
    克隆来的源码（README.md、code.py、tests/、web/…）归不到中间文件，落到
    「最终产出」一类，默认保留，用户手动勾才删。
    """
    name = str(rec.get("rel") or rec.get("path") or "").replace("\\", "/")
    return bool(_INTERMEDIATE_NAME.search("/" + name))


def describe(records: list[dict], root: str | Path) -> list[dict]:
    """转成给界面用的形态：相对路径 + 动作 + 是否存在。

    `certain` 一并透出：工具亲口写的（True）与扫描相减推出来的（False）在界面上
    要有区别——后者只能说「这段时间它变了」，不能说「这个任务产出了它」。
    """
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
                # 老台账里没有这个字段：按 via 反推（工具记录是确证，其余是推测）
                "certain": bool(r.get("certain", (r.get("via") or "scan") == "tool")),
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
    "may_have_written",
    "rel_to_root",
    "norm",
    "READONLY_RISK",
    "SKIP_DIRS",
]
