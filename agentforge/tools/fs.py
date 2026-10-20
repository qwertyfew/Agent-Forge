from __future__ import annotations

from pathlib import Path
import ast
import json

from .base import SAFE, WRITE, make_tool
from ..evidence import audit_claims, audit_citations
from .. import writepolicy
from ..record import RECORDER_CTX
from .. import skeleton
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

    # ---------- 内容核查（写盘前统一入口） ----------
    def _content_gate(path: str, content: str):
        """写盘前的完整核查链：证据门槛 → 引用核查 → 语义闸门。

        返回 (拒绝文案 | None, cit_audit)。write_file / write_skeleton /
        fill_section 共用这一条链：分段填充时只核查**本节内容**，被拒的代价
        也就只有一节，不会牵连已经填好的部分。
        """
        ledger = getattr(guard, "ledger", None)
        cit_audit = None
        # 写前策略（writepolicy.py）：先看证据，再谈内容。
        # 引用密集型文档在一次 read_file 都没有时直接拒收——引用只能来自真读过的
        # 代码。rerun22 那份差距清单「路径全对、结论全错」，根因就是凭印象写。
        if content and getattr(cfg, "prewrite_gate", True):
            dec = writepolicy.intent_gate(path, content, cfg=cfg, guard=guard,
                                          ledger=ledger, root=cfg.workspace_root)
            if not dec.allow:
                return dec.message, None
        # 不再用「含反引号」当门槛：rerun25 把引用改写成圆括号 (lc_loop/code.py:7-13)，
        # 整份文件一个反引号都没有，核查直接被跳过，9 条虚构路径全部落盘。
        # 审计内部按 _extract_citations 提取（反引号 + 中英文圆括号），且自身
        # 用 checked>=2 做「引用密集文档」门槛，这里不该再加形状限制。
        if content:
            try:
                cit_audit = audit_citations(content, guard, cfg.workspace_root, ledger)
            except Exception:
                cit_audit = None
            if cit_audit is not None:
                errors = [i for i in cit_audit.issues if i.level == "error"]
                if errors and cit_audit.checked >= 2:
                    return writepolicy.reject_message(path, content, errors, "citation", cfg), None
        # 语义层核查：路径对、结论错也要拦。rerun22 的差距清单写「AgentForge 缺乏
        # 上下文压缩 / 权限审批 / 任务清单」，路径全是真的，结论却与代码相反——
        # 照它改会重复造轮子。这里只判「否定断言 vs 真实定义」这一种确定矛盾，
        # 命中就把真实 path:line 一起给回去（一次改对，不用反复试探）。
        if content and getattr(cfg, "claims_gate", True):
            try:
                claim_issues = audit_claims(content, cfg.workspace_root)
            except Exception:
                claim_issues = []
            if claim_issues:
                return writepolicy.reject_message(path, content, claim_issues, "claims", cfg), None
        return None, cit_audit

    def _write_target(path: str):
        """写盘落点：产出目录重定向（resolve_for_write）+ 是否被重定向。"""
        rfw = getattr(guard, "resolve_for_write", None)
        p = rfw(path) if rfw else guard.resolve(path)
        return p, p != guard.resolve(path)

    def _snapshot(path: str, content: str, old: str) -> str:
        """落盘后留一份全文版本，返回写进回执的说明（无记录器时返回空串）。

        这是「用文件层级保证」的那一半：每次成功写盘都是一个**新文件**，
        所以整节替换 / 整篇重写都不再是不可逆的破坏——上一版永远在
        history/<task>/versions/ 里。模型被覆盖了内容也不需要重生成，直接
        从版本恢复；人复盘时也能逐版对比。
        """
        rec = RECORDER_CTX.get()
        if rec is None:
            return ""
        try:
            rel, add, rem = rec.version(path, content, old=old)
        except Exception:  # noqa: BLE001
            return ""
        if not rel:
            return ""
        stat = f"+{add}/-{rem} 行" if (add or rem) else "内容未变"
        return f"\n  版本：{rel}（{stat}）"

    def _record_citations(path: str, audit, version: str) -> None:
        """把引用核查结论也落成文件（citations.jsonl），供复盘与自动评估。"""
        rec = RECORDER_CTX.get()
        if rec is None or audit is None:
            return
        try:
            stats = getattr(audit, "stats", None) or {}
            rec.citations(path, total=int(stats.get("total", 0)),
                          with_line=int(stats.get("with_line", 0)),
                          inside=int(stats.get("inside", 0)),
                          outside=int(stats.get("outside", 0)), version=version)
        except Exception:  # noqa: BLE001
            pass

    # ---------- write_file ----------
    def write_file(path: str, content: str) -> str:
        # 产出目录的架构级保证：任务设有产出目录时，相对路径强制落进去
        # （resolve_for_write；任务书点名的路径与绝对路径走例外通道）。
        p, redirected = _write_target(path)
        existed = p.exists()
        old = p.read_text(encoding="utf-8", errors="replace") if existed else ""
        err, cit_audit = _content_gate(path, content)
        if err:
            return err

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
        
        if existed and p.suffix.lower() in {".py", ".json"}:
            check_result = _syntax_check(p)
            if check_result:
                p.write_text(old, encoding="utf-8", newline="")
                return writepolicy.reject_message(
                    path, content,
                    [("", f"语法错误：{check_result}（文件已回滚到原内容）")],
                    "syntax", cfg)
        # 整篇写出即视为「不再是骨架」：旧骨架记录若留着，收尾核查会拿
        # 建档时的正文去比对，把刚写完的整篇文档判成「还没填」。
        skeleton.drop_state(cfg.workspace_root, p)
        
        action = "覆盖" if existed else "新建"
        diff = "" if not existed else f"（原 {len(old)} 字符 → 现 {len(content)} 字符）"
        note = "（相对路径已按产出规则自动写入产出目录）" if redirected else ""
        ver = _snapshot(path, content, old)
        note += ver
        if cit_audit is not None:
            _record_citations(path, cit_audit, ver.split("版本：")[-1].split("（")[0] if ver else "")
        # 软警示（不阻断）：把引用核查的 warning 回传给模型，让它有机会修正
        if cit_audit is not None:
            warns = [i for i in cit_audit.issues if i.level == "warn"]
            if warns:
                note += "\n[引用核查提醒] " + "；".join(f"{i.token or '报告'}: {i.detail}" for i in warns)
        return f"已{action}文件 {p} {diff}{note}"

    # ---------- write_skeleton / fill_section（骨架先行） ----------
    def write_skeleton(path: str, outline: str) -> str:
        """只写结构：`## 小节标题` + 一句话意图，几百字符。

        骨架几乎不可能被内容核查拒（没有事实断言可错），所以「先落一个能通过
        核查的结构」几乎零成本；正文随后一节一节填，单次失败只丢一节。
        """
        bad = skeleton.outline_error(outline)
        if bad:
            return bad
        err, _ = _content_gate(path, outline)
        if err:
            return err
        p, redirected = _write_target(path)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(outline, encoding="utf-8", newline="")
        skeleton.record_skeleton(cfg.workspace_root, p, outline)
        ver = _snapshot(path, outline, "")
        st = skeleton.status(outline)
        names = "、".join(s.heading for s in skeleton.body_sections(outline)[:12])
        note = "（相对路径已按产出规则自动写入产出目录）" if redirected else ""
        return (
            f"已{'覆盖' if existed else '新建'}骨架 {p}（{st['total']} 节 / "
            f"{len(outline)} 字符）{note}{ver}\n"
            f"小节：{names}\n"
            "下一步：用 fill_section(path, heading, content) 一次填一节，"
            "长章节第二次用 mode='append' 续写同一节。\n"
            f"注意：骨架本身不算交付——{st['total']} 个小节全部填上正文之前，"
            "收尾核查会判它没写（只看正文有没有真填，不看文件是否存在）。"
        )

    def fill_section(path: str, heading: str, content: str = "",
                     mode: str = "replace") -> str:
        """把一段正文填进某一节。核查只看这一节，被拒不牵连已填好的小节。"""
        p, _ = _write_target(path)
        if not p.exists():
            return (f"[错误] 文件不存在：{p}\n"
                    "先用 write_skeleton 写骨架（只放小节标题），再逐节 fill_section。")
        text = p.read_text(encoding="utf-8", errors="replace")
        new_text, err = skeleton.fill(text, heading, content, mode)
        if err:
            return err
        # 核查目标就是本节内容：这正是「骨架先行」的收益——单次失败的成本
        # 等于一节的生成，而不是整篇。
        gate_err, _ = _content_gate(path, content)
        if gate_err:
            return gate_err + "\n（本次只丢了这一节；已填好的小节原样保留在文件里。）"
        p.write_text(new_text, encoding="utf-8", newline="")
        ver = _snapshot(path, new_text, text)
        if p.suffix.lower() in {".py", ".json"}:
            check = _syntax_check(p)
            if check:
                p.write_text(text, encoding="utf-8", newline="")
                return writepolicy.reject_message(
                    path, content,
                    [("", f"语法错误：{check}（文件已回滚到本次填充前）")],
                    "syntax", cfg)
        pr = skeleton.progress(cfg.workspace_root, p)
        msg = (f"已填充小节「{heading}」（{len(content)} 字符，{mode}）→ {p}{ver}\n"
               + skeleton.progress_text(cfg.workspace_root, p))
        # rerun28 实测误用：模型把整张表格当成一节，逐行 fill_section(默认 replace)，
        # 十一次调用各自「成功」，前面填的行全被后一次替换掉——文件只剩最后一行，
        # 模型只好整篇 write_file 重写，骨架先行的收益归零。
        # 回执必须把「整节替换」这件事说清楚，并在文件没变长时明确点出来。
        if str(mode or "").strip().lower() not in ("append", "a", "add"):
            if len(new_text) <= len(text):
                msg += (f"\n⚠ 这次是整节替换，文件没有变长（{len(text)} → {len(new_text)} 字符）。"
                        f"同一节第二次 replace 会把上一次填的内容丢掉——表格行、分段续写"
                        f"请改用 mode=\"append\"，只补局部用 edit_file。")
            else:
                msg += f"\n（整节替换：文件 {len(text)} → {len(new_text)} 字符）"
        if pr["pending"]:
            msg += "\n下一步：继续 fill_section 填：" + "、".join(str(x) for x in pr["pending"][:6])
        else:
            msg += "\n所有小节已填完，可以给出最终答复（收尾核查会按磁盘内容复核）。"
        return msg

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
        ver = _snapshot(path, src.replace(old_text, new_text, 1), src)
        return f"已修改 {p}（1 处替换）{ver}"

    # ---------- delete_file ----------
    def delete_file(path: str) -> str:
        p = guard.resolve(path)
        if not p.exists():
            return f"[错误] 文件不存在: {p}"
        p.unlink()
        skeleton.drop_state(cfg.workspace_root, p)
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
            "write_skeleton",
            "先写文档骨架：只放 `## 小节标题` 和一句话意图（几百字符），"
            "再用 fill_section 逐节填正文。超过两节的文档一律先骨架——"
            "骨架几乎不会被内容核查拒，单次生成失败只损失一节。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径（后续 fill_section 用同一路径）"},
                    "outline": {"type": "string",
                                "description": "骨架正文：标题 + 2 个以上 `## 小节标题`，每节可带一句意图（≤240 字符）"},
                },
                "required": ["path", "outline"],
            },
            write_skeleton,
            WRITE,
        ),
        make_tool(
            "fill_section",
            "把一段正文填进骨架里的某一个 `## 小节`。内容核查只针对这一节，"
            "被拒不牵连其他小节；mode='append' 用于把长章节分几次续写。"
            "注意 replace 是**整节替换**：同一节第二次 replace 会丢掉上一次填的内容，"
            "逐行/逐段追加同一个节必须用 mode='append'。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "骨架文件路径"},
                    "heading": {"type": "string", "description": "小节标题（不含 #，如「3. 结论」）"},
                    "content": {"type": "string", "description": "这一节的完整正文（Markdown）"},
                    "mode": {"type": "string", "enum": ["replace", "append"],
                             "description": "replace=替换该节正文（默认）；append=接在已有正文之后续写"},
                },
                "required": ["path", "heading", "content"],
            },
            fill_section,
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
