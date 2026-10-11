"""长篇小说写作工具族。

长篇（10万+ 字）远超单次对话的上下文容量，正确的做法是**文件即真相**：
大纲、人物、世界观、各章节全部落盘，每章独立成文件，任何一轮对话只
加载需要的部分。本模块提供落盘工作流需要的两个工具：

- novel_compile  把章节目录按序合成全书文件（txt/md），汇报字数
- novel_stats    统计章节字数与总进度
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import SAFE, WRITE, make_tool

_TEXT_SUFFIX = {".md", ".txt"}
_INT_SPLIT = re.compile(r"(\d+)")

# 中文字符 + 英文单词的混合计数
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _natural_key(p: Path) -> list:
    """chapter2 排在 chapter10 前面的自然排序。"""
    return [int(t) if t.isdigit() else t.lower() for t in _INT_SPLIT.split(p.name)]


def _count_words(text: str) -> int:
    """出版业口径：汉字数 + 英文单词数。"""
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


def _chapter_files(d: Path) -> list[Path]:
    return sorted(
        [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in _TEXT_SUFFIX],
        key=_natural_key,
    )


def build_novel_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    # ---------- novel_stats ----------
    def novel_stats(path: str = ".", target_words: int = 0) -> str:
        p = guard.resolve(path)
        if not p.exists():
            return f"[错误] 路径不存在: {p}"
        files = [p] if p.is_file() else _chapter_files(p)
        if not files:
            return f"[错误] {p} 下没有 .md/.txt 文件"
        rows, total = [], 0
        for f in files:
            try:
                n = _count_words(f.read_text(encoding="utf-8", errors="replace"))
            except OSError as e:
                return f"[错误] 读取失败: {e}"
            total += n
            rows.append(f"  {f.name:<40} {n:>7,} 字")
        head = f"{p}\n共 {len(files)} 个文件，{total:,} 字"
        if target_words:
            pct = min(100.0, total / max(1, int(target_words)) * 100)
            head += f"（目标 {int(target_words):,} 字，进度 {pct:.1f}%）"
        out = head + "\n" + "\n".join(rows)
        return out[:max_out]

    # ---------- novel_compile ----------
    def novel_compile(dir_path: str, output: str, title: str = "") -> str:
        d = guard.resolve(dir_path)
        if not d.is_dir():
            return f"[错误] 目录不存在: {d}"
        files = _chapter_files(d)
        if not files:
            return f"[错误] {d} 下没有 .md/.txt 章节文件"
        dest = guard.resolve(output)
        if dest.suffix.lower() not in _TEXT_SUFFIX:
            return "[错误] output 必须是 .md 或 .txt 文件"
        if dest.resolve() in {f.resolve() for f in files}:
            return "[错误] 输出文件不能是章节文件本身，请换个名字"
        parts, total = [], 0
        for i, f in enumerate(files, 1):
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            n = _count_words(text)
            total += n
            header = f"# {f.stem}" if not text.lstrip().startswith("#") else ""
            parts.append("\n\n".join(x for x in (header, text) if x))
            parts.append("")  # 章节间空行
        if title:
            parts.insert(0, f"# {title}\n")
        body = "\n".join(parts)
        dest.write_text(body, encoding="utf-8", newline="")
        return (
            f"已合成 {len(files)} 章 → {dest}\n"
            f"总字数: {total:,}（平均每章 {total // len(files):,} 字）\n"
            f"各章: " + ", ".join(f"{f.stem}:{_count_words(f.read_text(encoding='utf-8', errors='replace')):,}" for f in files)
        )

    return [
        make_tool(
            "novel_stats",
            "统计小说章节文件的字数与总进度。写作过程中随时用它核对每章篇幅和全书进度。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "章节目录或单个文件，默认工作区根目录"},
                    "target_words": {"type": "integer", "description": "全书目标字数，填了会显示完成百分比"},
                },
                "required": [],
            },
            novel_stats,
            SAFE,
        ),
        make_tool(
            "novel_compile",
            "把章节目录里的 .md/.txt 按自然顺序（chapter2 排在 chapter10 前）合成一个全书文件，并汇报各章与总字数。",
            {
                "type": "object",
                "properties": {
                    "dir_path": {"type": "string", "description": "章节所在目录"},
                    "output": {"type": "string", "description": "输出的全书文件路径（.md 或 .txt）"},
                    "title": {"type": "string", "description": "全书标题，作为文首大标题"},
                },
                "required": ["dir_path", "output"],
            },
            novel_compile,
            WRITE,
        ),
    ]
