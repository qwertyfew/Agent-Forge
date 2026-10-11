from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from .base import SAFE, make_tool

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".next", "target", "bin", "obj", ".gradle",
}


def build_search_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    def search_code(
        pattern: str,
        path: str = ".",
        file_glob: str = "",
        max_results: int = 60,
    ) -> str:
        """按正则在文件中搜索，输出 文件:行号:内容。"""
        root = guard.resolve(path)
        if not root.exists():
            return f"[错误] 路径不存在: {root}"
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"[错误] 正则非法: {e}"

        files: list[Path] = [root] if root.is_file() else []
        if root.is_dir():
            for p in root.rglob("*"):
                if any(part in SKIP_DIRS for part in p.parts):
                    continue
                if p.is_file() and p.stat().st_size < 2_000_000:
                    if file_glob and not fnmatch.fnmatch(p.name, file_glob):
                        continue
                    files.append(p)
                if len(files) > 4000:
                    break

        hits: list[str] = []
        scanned = 0
        for f in files:
            scanned += 1
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line.strip()[:300]}")
                    if len(hits) >= int(max_results):
                        break
            if len(hits) >= int(max_results):
                break

        if not hits:
            return f"未找到匹配 /{pattern}/ （已扫描 {scanned} 个文件）"
        body = "\n".join(hits)
        return (
            f"匹配 /{pattern}/  命中 {len(hits)} 处（扫描 {scanned} 个文件）\n{body}"
        )[:max_out]

    def find_files(pattern: str, path: str = ".", max_results: int = 100) -> str:
        """按 glob 模式找文件，如 **/*.py。"""
        root = guard.resolve(path)
        if not root.exists():
            return f"[错误] 路径不存在: {root}"
        out: list[str] = []
        for p in root.glob(pattern):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            kind = "DIR " if p.is_dir() else "FILE"
            out.append(f"  [{kind}] {p}")
            if len(out) >= int(max_results):
                break
        if not out:
            return f"未找到匹配 {pattern} 的文件（根目录 {root}）"
        return f"匹配 {pattern}  共 {len(out)} 项\n" + "\n".join(out)

    return [
        make_tool(
            "search_code",
            "按正则表达式搜索代码/文本内容，返回 文件:行号:内容。定位函数定义、报错字符串、配置项时用它。",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式，如 def\\s+login 或 TODO"},
                    "path": {"type": "string", "description": "搜索根目录，默认工作区根目录"},
                    "file_glob": {"type": "string", "description": "限定文件名，如 *.py，留空表示不限"},
                    "max_results": {"type": "integer", "description": "最多返回多少条，默认 60"},
                },
                "required": ["pattern"],
            },
            search_code,
            SAFE,
        ),
        make_tool(
            "find_files",
            "按 glob 模式查找文件，如 **/*.py、*.md。",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
                    "path": {"type": "string", "description": "起始目录，默认工作区根目录"},
                    "max_results": {"type": "integer", "description": "最多返回多少条，默认 100"},
                },
                "required": ["pattern"],
            },
            find_files,
            SAFE,
        ),
    ]
