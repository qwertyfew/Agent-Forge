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


def progress(root: str | Path, rel: str) -> dict:
    """状态感知的进度：{total, filled, pending, unfinished}。"""
    text = _read_text(root, rel) or ""
    st = load_state(root, rel)
    if st:
        pend = unfilled_against(text, st)
        total = len(st.get("headings") or [])
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


def unfilled_against(text: str, st: dict) -> list[str]:
    """对照骨架记录：哪些小节还没填（正文与建档时一模一样，或干脆没了）。"""
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
    return out
