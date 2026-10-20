"""骨架先行：先写结构（少量 token），核查通过后再分段填充。

问题在成本结构：`write_file` 是「一次生成一整篇，然后才核查」。一篇三千字的
报告被引用核查打回，损失的是三千字的生成（rerun23 连拒 4 次，每次都从头再来）；
更糟的是模型在被拒后倾向于**整篇重写**，把已经写对的部分也改坏。

这里的做法是把单次生成的失败成本压到「一节」：

1. `write_skeleton`：只写标题结构（每个小节一行意图），几百字符就够。骨架几乎
   不可能被内容核查拒——它没有事实断言可错。骨架先落盘，交付物路径立刻存在。
2. `fill_section`：一次只填一节。核查只针对**这一节的内容**，被拒也只丢这一节
   的生成，已填好的小节原地保留；支持 `mode="append"` 把一节分几次写（长章节
   不必一口气生成）。
3. 未填完的骨架**不算交付**：`unfinished()` 是纯内容判定（有标题、正文为空或
   只有占位符），契约闸门、产物核查、未交付硬闸都认它。否则写个骨架就能骗过
   「文件存在」的存在性核查——那是把闸门自己拆了。

判定刻意不依赖任何持久状态：只看文件本身。这样跨进程、跨闸门、重启后都成立，
也不会因为状态文件丢失而把半成品当成品。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

# Markdown 标题。代码块里的 `# 注释` 不算标题（由扫描器跳过围栏块保证）。
HEADING_RX = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")

# 占位符：写完骨架后模型常留这些东西，它们不算「已填」。
PLACEHOLDER_RX = re.compile(
    r"^(?:[-*_]+|\.{2,}|…+|todo|tbd|fixme|待补\w*|待填\w*|待完善|待定|待写|"
    r"此处\s*略|同上|（?略）?)$",
    re.IGNORECASE,
)

# 骨架体积上限：超过这个长度说明它已经不是骨架，而是正文伪装成骨架
SKELETON_MAX_CHARS = 2400
# 骨架里单个小节的正文上限（标题下一行的意图说明，一句话就够）
SECTION_HINT_MAX = 240
# 全文正文总字符低于此值，只要还有空节就判「骨架未填」
THIN_BODY = 200
# 正文足够长时，空节要达到这个数量才判未填（避免误伤「某节确实为空」的正常文档）
PENDING_MIN = 2

FENCE_RX = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


@dataclass
class Section:
    heading: str          # 标题文本（不含 #）
    level: int            # 1-6
    head_line: int        # 标题所在行的下标（0 基）
    body_start: int
    body_end: int         # 不含（下一个同级或更高级标题，或文件末尾）

    def body(self, lines: list[str]) -> str:
        return "\n".join(lines[self.body_start:self.body_end])


def _clean(text: str) -> str:
    """去掉空白与占位符后剩下的正文——判断「这一节到底写了东西没有」。"""
    out: list[str] = []
    for ln in str(text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if PLACEHOLDER_RX.match(s):
            continue
        s = re.sub(r"^[-*+][ \t]+", "", s)       # 列表符号不构成内容
        s = re.sub(r"^#+[ \t]*", "", s)          # 子标题本身不算正文
        if s.strip():
            out.append(s)
    return "".join(out)


def _lines(text: str) -> list[str]:
    return str(text or "").splitlines()


def sections(text: str) -> list[Section]:
    """按标题切分（跳过围栏代码块里的伪标题），返回带层级的小节列表。"""
    lines = _lines(text)
    heads: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i, ln in enumerate(lines):
        m = FENCE_RX.match(ln)
        if m:
            mark = m.group(1)
            if fence is None:
                fence = mark[0] * 3
            elif mark.startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        h = HEADING_RX.match(ln)
        if h:
            heads.append((i, len(h.group(1)), h.group(2).strip()))
    out: list[Section] = []
    for idx, (i, level, title) in enumerate(heads):
        end = len(lines)
        for j, lv, _ in heads[idx + 1:]:
            if lv <= level:
                end = j
                break
        out.append(Section(title, level, i, i + 1, end))
    return out


def body_sections(text: str) -> list[Section]:
    """真正需要「填」的小节：去掉文档最外层那个标题。

    `# 差距清单` 这种一级标题是文档名，它的「正文」覆盖全文——拿它当待填小节，
    填完任何一节它都算变过、进度还算错。只在「首个标题是 1 级、后面全是更深
    级别」时去掉它；没有这种结构就原样返回。
    """
    secs = sections(text)
    if len(secs) >= 2 and secs[0].level == 1 and all(s.level > 1 for s in secs[1:]):
        return secs[1:]
    return secs


def pending(text: str) -> list[str]:
    """还没有正文的小节标题（顺序与文档一致）。"""
    lines = _lines(text)
    return [s.heading for s in body_sections(text) if not _clean(s.body(lines))]


def unfinished(text: str) -> bool:
    """这份内容是不是「只搭了骨架、正文没填」。

    只在两种情形判是：①有标题且正文总量极薄（典型的纯骨架）；②正文不算少，
    但空着的小节有两个以上（分段填充做到一半就收工）。单个空小节不判——正常
    文档里确实会有略过的小节。
    """
    secs = body_sections(text)
    if not secs:
        return False
    lines = _lines(text)
    empty = [s.heading for s in secs if not _clean(s.body(lines))]
    if not empty:
        return False
    total = sum(len(_clean(s.body(lines))) for s in secs)
    if total < THIN_BODY:
        return True
    return len(empty) >= PENDING_MIN


def unfinished_file(root: str | Path, rel: str) -> bool:
    """交付物路径上的「骨架未填完」判定。

    有骨架记录时用记录比对（精确）：骨架里每一节的正文若与建档时一字不差，
    说明这节还没填——不管它是空的、还是写着「TODO」「这里写压缩现状」这类
    一行意图。没有记录时退回通用判定（真正空的节才算），保证不误伤普通文档。
    """
    text = _read_text(root, rel)
    if text is None:
        return False
    st = load_state(root, rel)
    if st:
        return bool(unfilled_against(text, st))
    return unfinished(text)


def _state_total(st: dict) -> int:
    """骨架记录里的待填位数：小节数 + 表格行数（任务书骨架可能是表格）。"""
    return len(st.get("headings") or []) + len(st.get("rows") or [])


def progress(root: str | Path, rel: str) -> dict:
    """状态感知的进度：{total, filled, pending, unfinished}。"""
    text = _read_text(root, rel) or ""
    st = load_state(root, rel)
    if st:
        pend = unfilled_against(text, st)
        total = _state_total(st)
        return {"total": total, "filled": total - len(pend),
                "pending": pend, "unfinished": bool(pend)}
    return status(text)


def progress_text(root: str | Path, rel: str) -> str:
    st = progress(root, rel)
    if not st["total"]:
        return "该文件没有记录到小节结构"
    head = f"骨架进度 {st['filled']}/{st['total']} 节"
    if st["pending"]:
        return head + "；还没填：" + "、".join(st["pending"][:8])
    return head + "，全部小节已填完"


# ---------------------------------------------------------------------------
# 骨架记录：只有「模型明确走过 write_skeleton」的文件才建档
# ---------------------------------------------------------------------------
# 为什么需要这份记录：骨架里每节允许写一行意图（「这里写压缩链路现状」），
# 那一行是正文形状的东西——纯内容判定会把它当成「已经填了」。记录下建档时的
# 每节正文，之后比对「正文有没有变过」就能精确判断填没填，不必猜长度阈值
# （阈值一定会误伤短而完整的小节）。
#
# 放在 <工作区>/.agentforge/skeletons/ 而不是 cfg.state_dir：收尾核查
# （verify.find_artifact_issues）只拿得到工作区根目录，两边必须看同一份记录。

STATE_SUBDIR = Path(".agentforge") / "skeletons"



def status(text: str) -> dict:
    secs = body_sections(text)
    lines = _lines(text)
    empty = [s.heading for s in secs if not _clean(s.body(lines))]
    return {"total": len(secs), "filled": len(secs) - len(empty),
            "pending": empty, "unfinished": unfinished(text)}


def status_text(text: str) -> str:
    st = status(text)
    if not st["total"]:
        return "该文件没有小节标题，无法分段填充"
    head = f"骨架进度 {st['filled']}/{st['total']} 节"
    if st["pending"]:
        return head + "；还没填：" + "、".join(st["pending"][:8])
    return head + "，全部小节已填完"


def outline_error(outline: str) -> str | None:
    """骨架合法性检查。返回错误文案（None 表示通过）。"""
    text = str(outline or "")
    if not text.strip():
        return "[错误] outline 为空：骨架至少要有两个 `## 小节标题`。"
    secs = body_sections(text)
    if len(secs) < 2:
        return ("[错误] 骨架至少需要 2 个 `## 小节标题`（当前识别到 "
                f"{len(secs)} 个，代码块里的 # 不算；`# 文档名` 这类一级标题不算小节）。"
                "短文档（单节）请直接用 write_file。")
    if len(text) > SKELETON_MAX_CHARS:
        return (f"[错误] 骨架 {len(text)} 字符，超过上限 {SKELETON_MAX_CHARS}："
                "骨架只放结构，正文交给 fill_section 分段写。")
    lines = _lines(text)
    fat = [(s.heading, len(_clean(s.body(lines)))) for s in secs
           if len(_clean(s.body(lines))) > SECTION_HINT_MAX]
    if fat:
        top = "、".join(f"{h}（{n} 字符）" for h, n in fat[:4])
        return (f"[错误] 这些小节的正文太长，不是骨架：{top}。"
                f"每节只写一句意图（≤{SECTION_HINT_MAX} 字符），正文用 fill_section 填。")
    return None


def _norm(s: str) -> str:
    return re.sub(r"[\s`*_：:，,。.、\-—()（）]+", "", str(s or "")).lower()


def find_section(text: str, heading: str) -> tuple[Section | None, list[str]]:
    """按标题定位小节：先精确、再归一化（忽略空格与标点）。

    返回 (命中, 可用标题列表)。与多个小节归一化后相同则视为歧义，不猜。
    """
    secs = sections(text)
    names = [s.heading for s in secs]
    want = str(heading or "").strip()
    want = re.sub(r"^#+[ \t]*", "", want).strip().rstrip("#").strip()
    for s in secs:
        if s.heading == want:
            return s, names
    key = _norm(want)
    if key:
        hits = [s for s in secs if _norm(s.heading) == key]
        if len(hits) == 1:
            return hits[0], names
        if len(hits) > 1:
            return None, names
        # 退一步：包含关系（模型常把「## 3. 结论」写成「结论」）
        hits = [s for s in secs if key in _norm(s.heading)]
        if len(hits) == 1:
            return hits[0], names
    return None, names


def fill(text: str, heading: str, content: str, mode: str = "replace"
         ) -> tuple[str, str | None]:
    """把 content 填进 heading 这一节。返回 (新全文, 错误文案)。

    mode: replace（默认，替换该节正文）| append（接在该节已有正文之后——
    长章节可以分几次写，每次只生成一段）。
    """
    body_in = str(content or "").strip("\n")
    if not body_in.strip():
        return text, "[错误] content 为空，没有可填入的内容。"
    sec, names = find_section(text, heading)
    if sec is None:
        avail = "、".join(names[:10]) or "（无）"
        return text, (f"[错误] 未找到小节「{heading}」，文件未改动。\n"
                      f"可用小节：{avail}\n请用其中一个标题重新调用 fill_section。")
    lines = _lines(text)
    old_body = sec.body(lines)
    append = str(mode or "").strip().lower() in ("append", "a", "add")
    keep: list[str] = []
    if append and _clean(old_body):
        keep = [ln for ln in old_body.splitlines() if not PLACEHOLDER_RX.match(ln.strip())]
        while keep and not keep[-1].strip():
            keep.pop()
    new_body = keep + ([""] if keep else []) + body_in.splitlines()
    tail = lines[sec.body_end:]
    while tail and not tail[0].strip():
        tail = tail[1:]
    out = lines[:sec.body_start] + [""] + new_body
    if tail:
        out += [""] + tail
    joined = "\n".join(out)
    if str(text or "").endswith("\n"):
        joined += "\n"
    return joined, None


# ---------------------------------------------------------------------------
# 状态读写（全部旁路：任何一步失败都退化成「没有记录」，不影响写盘）
# ---------------------------------------------------------------------------

def _key(root: str | Path, path: str) -> str:
    p = Path(str(path))
    if not p.is_absolute():
        p = Path(root) / str(path)
    try:
        return os.path.normcase(str(p.resolve()))
    except OSError:
        return os.path.normcase(str(p))


def state_path(root: str | Path, path: str) -> Path:
    h = hashlib.sha1(_key(root, path).encode("utf-8", "ignore")).hexdigest()[:16]
    return Path(root) / STATE_SUBDIR / f"{h}.json"


def _read_text(root: str | Path, path: str) -> str | None:
    try:
        p = Path(str(path))
        if not p.is_absolute():
            p = Path(root) / str(path)
        if not p.is_file() or p.stat().st_size > 2_000_000:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def load_state(root: str | Path, path: str) -> dict | None:
    try:
        f = state_path(root, path)
        if not f.is_file():
            return None
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("headings") is not None else None
    except (OSError, ValueError):
        return None


def record_skeleton(root: str | Path, path: str, text: str) -> bool:
    """给「模型刚写完的骨架」建档：记下每节标题与当时的正文。"""
    try:
        lines = _lines(text)
        secs = body_sections(text)
        if not secs:
            return False
        f = state_path(root, path)
        f.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "path": _key(root, path),
            "ts": round(time.time(), 3),
            "headings": [s.heading for s in secs],
            "bodies": {s.heading: _clean(s.body(lines)) for s in secs},
        }
        # 表格骨架把表头也存进契约：rerun42 实证模型会整篇换掉格式（自己的
        # ## 小节、没有证据列表头），证据列闸门失去约束对象，0 引用还能过收尾。
        hdr = table_header(text)
        if hdr:
            rec["header"] = hdr
        f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return True
    except (OSError, ValueError, TypeError):
        return False


def drop_state(root: str | Path, path: str) -> None:
    """整篇重写（write_file）或删除文件后，骨架记录作废——文件已经不是骨架了。"""
    try:
        f = state_path(root, path)
        if f.is_file():
            f.unlink()
    except OSError:
        pass


def header_missing(content: str, header: list[str]) -> list[str]:
    """契约表头里哪些列在内容中找不到（归一化比对，忽略空白与标点差异）。

    「表头即契约」的另一半：骨架建档时表头随档案存下，write_file 整篇重写
    必须保留这些列——rerun42 实证模型换掉格式后，证据列闸门就没有了约束对象。
    """
    cn = _norm(content)
    return [c for c in (header or []) if _norm(c) and _norm(c) not in cn]


def registered_paths(root: str | Path) -> set[str]:
    """骨架登记过的文件（resolve 后小写绝对路径集合）——shell 落盘拦截用。

    登记过的文件就是本轮的交付物：内容变更必须走核查链覆盖的入口
    （write_file / fill_section / edit_file）；shell 的 cp/重定向是第四条路，
    会绕过全部写闸（rerun39 实证），对它关闭。
    """
    d = Path(root) / STATE_SUBDIR
    out: set[str] = set()
    try:
        for f in d.glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                p = str(rec.get("path") or "").strip()
                if p:
                    out.add(str(Path(p).resolve()).lower())
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def unfilled_against(text: str, st: dict) -> list[str]:
    """对照骨架记录：哪些待填位还没填（内容与建档时一模一样，或干脆没了）。

    两种形态都认：`write_skeleton` 建的分节骨架看小节正文；任务书骨架建的
    表格看数据行（首列之外还全是「（待填）」= 这一项没写）。
    """
    headings = [str(h) for h in (st.get("headings") or [])]
    bodies = st.get("bodies") or {}
    lines = _lines(text)
    out: list[str] = []
    for h in headings:
        sec, _ = find_section(text, h)
        if sec is None:
            out.append(h)                       # 标题被删了：这节等于没写
            continue
        cur = _clean(sec.body(lines))
        base = str(bodies.get(h) or "")
        if not cur or cur == _clean(base):
            out.append(h)
    rows = [str(r) for r in (st.get("rows") or [])]
    if rows:
        cur_rows = _table_rows(text)
        base_rows = st.get("row_bodies") or {}
        for r in rows:
            line = _lookup_row(cur_rows, r)
            if line is None:
                out.append(r)                   # 行被删了 = 这一项没写
                continue
            if _row_blank(line) or _norm(line) == _norm(str(base_rows.get(r) or "")):
                out.append(r)
    return out


# ---------------------------------------------------------------------------
# 任务书驱动的骨架（契约骨架）
# ---------------------------------------------------------------------------
# 为什么骨架要由任务书生成：rerun29 的交付物「流程全对、内容缺两项」——任务书写明
# 至少覆盖十个对比点，模型只写了八行，引用核查一处没错（它只判「写出来的对不对」），
# 于是缺的两项谁也没发现。缺口不是模型写错了，是**没人把「该写什么」变成结构**。
#
# 所以：任务书里能确定性提取出的必填项（「覆盖这些…：A、B、C」/「附一节『X』」）
# 在任务一开始就落成骨架——每个必填项一个待填位。缺项在骨架阶段就露（进度里直接
# 显示「还没填：会话恢复」），收尾再按同一份清单做覆盖核查，与模型自述无关。
#
# 提取只认显式列举，不做语义猜测：提取不到两项就完全不插手（宁可不管，也不乱建）。

# 「至少覆盖这些对比点：A、B、C」——冒号后是同一条列举
_COVER_RX = re.compile(
    r"(?:至少)?(?:必须|需要|务必要|要)?\s*(?:覆盖|包含|写明|列出|给出)"
    r"[^：:\n]{0,16}[：:]\s*(.+)$")
# 「清单末尾附一节「按性价比排序的借鉴清单」」——引号里的就是一个小节
_QUOTED_SEC_RX = re.compile(
    r"(?:附|加|补|带|给出)(?:一|上|一个)?(?:节|小节|章节|部分)[^「」\n]{0,8}"
    r"「([^」]{2,30})」|「([^」]{2,30})」\s*(?:一节|小节|章节|部分)")
# 任务书标题：「# 任务：逆向分析…」
_TITLE_RX = re.compile(r"^#\s+(?:任务[：:]\s*)?(.+?)\s*$")
# 表格分隔行：| --- | :---: | —— 只含 -、:、| 与空白
_TABLE_SEP_RX = re.compile(r"^\|[\s:\-|]+\|$")
_BULLET_RX = re.compile(r"^[-*+]\s+")

# 一个必填项的长度边界：太短多半是符号，太长说明这一句不是列举项
_ITEM_MIN, _ITEM_MAX = 2, 40
# 骨架最多铺这么多项：再多就不是骨架，是任务书抄写
_SEED_MAX = 16


def _split_items(blob: str) -> list[str]:
    """把「A、B、C（含括注）、D」切成条目，去掉序号与句末标点。"""
    out: list[str] = []
    for part in re.split(r"[、，,;；]|以及", str(blob or "")):
        p = part.strip().strip("。.；;、|").strip()
        p = re.sub(r"^(?:至少|还有|以及)\s*", "", p)
        if not p:
            continue
        if _ITEM_MIN <= len(p) <= _ITEM_MAX:
            out.append(p)
    return out


def _parse_items(brief: str) -> tuple[list[str], list[str]]:
    """把任务书拆成 (逐项列举, 指名小节)。

    前者来自「覆盖这些…：A、B、C」（多为表格行 / 对比项）；后者来自「附一节「X」」
    （任务书自己说的是「一节」，那就该是独立小节，不该塞进表格当一行）。
    """
    cover: list[str] = []
    secs: list[str] = []
    for raw in str(brief or "").splitlines():
        line = raw.strip().lstrip(">").strip()
        line = _BULLET_RX.sub("", line).strip()
        if not line:
            continue
        m = _COVER_RX.search(line)
        if m:
            cover.extend(_split_items(m.group(1)))
        for a, b in _QUOTED_SEC_RX.findall(line):
            q = (a or b).strip()
            if q:
                secs.append(q)
    return cover, secs


def _uniq(items: list[str], skip: set[str], limit: int = _SEED_MAX) -> list[str]:
    out: list[str] = []
    for it in items:
        k = _norm(it)
        if not k or k in skip:
            continue
        skip.add(k)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def required_items(brief: str, limit: int = _SEED_MAX) -> list[str]:
    """任务书里显式列举的「必须覆盖的项」（顺序保持，去重）。

    只提取两种确定形态：①「覆盖/包含…：A、B、C」；②「附一节「X」」。
    提取不到或不足两项就返回空列表——这时不插手，避免把普通句子当结构。
    """
    cover, secs = _parse_items(brief)
    seen: set[str] = set()
    return _uniq(cover, seen, limit) + _uniq(
        secs, seen, max(0, limit - len(_uniq(cover, set(), limit))))


def brief_title(brief: str) -> str:
    """任务书的一级标题（去掉「任务：」前缀），拿不到就给个中性标题。"""
    for raw in str(brief or "").splitlines():
        m = _TITLE_RX.match(raw.strip())
        if m:
            t = m.group(1).strip()
            if 2 <= len(t) <= 80:
                return t
    return "交付物"


def table_shape(brief: str) -> list[str] | None:
    """任务书里声明的表格列名（`| A | B |` + 分隔行）。没有就返回 None。

    有表格就按表格铺骨架：任务书的格式要求是硬性的（rerun29 要求四列逐行），
    这时候硬塞 `## 小节` 会跟格式要求打架——骨架应该长得像**将要交付的东西**。
    """
    lines = _lines(brief)
    for i, s in enumerate(x.strip() for x in lines):
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not (2 <= len(cells) <= 8 and all(cells)):
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if _TABLE_SEP_RX.match(nxt):
            return cells
        # 没有分隔行也认：任务书里的表头常常是手写的一行（rerun29/30 就是这样），
        # 只要上下都不是表格行，它就是「格式声明里的表头」而不是数据行。
        prev = lines[i - 1].strip() if i else ""
        if not nxt.startswith("|") and not prev.startswith("|"):
            return cells
    return None


def outline_from_brief(brief: str) -> tuple[str, list[str], str] | None:
    """按任务书生成骨架文本。返回 (全文, 待填项, 形态)；提取不到返回 None。

    形态 table：任务书自己声明了表格 → 表头照抄，每个必填项一行待填；
    形态 sections：否则每个必填项一个 `## 小节` + 一行待填占位。
    """
    cover, secs = _parse_items(brief)
    items = required_items(brief)
    if len(items) < 2:
        return None
    title = brief_title(brief)
    head = [f"# {title}", "",
            "> 本骨架由 harness 按任务书要求生成：每一项都要填，空的项收尾判未交付；"
            "标题/行首列不要删改（覆盖核查按这一列对账）。", ""]
    cols = table_shape(brief)
    if cols and cover:
        pad = ["（待填）"] * (len(cols) - 1)
        body = ["| " + " | ".join(cols) + " |",
                "| " + " | ".join(["---"] * len(cols)) + " |"]
        for it in cover:
            body.append("| " + " | ".join([it] + pad) + " |")
        out = head + body + [""]
        for it in secs:                    # 任务书点名「一节」的，单独给个小节
            out += [f"## {it}", "", "（待填）", ""]
        return "\n".join(out), items, "table"
    body = []
    for it in items:
        body += [f"## {it}", "", "（待填）", ""]
    return "\n".join(head + body), items, "sections"


def _table_rows(text: str) -> dict[str, str]:
    """文档里所有表格的数据行：{首列文本: 整行}（跳过表头与分隔行）。"""
    lines = _lines(text)
    rows: dict[str, str] = {}
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if (s.startswith("|") and i + 1 < len(lines)
                and _TABLE_SEP_RX.match(lines[i + 1].strip())):
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                raw = lines[j].strip()
                cells = [c.strip() for c in raw.strip("|").split("|")]
                if cells and cells[0]:
                    rows[cells[0]] = raw
                j += 1
            i = j
            continue
        i += 1
    return rows


def _row_blank(row: str) -> bool:
    """表格行是否只有首列有内容（其余都是空/占位符）——即「这一项还没写」。"""
    cells = [c.strip() for c in str(row or "").strip().strip("|").split("|")]
    if len(cells) <= 1:
        return True
    for c in cells[1:]:
        if not c or PLACEHOLDER_RX.match(c) or c.strip("（）()【】 ") == "待填":
            continue
        return False
    return True


def _lookup_row(rows: dict[str, str], label: str) -> str | None:
    if label in rows:
        return rows[label]
    key = _norm(label)
    for k, v in rows.items():
        if _norm(k) == key:
            return v
    for k, v in rows.items():
        nk = _norm(k)
        if len(key) >= 3 and (key in nk or nk in key):
            return v
    return None


def seed_from_brief(root: str | Path, rel: str, brief: str) -> dict | None:
    """按任务书在 rel 上铺骨架文件（已存在则不动）。返回 {path, items, kind}。

    已存在的文件一律不碰：模型可能已经动手写了，覆盖它等于替模型做决定、还
    会把它的进度抹掉。骨架只在「任务开始时这个路径还不存在」这一种情况下落地。
    """
    p = Path(str(rel))
    if not p.is_absolute():
        p = Path(root) / str(rel)
    try:
        if p.exists():
            return None
    except OSError:
        return None
    built = outline_from_brief(brief)
    if built is None:
        return None
    text, items, kind = built
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    except OSError:
        return None
    record_seeded(root, rel, text, items, kind)
    return {"path": str(rel), "items": items, "kind": kind}


def record_seeded(root: str | Path, rel: str, text: str, items: list[str],
                  kind: str) -> bool:
    """给 harness 自己铺的骨架建档，让「待填项」在收尾核查里可判。"""
    try:
        f = state_path(root, rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        rec: dict = {
            "path": _key(root, rel),
            "ts": round(time.time(), 3),
            "source": "brief",
            "kind": kind,
            "headings": [],
            "bodies": {},
            "rows": list(items),
            "row_bodies": {},
        }
        if kind == "table":
            rows = _table_rows(text)
            rec["rows"] = list(items)
            rec["row_bodies"] = {it: rows.get(it, "") for it in items}
        else:
            # 分节形态：items 就是小节标题，记进 headings；rows 必须留空，
            # 否则同一项被算两次（既等小节正文、又等表格行），永远填不完。
            rec["rows"] = []
            rec["row_bodies"] = {}
            lines = _lines(text)
            secs = body_sections(text)
            rec["headings"] = [s.heading for s in secs]
            rec["bodies"] = {s.heading: _clean(s.body(lines)) for s in secs}
        f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return True
    except (OSError, ValueError, TypeError):
        return False


def _flat(s: str) -> str:
    """覆盖比对用的归一化：去空白/标点/大小写差异，只留字。"""
    s = str(s or "").lower()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)      # 括注是补充说明，不参与比对
    return re.sub(r"[\s`*_：:，,。.、\-—~～·|/\\'\"]+", "", s)


def coverage_missing(text: str, required: list[str]) -> list[str]:
    """正文里没出现的必填项（顺序与任务书一致）。

    判据只看「这一项的字面是否出现在交付物里」——不论它是表格首列、小节标题
    还是正文一句话。任务书要求的是「覆盖」，不是「按这个标题分节」，所以不
    强加结构。归一化后做子串匹配；括注里的备选写法（原生/文本兜底）额外认。
    """
    flat = _flat(text)
    if not flat:
        return list(required or [])
    miss: list[str] = []
    for it in required or []:
        key = _flat(it)
        if not key:
            continue
        if key in flat:
            continue
        # 括注里的备选写法也算覆盖（任务书用「（原生/文本兜底）」举例时，
        # 交付物只写「原生工具调用」同样满足，不必逐字照抄）
        alts = re.findall(r"[（(]([^）)]{2,40})[）)]", str(it))
        hit = False
        for alt in alts:
            for piece in re.split(r"[/、，,]", alt):
                k2 = _flat(piece)
                if len(k2) >= 2 and k2 in flat:
                    hit = True
                    break
            if hit:
                break
        if not hit:
            miss.append(it)
    return miss


def required_missing(root: str | Path, rel: str, required: list[str]) -> list[str]:
    """从磁盘上的交付物算「缺哪些必填项」。文件不在时返回全部。"""
    text = _read_text(root, rel)
    if text is None:
        return list(required or [])
    return coverage_missing(text, required)


def _row_index(text: str) -> tuple[list[str], list[tuple[int, str]]]:
    """返回 (全文行, [(行下标, 首列文本)])——只认真正的表格数据行（跳过表头/分隔）。"""
    lines = _lines(text)
    rows: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("|") and i + 1 < len(lines) \
                and _TABLE_SEP_RX.match(lines[i + 1].strip()):
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if cells and cells[0]:
                    rows.append((j, cells[0]))
                j += 1
            i = j
            continue
        i += 1
    return lines, rows


def table_header(text: str) -> list[str] | None:
    """表格的表头单元格（紧邻分隔行的第一行）；没有表格返回 None。"""
    lines = _lines(text)
    for i, s in enumerate(lines):
        if s.strip().startswith("|") and i + 1 < len(lines) \
                and _TABLE_SEP_RX.match(lines[i + 1].strip()):
            return [c.strip() for c in s.strip().strip("|").split("|")]
    return None


def table_data_rows(text: str) -> list[list[str]]:
    """全部数据行的完整单元格（含首列）。"""
    lines, rows = _row_index(text)
    return [[c.strip() for c in lines[idx].strip().strip("|").split("|")]
            for idx, _ in rows]


def row_cells(text: str, label: str) -> list[str] | None:
    """`label` 行的完整单元格（含首列）。找不到 / 命中多行返回 None。"""
    _, rows = _row_index(text)
    want = _norm(label)
    hit = [i for i, lab in rows if _norm(lab) == want]
    if not hit:
        hit = [i for i, lab in rows if want and (want in _norm(lab) or _norm(lab) in want)]
    if len(hit) != 1:
        return None
    cells = [c.strip() for c in _lines(text)[hit[0]].strip().strip("|").split("|")]
    return cells or None


def fill_row(text: str, label: str, content: str) -> tuple[str, str | None]:
    """把表格里 `label` 这一行的其余单元格填上。返回 (新全文, 错误文案)。

    为什么需要它：任务书骨架常常是表格（一表头 + 每个必填项一行），而
    `fill_section` 只认 `## 小节`。没有逐行入口时，模型只能整篇 write_file，
    一次引用错误就全篇作废——rerun32 就这样连撞五轮、最终零交付。
    有了它，单次失败的成本和分节骨架一样：一行。
    """
    body = str(content or "").strip()
    if not body:
        return text, "[错误] content 为空，没有可填入的内容。"
    lines, rows = _row_index(text)
    if not rows:
        return text, "[错误] 这个文件里没有表格（或表格还没有数据行），无法按行填充。"
    want = _norm(label)
    hit = [i for i, lab in rows if _norm(lab) == want]
    if not hit:
        hit = [i for i, lab in rows if want and (want in _norm(lab) or _norm(lab) in want)]
    if len(hit) != 1:
        avail = "、".join(lab for _, lab in rows[:12])
        if not hit:
            return text, (f"[错误] 表格里没有「{label}」这一行，文件未改动。\n"
                          f"可用的行：{avail}")
        return text, (f"[错误] 「{label}」匹配到多行（{len(hit)} 行），不猜。\n"
                      f"可用的行：{avail}\n请用首列原文重新调用。")
    idx = hit[0]
    cells = [c.strip() for c in lines[idx].strip().strip("|").split("|")]
    first = cells[0] if cells else str(label)
    want_cols = len(cells) - 1
    # 模型的格式习惯（rerun34/35 两次现场一致）：给「其余格」也包上首尾 `|`。
    # 按格数消歧，不靠模型自觉：格数 == 其余格数 → 按其余格解释（首列保留）；
    # 格数 == 整行列数 → 按整行对账（首列必须仍是本行标识）。两种都解释不了
    # 的格数才拒收。
    full_row = body.startswith("|")
    if full_row:
        inner = body.strip("|")
        if len([p for p in inner.split("|")]) == want_cols:
            full_row = False
            body = inner
    if full_row:
        # 整行形式（含首列）：列数按整行对账——按「其余格」对账的话少一列也
        # 能通过（rerun34：首列被覆盖、整表错位），且首列必须仍是本行标识。
        row_cells = [p.strip() for p in body.strip("|").split("|")]
        if len(row_cells) != len(cells):
            return text, (f"[错误] content 以 `|` 开头且格数对不上：整行（含首列）"
                          f"需要 {len(cells)} 个单元格，你给了 {len(row_cells)} 个，"
                          f"文件未改动。\n首列是「{first}」。最省事的写法：去掉首尾的"
                          f" `|`，只给其余 {want_cols} 格，如 `证据A | 证据B | 建议`。")
        new_first = row_cells[0]
        got_first = _norm(new_first)
        if got_first != _norm(first) and not (
                got_first and (got_first in _norm(first) or _norm(first) in got_first)):
            return text, (f"[错误] 整行 content 把首列从「{first}」改成了「{new_first}」，"
                          "文件未改动。行首列不要删改——覆盖核查按这一列对账；"
                          f"只填后面的 {want_cols} 格即可。")
        new_row = body
    else:
        parts = [p.strip() for p in body.split("|")]
        got = len(parts)
        new_row = "| " + " | ".join([first] + parts) + " |"
        if got != want_cols:
            # 列数不符会让整张表错位（后面所有行都跟着歪），宁可拒掉这一行。
            return text, (f"[错误] 这一行需要 {want_cols} 个单元格，"
                          f"你给了 {got} 个，文件未改动。\n"
                          f"表头是：{' | '.join(cells)}\n"
                          f"content 用 `|` 分隔其余 {want_cols} 格，例如 "
                          f"`证据A | 证据B | 建议`（首尾带不带 `|` 都行）。")
    out = list(lines)
    out[idx] = new_row
    joined = "\n".join(out)
    if str(text or "").endswith("\n"):
        joined += "\n"
    return joined, None
