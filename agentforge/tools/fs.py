from __future__ import annotations

from pathlib import Path
import ast
import json

from .base import SAFE, WRITE, make_tool
def _syntax_check(p: Path) -> str | None:
    try:
        if p.suffix.lower() == ".py":
            ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        elif p.suffix.lower() == ".json":
            json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as e:
        return f"第{e.lineno}行: {e.msg}"
    except json.JSONDecodeError as e:
        return f"第{e.pos}位置: {e.msg}"
    except Exception as e:
        return str(e)
    return None

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
    # 读取规则对齐 WorkBuddy：默认一次最多 2000 行、单行超 2000 字符截断。
    # 真正卡脖子的其实是字符预算（max_tool_output_chars，12000 字符折算约
    # 150-200 行代码）——员工因此拿 offset 一格格续读，读工具调用次数虚高。
    # 动态扩限：默认读取发现文件没显示完时记住该文件，下次对它的默认读取
    # 把字符预算翻倍（上限 4 倍）；显式给 limit 视为模型自己要小份，不动状态。
    # 文件完整读过一遍后状态清除，恢复默认预算——预算只对「确实很大的文件」放宽。
    read_budget: dict[str, int] = {}
    MAX_LINE = 2000

    def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
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
        if start > len(lines):
            return f"[错误] offset {start} 超出文件行数 {len(lines)}"
        end = min(len(lines), start + max(1, int(limit)) - 1)
        shown = [f"{p}  ({len(lines)} 行, 显示 {start}-{end})"]
        for i in range(start, end + 1):
            ln = lines[i - 1]
            if len(ln) > MAX_LINE:
                ln = ln[:MAX_LINE] + f"…[第 {i} 行超 {MAX_LINE} 字符已截断]"
            shown.append(f"{i:>6}| {ln}")
        body = "\n".join(shown)
        budget = read_budget.get(str(p), max_out)
        if len(body) > budget:
            body = body[:budget]
            nxt = min(budget * 2, max_out * 4)
            body += (f"\n...[已截断，文件未显示完。下一次不带 limit 的 read_file "
                     f"会自动把读取预算扩大到 {nxt} 字符；也可用更大的 offset 跳读后半部分]")
            read_budget[str(p)] = nxt
        elif end >= len(lines):
            read_budget.pop(str(p), None)   # 完整读过了，恢复默认预算
        return body

    # ---------- write_file ----------
    def write_file(path: str, content: str) -> str:
        p = guard.resolve(path)
        existed = p.exists()
        old = p.read_text(encoding="utf-8", errors="replace") if existed else ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
        
        if existed and p.suffix.lower() in {".py", ".json"}:
            check_result = _syntax_check(p)
            if check_result:
                p.write_text(old, encoding="utf-8", newline="")
                return f"[错误] 修改已回滚（语法检查失败）: {check_result}\n你的 content 引入了语法错误，请修正后重试。"
        
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
        src = p.read_text(encoding="utf-8", errors="replace")
        p.write_text(src.replace(old_text, new_text, 1), encoding="utf-8", newline="")
        check_result = _syntax_check(p)
        if check_result:
            p.write_text(src, encoding="utf-8", newline="")
            return f"[错误] 修改已回滚（语法检查失败）: {check_result}\n你的 new_text 引入了语法错误，请修正后重试。"
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
                    "limit": {"type": "integer", "description": "最多读取多少行，默认 2000；大文件读不完时第二次会自动扩大预算"},
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
