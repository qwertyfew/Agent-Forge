from __future__ import annotations

"""仓库分析 / 差距清单 / 逆向报告的「引用证据」架构层保证。

为什么下沉到架构层（而非只靠提示词）：
提示词里写了「引用前先核实存在 / 区分文件还是包 / 行号绑定符号」，但本地小模型
经常不照做——上一轮 harness 产出的差距清单里 9 处引用路径根本不存在或把包目录
当文件（如 `learn_claude_code/...` 真实目录是 `learn-claude-code/...`，`agentforge/tools.py`
真实是 `agentforge/tools/` 包）。读者按引用复现不到。

这里把「能由代码确定性判定的事」用代码卡死：
  - 引用路径不存在            -> 错误（架构保证，写入直接退回）
  - 把目录当文件引用          -> 错误（架构保证）
  - 行号超出文件实际行数      -> 错误（架构保证）
  - 引用 external/ 仓库文件但本任务从未 read_file 过 -> 错误（阻断；
    search_code/list_dir 片段不算已读——rerun11/12 实证片段结论产出
    事实性错误「缺乏自动压缩」与假阴性「未发现会话恢复」）
  - 引用其他本任务没读过的路径 -> 警示（台账比对，不阻断）
  - 裸行号缺符号锚点          -> 警示（偏好，不阻断）
  - 报告含数字却只抓过网页    -> 警示（二手来源，不阻断）

只校验「含 / 的引用」且只认两种包裹形态——反引号 `` `path` `` 与圆括号 `(path)`；
裸文件名（如 `loop.py`）与圆括号里的纯文本不进校验，避免误伤代码 / 笔记与普通
行文。两种形态都要，是因为 rerun7 实测：硬校验拦下反引号坏引用后，模型把路径
改写进圆括号（`(s17_goal_loop/code.py)`）绕开了只认反引号的抽取器——校验器必须
与模型的转写赛跑，两种常见引用形态都覆盖。判定与提示词互补：提示词讲「为什么」，
这里是「硬边界」。
"""

from pathlib import Path
import re
from typing import NamedTuple

# 反引号包裹的引用：`` `path` `` / `` `path:line` `` / `` `path:line-line` `` / `` `dir/` ``
_BT_RX = re.compile(r"`([^`\n]{1,220})`")
# 圆括号包裹的引用：(path) / (path:line) / (dir/...)；全角（…）一并收
# （gemma 系模型写中文报告时两种括号都会用，只认半角会整份漏检——rerun9 实测）
_PAREN_RX = re.compile(r"[(（]([^()()\n]{1,220})[)）]")

# 圆括号引用要「像路径」才收：含 /、无空白、非 URL、末段是目录或带扩展名文件
_URL_RX = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_TAIL_FILE_RX = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9]{1,5}$")


def _paren_like(tok: str) -> bool:
    """圆括号内容的准入判定：宁可放过（漏检）不可错抓（误伤行文）。"""
    if "/" not in tok and "\\" not in tok:
        return False
    if _URL_RX.match(tok) or re.search(r"\s", tok):
        return False
    if tok.endswith("/") or tok.endswith(("...", "…")):
        return True                      # 目录引用（含 dir/... 省略写法）
    tail = tok.rstrip("/").rsplit("/", 1)[-1]
    # 允许行号尾巴：code.py:96 / code.py:96-127（rerun9 实测：交付物里的引用
    # 几乎全带行号，不剥尾巴会被形状校验整份拒收，审计 checked=0 等于没查）
    tail = re.sub(r"[:：][0-9][0-9\-]*$", "", tail)
    return bool(_TAIL_FILE_RX.match(tail))


