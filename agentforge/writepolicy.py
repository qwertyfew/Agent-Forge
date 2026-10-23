"""写前策略：把「写错了再打回」改成「写之前就知道能不能写」。

AgentForge 原有的内容核查（引用核查、语义闸门）都在 `write_file` 内部，
也就是**模型已经把整篇内容生成出来之后**。被拒的代价是整篇重写——rerun23
里模型被引用核查连拒 4 次，每次都是三千字从头再来；同一轮 7 次宣称已写入、
文件却不存在。dsh 的对应做法是 `fs/write-intent`：写之前先问策略。

这里补三层，全部在内容落盘之前：

1. **证据门槛**（`intent_gate`）：一份引用密集型文档（≥3 处带目录的引用），
   如果本任务一次 `read_file` 都没有，直接拒收并给出「先读这些文件」的清单。
   引用只能来自真读过的代码，不是来自印象——这是 rerun22「路径全对、结论全错」
   的根因。
2. **草稿暂存**（`reject_message`）：任何核查拒绝都先把内容存进 `.drafts/`，
   回传草稿路径 + 逐条定位（草稿第 N 行）。被拒不再等于丢掉重来，改几行即可。
3. **重复拒绝升级**：同一路径第二次被拒，明确禁止重写全文，只允许基于草稿
   做局部替换——把「生成成本」从每轮一整篇压到每轮几行。

PTC 常驻下所有写盘都发生在 `ops_batch` 脚本里，但仍然走 `fs.write_file`，
所以策略天然覆盖两条路径，不需要在脚本层另写一套。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .evidence import citations_in

# 草稿目录（工作区根下，不进产物目录、不参与交付核查）
DRAFT_DIR = ".drafts"

# 判定「引用密集型文档」的门槛：低于这个数按普通文件处理，不启用证据门槛
DENSE_REFS = 3

# 同一路径被拒次数的计数（跨 run 保留：模型在同一个任务里会反复试同一路径）
_rejects: dict[str, int] = {}


def _key(path: str) -> str:
    return str(path or "").replace("\\", "/").strip()


def note_reject(path: str) -> int:
    """记一次拒绝，返回这是第几次。"""
    k = _key(path)
    n = _rejects.get(k, 0) + 1
    if len(_rejects) > 500:            # 长期运行的服务：超量就清空，别当缓存用
        _rejects.clear()
    _rejects[k] = n
    return n


def reject_count(path: str) -> int:
    return _rejects.get(_key(path), 0)


@dataclass
class PrewriteDecision:
    allow: bool
    kind: str = ""                      # "evidence" | ""
    message: str = ""
    required: list[str] = field(default_factory=list)


def _project_refs(content: str) -> list[str]:
    """带目录分隔符的项目内引用（路径部分，去掉 :行号）。"""
    out: list[str] = []
    for tok in citations_in(content or ""):
        core = tok[:-1] if tok.endswith("/") else tok
        m = re.match(r"^(.*?)(?::\d+(?:-\d+)?)?$", core)
        p = (m.group(1) if m else core).strip()
        if ("/" in p or "\\" in p) and p not in out:
            out.append(p)
    return out


def _locate(content: str, token: str, limit: int = 1) -> list[int]:
    """token 在内容里的行号（1 起，最多 limit 个）——用来告诉模型「改哪几行」。"""
    if not token:
        return []
    hits: list[int] = []
    for i, line in enumerate(str(content or "").splitlines(), 1):
        if token in line:
            hits.append(i)
            if len(hits) >= limit:
                break
    return hits


def intent_gate(path: str, content: str, *, cfg, guard, ledger,
                root: str | Path) -> PrewriteDecision:
    """写盘前的证据门槛。只有一种情形会拒：引用密集，却一次都没读过。"""
    if not getattr(cfg, "prewrite_gate", True):
        return PrewriteDecision(True)
    if not content or ledger is None:
        return PrewriteDecision(True)
    refs = _project_refs(content)
    if len(refs) < DENSE_REFS:
        return PrewriteDecision(True)          # 普通文件，不设门槛
    try:
        reads = ledger.read_count()
    except Exception:                          # noqa: BLE001
        return PrewriteDecision(True)
    if reads > 0:
        return PrewriteDecision(True)          # 有过取证，交给常规核查判断质量

    # 需要先读的清单：引用里真实存在于工作区、且本任务没读过的文件
    need: list[str] = []
    base = Path(root)
    for rel in refs:
        try:
            p = guard.resolve(rel)
        except Exception:                      # noqa: BLE001
            p = base / rel
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if ledger.was_read(p):
            continue
        try:
            shown = p.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            shown = rel
        if shown not in need:
            need.append(shown)
        if len(need) >= 6:
            break

    lines = [
        f"[错误] 写前检查未通过，目标文件未写入：{path}",
        f"这份内容引用了 {len(refs)} 处项目内文件，但本任务一次 read_file 都没有——"
        "引用只能从印象里编，写出来必然对不上。",
    ]
    if need:
        lines.append("先读这些文件（一次 read_file 就够，不必整篇读完）：")
        lines += [f"  - {p}" for p in need]
    else:
        lines.append("引用的路径在工作区里也不存在：要么拼错了，要么是凭空写的。请先 ls 确认真实路径。")
    lines.append("读完之后再写；你的分析结论可以直接复用，不需要重新想。")
    return PrewriteDecision(False, "evidence", "\n".join(lines), need)


def stash(path: str, content: str, cfg) -> str:
    """把被拒的内容存成草稿，返回相对工作区的路径（失败返回空串）。"""
    try:
        root = Path(cfg.workspace_root)
        rel = _key(path).lstrip("./") or "draft.md"
        safe = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff/]", "_", rel).replace("/", "__")
        d = root / DRAFT_DIR
        d.mkdir(parents=True, exist_ok=True)
        f = d / (safe or "draft.md")
        f.write_text(str(content or ""), encoding="utf-8", newline="")
        return f"{DRAFT_DIR}/{f.name}"
    except OSError:
        return ""


def reject_message(path: str, content: str, issues, kind: str, cfg) -> str:
    """核查拒绝时统一出口：暂存草稿 + 逐条定位 + 局部修正指引。

    kind: "citation"（引用核查）| "claims"（语义闸门）| "syntax"（语法检查）
    """
    head = {
        "citation": "引用核查未通过",
        "claims": "语义核查未通过",
        "syntax": "语法检查失败",
        "evidence": "证据核查未通过",
    }.get(kind, "核查未通过")
    n = note_reject(path)
    lines = [f"[错误] {head}，目标文件未写入：{path}"]
    for it in issues or []:
        if isinstance(it, (tuple, list)) and len(it) == 2:
            token, detail = str(it[0] or ""), str(it[1] or "")
        else:
            token = getattr(it, "token", "") or ""
            detail = getattr(it, "detail", "") or str(it)
        where = ""
        if token:
            hits = _locate(str(content or ""), token)
            if hits:
                where = f"（草稿第 {hits[0]} 行）"
        lines.append(f"  - {token or '内容'}: {detail}{where}")

    draft = stash(path, content, cfg)
    if draft:
        lines.append(
            f"你的内容已暂存为草稿 {draft}（{len(str(content or ''))} 字符），"
            "**不要整篇重写**。"
        )
        lines.append(
            f"修正方式：edit_file(path='{draft}', old_string=<出问题的原文片段>, "
            f"new_string=<改正后的片段>) 只改上面点到的几行；改完后再调一次 "
            f"write_file，把草稿当前内容原样提交（会重新过核查）。"
        )
    else:
        lines.append("（草稿暂存失败，请直接修正后重试。）")
    if n >= 2:
        lines.append(
            f"这是同一路径第 {n} 次被拒：禁止重写全文，只允许基于草稿做局部替换——"
            "整篇重写只会把已经对的部分也改坏。"
        )
    if kind == "citation" and len(issues or []) >= 3:
        # rerun43 实录：模型一条一条试，每轮烧一整篇生成。批量指引把 N 轮压成 1 轮。
        lines.append(
            "这是同一类问题：先把上面点到的**每个文件**都 read_file 一遍（一次脚本里"
            "连读即可），确认真实行号后一次性改草稿再提交——不要一条一条试探。"
        )
        lines.append(
            "表格类交付物的最快路径（实测可行）：在**同一个 ops_batch 脚本**里先"
            " read_file 全部引用文件，紧接着对每一行连续 fill_section——脚本内读写"
            "不增轮次，被拒也只丢一行。"
        )
    return "\n".join(lines)


def draft_ready(path: str, cfg) -> str:
    """该路径是否已存在草稿（脚本执行前用：优先在草稿上改，别再从头写）。"""
    try:
        root = Path(cfg.workspace_root)
        rel = _key(path).lstrip("./") or "draft.md"
        safe = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff/]", "_", rel).replace("/", "__")
        f = root / DRAFT_DIR / (safe or "draft.md")
        return f"{DRAFT_DIR}/{f.name}" if f.is_file() else ""
    except OSError:
        return ""


def clear_rejects(path: str) -> None:
    """该路径一次成功写盘后清零拒绝计数：ban 针对的是「连续重写」这个行为，
    不是给路径判无期。下次再连续被拒，同样会被拦。"""
    _rejects.pop(_key(path), None)


# 整写 OCC（base_version）被拦的次数。见 fs.write_file：同一路径第 3 次整写
# 要带 base_version；**连拦两次就 fail-open 放行**——闸门只该把「重复生成」
# 换成「改几行」，不该把交付物卡死。这是 doctrine 里「闸门只能证伪，
# 不能误伤交付」的落地。
_occ_blocks: dict[str, int] = {}


def note_occ_block(path: str) -> int:
    """记一次 OCC 拦截，返回这是第几次。"""
    k = _key(path)
    n = _occ_blocks.get(k, 0) + 1
    if len(_occ_blocks) > 500:
        _occ_blocks.clear()
    _occ_blocks[k] = n
    return n


def clear_occ(path: str) -> None:
    _occ_blocks.pop(_key(path), None)


def rewrite_ban(path: str, content: str, cfg) -> str:
    """同路径被拒 ≥2 次后，write_file 的整篇重写禁令——程序层强制，不是叮嘱。

    rerun34 实录：回执末尾的「禁止重写全文」（reject_message 里那句）被模型
    无视，同一路径连撞三次整篇 write_file，每轮烧掉一整篇的生成成本。这里把
    禁令上移到 A 层：被拒 ≥2 次且草稿已暂存时，write_file 只接受「草稿的当前
    内容」——想改，先 edit_file 草稿再原样提交（仍会过常规核查），或直接
    edit_file 目标文件做局部修改。没有草稿（暂存失败）时不拦，退回老路。
    返回 "" 表示放行。
    """
    if reject_count(path) < 2:
        return ""
    draft = draft_ready(path, cfg)
    if not draft:
        return ""
    try:
        draft_text = (Path(cfg.workspace_root) / draft).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if str(content or "").strip() == draft_text.strip():
        return ""          # 走的就是「改草稿 → 原样提交」这条路，放行
    n = reject_count(path)
    return "\n".join([
        f"[错误] 这是同一路径第 {n} 次被拒，write_file 整篇重写已被禁用：{path}",
        f"上次被拒的内容已暂存为草稿 {draft}。只允许两条路：",
        f"  1. edit_file(path='{draft}', old_string=<错的片段>, "
        "new_string=<改正后的片段>) 修好草稿，再把草稿当前内容原样 write_file 提交；",
        "  2. 直接 edit_file 目标文件，只换出问题的几行（已填好的部分不受影响）。",
        "表格类交付物的最快路径：在**同一个 ops_batch 脚本**里先 read_file 全部"
        "引用文件，紧接着逐行 fill_section——被拒只丢一行，脚本内读写不增轮次。",
        "重新生成一整篇不会被接收——已经对的部分会在重写里被改坏。",
    ])
