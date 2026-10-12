"""影响路径分析工具（借鉴 TencentDB Agent Memory 的 CodeGraph 思路）。

改代码最怕的不是改不动，而是改完之后别处悄悄坏了。要避免这点，动手前先知道
「这个符号 / 文件到底被谁引用了」。

这里不建真正的代码图谱——那需要解析器和索引服务，与项目「纯本地、零依赖」的
定位冲突。取而代之的是零依赖的静态扫描，直接给出**影响路径**：

    定义处   某个符号被声明/定义的位置（def / class / const / 文件本身）
    引用处   其它文件里出现该符号的行，按文件聚合

输出里「定义处」与「引用处」分开列，模型一眼就能看出改动的波及范围：只动定义、
还是连带把引用方一起更新。它只读、不改，属于 SAFE 类工具，可自动执行。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from .base import SAFE, make_tool
from .search import SKIP_DIRS

# 参与扫描的源码 / 配置扩展名。不扫二进制与图片，避免读出一堆乱码。
_SCAN_EXT = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".rb", ".php", ".swift", ".scala", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".html", ".htm", ".css", ".scss", ".less", ".sql", ".md",
}
_MAX_FILE_BYTES = 1_500_000
_MAX_SCAN_FILES = 4000

# 定义关键字：多数语言声明一个符号都绕不开这些词
_DEF_KEYWORDS = (
    "def|class|function|func|fn|interface|type|struct|enum|impl|trait|"
    "const|let|var|val|final|static|module|namespace|record"
)

_IMPORT_HINT = re.compile(
    r"^\s*(?:from\s|import\s|#include|require\s*\(|use\s|using\s|package\s)", re.I
)


def _word_re(symbol: str) -> re.Pattern[str]:
    """符号的匹配正则：ASCII 标识符按词边界，中文/含特殊字符的按字面量。

    词边界只排掉「前面紧跟单词字符」（避免 MyMemoryStore 命中 MemoryStore），
    但不排掉「点」——`mod.MemoryStore` 这种限定引用同样是真实引用，要算进来。
    """
    esc = re.escape(symbol)
    if re.fullmatch(r"[A-Za-z_][\w.\-]*", symbol):
        return re.compile(rf"(?<![\w]){esc}(?![\w])")
    return re.compile(esc)


def _def_re(symbol: str) -> re.Pattern[str]:
    """判断某行是不是「定义/声明了这个符号」。

    只认真声明（def/class/const/x = ...）；`import MemoryStore` 是**引用**而不是
    定义，单独用 _IMPORT_HINT 标注，否则「定义处」会被导入行淹没。
    """
    esc = re.escape(symbol)
    return re.compile(
        rf"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|"
        rf"abstract\s+|final\s+)*"
        rf"(?:{_DEF_KEYWORDS})\s+{esc}\b"
        rf"|^\s*{esc}\s*[:=]\s*(?:function|\()",
        re.I,
    )


def _glob_match(rel: str, name: str, pat: str) -> bool:
    """文件名过滤。同时兼顾 `*.py`（比对文件名）与 `**/*.py`（比对相对路径）。

    注意 `**/*.py` 用 fnmatch 直接比 `loop.py` 是**不匹配**的（模式里有个 `/`），
    所以额外把前导 `**/` 去掉再比一次文件名——否则顶层文件会被整批漏掉。
    """
    if not pat:
        return True
    if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
        return True
    if pat.startswith("**/"):
        rest = pat[3:]
        return fnmatch.fnmatch(name, rest) or fnmatch.fnmatch(rel, rest)
    return False


def _gather_files(root: Path, file_glob: str) -> list[Path]:
    bases = [root] if root.is_file() else []
    pat = (file_glob or "").strip()
    if root.is_dir():
        for p in root.rglob("*"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if not p.is_file():
                continue
            suffix = p.suffix.lower()
            if suffix not in _SCAN_EXT and suffix:
                continue
            if pat:
                # 含 / 的模式按相对路径匹配（**/*.py），否则只比文件名（*.py）
                if not _glob_match(_rel(p, root), p.name, pat):
                    continue
            try:
                if p.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            bases.append(p)
            if len(bases) > _MAX_SCAN_FILES:
                break
    return bases


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def build_graph_tools(guard, cfg):
    max_out = cfg.max_tool_output_chars

    def impact_of(
        symbol: str,
        path: str = ".",
        file_glob: str = "",
        max_files: int = 30,
        max_refs: int = 150,
    ) -> str:
        """分析改动某个符号/文件会影响哪些地方（定义处 + 引用处）。"""
        name = str(symbol or "").strip()
        if not name:
            return "[错误] symbol 为空，请给出要分析的函数名 / 类名 / 变量名 / 文件名。"
        root = guard.resolve(path)
        if not root.exists():
            return f"[错误] 路径不存在: {root}"

        word = _word_re(name)
        is_def = _def_re(name)
        files = _gather_files(root, file_glob.strip())
        if not files:
            return f"未在 {root} 下找到可扫描的源码/配置文件。"

        defs: list[str] = []
        refs: dict[str, list[str]] = {}
        total_refs = 0
        scanned = 0

        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            rel = _rel(f, root)
            lines = text.splitlines()
            for i, line in enumerate(lines, 1):
                if not word.search(line):
                    continue
                snippet = line.strip()[:200]
                if is_def.search(line):
                    defs.append(f"{rel}:{i}: {snippet}")
                    continue
                # 导入行同样是引用点，但标出来，方便一眼区分「只 import」与「真调用」
                mark = " [导入] " if _IMPORT_HINT.match(line) else " "
                hit = f"{rel}:{i}:{mark}{snippet}"
                bucket = refs.setdefault(rel, [])
                if len(bucket) < 6:                    # 单文件最多给 6 行样例
                    bucket.append(hit)
                total_refs += 1

        # 工作区里是否存在同名文件（改文件名/删文件时的直接影响面）
        same_name: list[str] = []
        if root.is_dir():
            for f in files:
                if f.name == name or f.stem == name:
                    same_name.append(_rel(f, root))

        if not defs and not refs and not same_name:
            return (
                f"影响路径：{name}\n"
                f"已扫描 {scanned} 个文件，**没有任何定义或引用**——"
                f"说明它没有被使用，或其名字拼写不同。改动它是安全的（但也要确认拼写正确）。"
            )

        ranked = sorted(refs.items(), key=lambda kv: -len(kv[1]))
        head = [
            f"影响路径：{name}",
            f"已扫描 {scanned} 个文件；定义 {len(defs)} 处，引用 {total_refs} 处，"
            f"分布在 {len(refs)} 个文件。",
        ]
        if same_name:
            head.append("同名文件：" + "、".join(same_name[:10]))
        body = ["", f"[定义处] ({len(defs)})"] + ([f"  {d}" for d in defs[:20]] or ["  （无）"])
        body.append("")
        body.append(f"[引用处]（按文件聚合，共 {len(refs)} 个文件）")
        shown = 0
        for rel, hits in ranked:
            if shown >= int(max_files):
                body.append(f"  …其余 {len(ranked) - shown} 个文件略（可收紧 path 或 file_glob 再查）")
                break
            n = len(hits)
            more = "…" if n >= 6 else ""
            body.append(f"  {rel}  （{n} 处）")
            for h in hits:
                body.append(f"      {h}")
            shown += 1
            if shown * 6 >= int(max_refs):
                break
        body.append("")
        body.append(
            "提示：改动前请确认每个引用方是否需要同步更新；只改定义处而漏掉引用方，"
            "往往就是回归的来源。"
        )
        return "\n".join(head + body)[:max_out]

    return [
        make_tool(
            "impact_of",
            "影响路径分析：给出某个函数名/类名/变量名/文件名在工作区里的**定义处**与"
            "**被引用的所有位置**（按文件聚合）。改动代码、重命名符号、调整接口前，"
            "先用它看清波及范围，避免改一处坏一片。只读、不改动任何文件。",
            {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "要分析的符号名，如 MemoryStore、build_registry、config.lanrun.yaml",
                    },
                    "path": {"type": "string", "description": "扫描根目录，默认工作区根目录"},
                    "file_glob": {"type": "string", "description": "限定文件，如 **/*.py，留空表示按源码扩展名扫描"},
                    "max_files": {"type": "integer", "description": "最多列出多少个文件，默认 30"},
                    "max_refs": {"type": "integer", "description": "引用处采样上限，默认 150"},
                },
                "required": ["symbol"],
            },
            impact_of,
            SAFE,
        )
    ]


__all__ = ["build_graph_tools"]