class EvidenceLedger:
    """模型实际核实过的文件 / 目录的唯一事实源（架构层记录，不靠提示词自觉）。

    每当 read_file / search_code / list_dir / fetch_* 成功，就把真实解析后的路径
    记进来。写报告做引用校验时，它用于判断「这个路径模型到底有没有真正读过」。
    """

    def __init__(self) -> None:
        # 解析后的绝对路径（小写）-> {"is_dir": bool, "lines": int|None, "read": bool}
        self._files: dict[str, dict] = {}
        self._web: set[str] = set()  # 抓过的网页 URL（二手来源标记）

    @staticmethod
    def _key(p: Path) -> str:
        try:
            return str(p.resolve()).lower()
        except OSError:
            return str(p).lower()

    def mark_file(self, p: Path, is_dir: bool = False, lines: int | None = None,
                  read: bool = False) -> None:
        self._files[self._key(p)] = {"is_dir": is_dir, "lines": lines, "read": read}

    def mark_dir(self, p: Path) -> None:
        self.mark_file(p, is_dir=True)

    def mark_web(self, url: str) -> None:
        self._web.add(str(url).strip().lower())

    def knows_file(self, p: Path) -> bool:
        rec = self._files.get(self._key(p))
        return bool(rec) and not rec["is_dir"]

    def was_read(self, p: Path) -> bool:
        """该文件本任务是否被 read_file 真读过（search_code/list_dir 命中不算）。

        rerun12 实证：search_code 返回的 300 字符片段会被模型当成「已读」，
        片段结论直接进交付物，产出事实性错误（「AgentForge 缺乏自动压缩」，
        实际 context.py 有完整 compact）。所以「检索命中过」与「读过」必须分开记。
        """
        rec = self._files.get(self._key(p))
        return bool(rec) and not rec["is_dir"] and bool(rec.get("read"))

    def knows_dir(self, p: Path) -> bool:
        rec = self._files.get(self._key(p))
        return bool(rec) and rec["is_dir"]

    def line_count(self, p: Path) -> int | None:
        return self._files.get(self._key(p), {}).get("lines")

    def has_web(self) -> bool:
        return bool(self._web)


class CitationIssue(NamedTuple):
    level: str      # "error" | "warn"
    kind: str       # missing | is_dir | line_range | unverified | bare_line | web_number
    token: str
    detail: str


class CitationAudit(NamedTuple):
    issues: list[CitationIssue]
    checked: int    # 实际进入校验的引用数（含 / 且落在工作区内的）


def _extract_citations(text: str) -> list[str]:
    """抽出引用（去重保序）：反引号包裹一律收；圆括号包裹须「像路径」才收
    （见 _paren_like）。这是精度与召回的平衡点：报告里的引用两种形态都有，
    而代码 / 笔记里的裸文件名、IP:端口、URL 不会进这里。"""
    seen: set[str] = set()
    out: list[str] = []

    def _add(tok: str) -> None:
        # dir/... 省略写法归一成目录引用（尾斜杠，且不产生双斜杠）
        if tok.endswith(("...", "…")):
            tok = tok.rstrip(".…").rstrip("/") + "/"
        t = tok.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    for m in _BT_RX.findall(text):
        _add(m)
    for m in _PAREN_RX.findall(text):
        if _paren_like(m):
            _add(m)
    return out


# 报告里出现的具体数字（疑似指标类断言）：用来配合「只抓过网页」做二手来源警示
_NUM_RX = re.compile(
    r"\b\d[\d.,]*\s?(?:%|stars?|forks?|times|倍|个|条|万|准确率|pass@|acc|准确率)\b",
    re.IGNORECASE,
)


