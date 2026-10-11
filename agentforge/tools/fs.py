from __future__ import annotations

from pathlib import Path

from .base import SAFE, WRITE, make_tool

TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".ps1", ".bat", ".sql", ".html",
    ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md",
    ".txt", ".rst", ".csv", ".xml", ".gradle", ".dockerfile", ".env",
}


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]"


def build_fs_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    # ---------- read_file ----------
    def read_file(path: str, offset: int = 1, limit: int = 800) -> str:
        p = guard.resolve(path)
        if not p.exists():
            return f"[错误] 文件不存在: {p}"
        if p.is_dir():
            return f"[错误] 这是目录不是文件: {p}"
        if p.suffix.lower() not in TEXT_SUFFIXES and p.stat().st_size > 2_000_000:
            return f"[错误] 疑似二进制文件，拒绝读取: {p}"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return f"[错误] 读取失败: {e}"
        start = max(1, int(offset))
        end = min(len(lines), start + int(limit) - 1)
        if start > len(lines):
            return f"[错误] offset {start} 超出文件行数 {len(lines)}"
        body = "\n".join(f"{i:>6}| {lines[i-1]}" for i in range(start, end + 1))
        return _clip(f"{p}  ({len(lines)} 行, 显示 {start}-{end})\n{body}", max_out)

    # ---------- write_file ----------
    def write_file(path: str, content: str) -> str:
        p = guard.resolve(path)
        existed = p.exists()
        old = p.read_text(encoding="utf-8", errors="replace") if existed else ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
        action = "覆盖" if existed else "新建"
        diff = "" if not existed else f"（原 {len(old)} 字符 → 现 {len(content)} 字符）"
        return f"已{action}文件 {p} {diff}"

    # ---------- edit_file ----------
    def edit_file(path: str, old_text: str, new_text: str) -> str:
        p = guard.resolve(path)
        if not p.exists():
            return f"[错误] 文件不存在: {p}"
        src = p.read_text(encoding="utf-8", errors="replace")
        count = src.count(old_text)
        if count == 0:
            return (
                f"[错误] 未找到待替换文本，未做任何改动。\n"
                f"请先用 read_file 确认原文（注意缩进与换行完全一致）。\n"
                f"待查找内容前 80 字符: {old_text[:80]!r}"
            )
        if count > 1:
            return (
                f"[错误] 找到 {count} 处匹配，存在歧义，未做任何改动。\n"
                f"请把 old_text 写得更长一些，包含足够的上下文以唯一定位。"
            )
        p.write_text(src.replace(old_text, new_text, 1), encoding="utf-8", newline="")
        return f"已修改 {p}（1 处替换）"

    # ---------- delete_file ----------
    def delete_file(path: str) -> str:
        p = guard.resolve(path)
        if not p.exists():
            return f"[错误] 文件不存在: {p}"
        p.unlink()
        return f"已删除 {p}"

    # ---------- list_dir ----------
    def list_dir(path: str = ".", max_entries: int = 200) -> str:
        p = guard.resolve(path)
        if not p.exists():
            return f"[错误] 路径不存在: {p}"
        if p.is_file():
            return f"{p}  ({p.stat().st_size} 字节)"
        items = []
        dirs, files = [], []
        try:
            for entry in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if entry.name.startswith("."):
                    continue
                if len(dirs) + len(files) >= int(max_entries):
                    break
                (dirs if entry.is_dir() else files).append(entry)
        except OSError as e:
            return f"[错误] 无法列出: {e}"
        for d in dirs:
            items.append(f"  [DIR ] {d.name}/")
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            items.append(f"  [FILE] {f.name}  ({size} B)")
        head = f"{p}\n共 {len(dirs)} 个目录 / {len(files)} 个文件"
        return _clip(head + "\n" + "\n".join(items), max_out)

    return [
        make_tool(
            "read_file",
            "读取文本文件内容，带行号。修改文件前必须先用它确认原文。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径或相对工作区的路径"},
                    "offset": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                    "limit": {"type": "integer", "description": "最多读取多少行，默认 800"},
                },
                "required": ["path"],
            },
            read_file,
            SAFE,
        ),
        make_tool(
            "write_file",
            "新建或整体覆盖一个文件。已有文件优先用 edit_file 做局部替换。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
            write_file,
            WRITE,
        ),
        make_tool(
            "edit_file",
            "对文件做精确的字符串替换。old_text 必须在文件中唯一存在，否则会报错并要求你补充上下文。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "old_text": {"type": "string", "description": "要被替换的原文，需与文件完全一致（含缩进）"},
                    "new_text": {"type": "string", "description": "替换后的新内容"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            edit_file,
            WRITE,
        ),
        make_tool(
            "delete_file",
            "删除一个文件。谨慎使用。",
            {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "要删除的文件路径"}},
                "required": ["path"],
            },
            delete_file,
            WRITE,
        ),
        make_tool(
            "list_dir",
            "列出目录内容，了解项目结构。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认工作区根目录"},
                    "max_entries": {"type": "integer", "description": "最多列出多少条目，默认 200"},
                },
                "required": [],
            },
            list_dir,
            SAFE,
        ),
    ]
