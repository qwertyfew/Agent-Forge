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

    def _pattern_variants(pattern: str) -> list[str]:
        """给 0 命中的检索生成命名风格变体（正则形式的转义串）。

        实测教训（rerun12）：字面搜「agent loop」搜不到 `Agent(Protocol).run`——
        检索 0 命中被当成「功能不存在」，产出假阴性结论。这里只对「纯词组」
        pattern（无正则元字符）做 snake/camel/kebab/紧凑 四种命名风格互转；
        正则 pattern 语义已由调用方定死，不猜。
        """
        if re.search(r"[\\^$.|?*+()\[\]{}]", pattern):
            return []
        words = [w for w in re.split(r"[\s_-]+", pattern.strip()) if w]
        if not words or len(words) > 4:
            return []
        cands = [
            "_".join(words),                            # snake_case
            "-".join(words),                            # kebab-case
            words[0] + "".join(w.capitalize() for w in words[1:]),  # camelCase
            "".join(w.capitalize() for w in words),     # PascalCase
            "".join(words),                             # 紧凑拼接
        ]
        seen, out = set(), []
        for c in cands:
            if c and c.lower() != pattern.lower() and c.lower() not in seen:
                seen.add(c.lower())
                out.append(re.escape(c))
        return out[:5]

    def search_code(
        pattern: str,
        path: str = ".",
        file_glob: str = "",
        max_results: int = 60,
    ) -> str:
        """按正则在文件中搜索，输出 文件:行号:内容。"""
        root = getattr(guard, "resolve_for_read", guard.resolve)(path)
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
            msg = f"未找到匹配 /{pattern}/ （已扫描 {scanned} 个文件）"
            # 0 命中 ≠ 功能不存在（rerun12 实证：字面「agent loop」搜不到
            # Agent(Protocol).run，导致「未发现会话恢复」的假阴性结论）。
            # 自动复扫命名变体，命中就把证据一并带回，省员工再猜一轮。
            for v in _pattern_variants(pattern):
                try:
                    vrx = re.compile(v, re.IGNORECASE)
                except re.error:
                    continue
                vhits: list[str] = []
                for f in files:
                    try:
                        text = f.read_text(encoding="utf-8", errors="replace")
                    except (OSError, UnicodeDecodeError):
                        continue
                    for i, line in enumerate(text.splitlines(), 1):
                        if vrx.search(line):
                            vhits.append(f"{f}:{i}: {line.strip()[:200]}")
                            if len(vhits) >= 5:
                                break
                    if len(vhits) >= 5:
                        break
                if vhits:
                    return (
                        f"未找到匹配 /{pattern}/，但命名变体 /{v}/ 命中 {len(vhits)} 处"
                        f"（0 命中 ≠ 功能不存在，先拿这些线索继续）：\n" + "\n".join(vhits)
                    )[:max_out]
            return (
                msg
                + "\n[提示] 0 命中 ≠ 功能不存在：换同义词/上位词再搜、读 README 与入口文件"
                "（__init__/main/index）、或用 list_dir 看目录结构后再下「未实现」结论。"
            )[:max_out]
        body = "\n".join(hits)
        return (
            f"匹配 /{pattern}/  命中 {len(hits)} 处（扫描 {scanned} 个文件）\n{body}"
        )[:max_out]

    def find_files(pattern: str, path: str = ".", max_results: int = 100) -> str:
        """按 glob 模式找文件，如 **/*.py。"""
        root = getattr(guard, "resolve_for_read", guard.resolve)(path)
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