def audit_citations(
    content: str,
    guard,
    workspace_root: Path,
    ledger: EvidenceLedger | None = None,
) -> CitationAudit:
    """对一份待写入的报告做引用校验。

    返回 (issues, checked)。架构层只卡 checked>=2 且存在 error 的情况（即这明显是
    一份引用密集的报告），单个零散引用不会阻断普通文件写入。
    """
    issues: list[CitationIssue] = []
    checked = 0
    root = Path(workspace_root).resolve()
    candidates = _extract_citations(content)

    for token in candidates:
        is_dir_ref = token.endswith("/")
        core = token[:-1] if is_dir_ref else token
        # 拆分可选的行号区间：path:12 或 path:12-34
        m = re.match(r"^(.*?)(?::(\d+)(?:-(\d+))?)?$", core)
        path_part = m.group(1) if m else core
        line_a = int(m.group(2)) if (m and m.group(2)) else None

        # 只校验含目录分隔符的引用：裸文件名（如 `loop.py`）太含糊，跳过
        if "/" not in path_part and "\\" not in path_part:
            continue
        try:
            p = guard.resolve(path_part)
        except Exception:
            continue
        # 只校验落在工作区内的引用（外部路径不归本仓库证据管）
        try:
            p.relative_to(root)
        except ValueError:
            continue
        checked += 1

        if not p.exists():
            hint = "（目录名大小写 / 连字符下划线、文件名是否拼错？）"
            # rerun7 实测高发：引用 external/ 下仓库文件时漏写 external/<repo>/ 前缀。
            # 自动探测：同相对路径在 external/<某仓库>/ 下存在时，给出可复现的修正。
            try:
                # 形态一：首段就是仓库名（learn-claude-code/s01/... → external/learn-claude-code/s01/...）
                # rerun9 交付物即此形态（4 处），旧探测只覆盖「漏写整段仓库名」的形态。
                if (root / "external" / path_part).exists():
                    hint = (f"（实际在 external/{path_part} —— "
                            f"引用 external/ 下的仓库文件必须带 external/ 前缀）")
                else:
                    for repo in (root / "external").iterdir() if (root / "external").is_dir() else ():
                        if (repo / path_part).exists():
                            hint = (f"（实际在 external/{repo.name}/{path_part} —— "
                                    f"引用 external/ 下的仓库文件必须带 external/<仓库名>/ 前缀）")
                            break
            except OSError:
                pass
            issues.append(CitationIssue(
                "error", "missing", token, f"引用路径不存在: {p}{hint}"))
            continue

        if is_dir_ref:
            # 显式目录引用（带尾斜杠）：存在即合法
            continue

        if p.is_dir():
            issues.append(CitationIssue(
                "error", "is_dir", token,
                f"引用的是目录不是文件: {p}（包目录请落到具体文件，如 {p}/xxx.py）"))
            continue

        # 行号越界校验（确定性）
        if line_a is not None:
            n = ledger.line_count(p) if ledger else None
            if n is None:
                try:
                    n = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
                except OSError:
                    n = None
            if n is not None and line_a > n:
                issues.append(CitationIssue(
                    "error", "line_range", token,
                    f"行号 {line_a} 超出文件实际范围（共 {n} 行）: {p}"))

        # 以下为软警示（不阻断写入）；external/ 仓库文件例外——升为硬错误
        if ledger is not None:
            # external/ 下的仓库文件是逆向分析的任务对象，结论必须来自真实读过
            # 的内容。检索命中（片段）/ 目录列举不算已读——rerun11/12 实证片段
            # 结论会产出事实性错误与假阴性，这里从「警示」升级为「阻断」。
            in_external = p.relative_to(root).as_posix().lower().startswith("external/")
            if in_external and not ledger.was_read(p):
                issues.append(CitationIssue(
                    "error", "not_read", token,
                    f"引用了本任务从未 read_file 过的文件: {p}"
                    f"（search_code / list_dir 片段不算已读——请先 read_file 该文件核实再写结论）"))
            elif not ledger.knows_file(p):
                issues.append(CitationIssue(
                    "warn", "unverified", token,
                    f"该路径本任务未实际读取 / 检索过，可能是凭印象写出: {p}"))
            elif line_a is not None:
                issues.append(CitationIssue(
                    "warn", "bare_line", token,
                    f"建议绑定符号锚点（如 `class X` 内 / `def run()` 处）而非裸行号 {token}"))

    # 规则 4 架构层协助：报告含具体数字，但本任务只抓过网页（二手来源）
    if ledger is not None and ledger.has_web() and _NUM_RX.search(content):
        issues.append(CitationIssue(
            "warn", "web_number", "",
            "报告含具体数字，但本任务仅抓取过网页（二手来源）；"
            "请确保数字来自仓库一手证据（代码 / README / 测试 / 官方 API），否则标注「（未核实）」"))

    return CitationAudit(issues, checked)
