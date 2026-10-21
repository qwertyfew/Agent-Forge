"""答复重复段去重（程序侧，确定性）。

**起因订正（很重要）**：rerun58/59 的评测里「同一份答复连刷三遍」，一开始被判成
模型生成退化，于是写了这个模块。事后核对事件流发现是**评测口径的错**：打回重写轮
会把整段答案重发一遍，而 dispatch_task 把**所有轮次**的 delta 全累加进了 reply.txt，
三份其实是「两次被打回的草稿 + 最后一次交付」。真正的修法在 dispatch_task（按 step
清空），已在同一批改动里修掉。

那这个模块还留着做什么：它是**兜底**，判据只针对「单条消息内部成块重复」这件事，
不依赖评测脚本怎么写。单条答复里连续 ≥3 段原样重复、或隔一两段又把 ≥40 字的话原样
说一遍，无论怎么解释都是缺陷——去掉它，用户读到的才是干净的。

判据：段落指纹（先抹掉 markdown 修饰与标点空白，措辞微调不影响）+ 两档门槛：
  · 近距离重复（间隔 ≤4 段）：≥1 段、≥40 字；
  · 长距离成块重复：≥3 段、≥200 字。
正常答复里重复同一个命令、重复引用同一个链接都不满足条件（见 test_dedupe）。
"""
from __future__ import annotations

import hashlib
import re

# 归一化时抹掉的「格式噪声」：markdown 修饰、标点、空白
_NOISE_RX = re.compile(r"[\s*`#>_~\-—:：,，。.!！?？\"'“”‘’()（）\[\]【】<>|/\\]+")


def _norm(p: str) -> str:
    return _NOISE_RX.sub("", (p or "")).lower()


def _fp(p: str, idx: int) -> str:
    n = _norm(p)
    if not n:
        return f"~empty{idx}"          # 空段落不参与重复判定
    return hashlib.sha1(n.encode("utf-8")).hexdigest()[:12]


def _drop_gapped(paras: list[str], fps: list[str], *,
                 min_paras: int, min_chars: int) -> tuple[int, int] | None:
    """找「隔开一段」的成块重复（中间可能夹了插入句）。返回 (起点 j, 段数 k)。"""
    best: tuple[int, int, int] | None = None      # (段数, 起点 j, 字符数)
    n = len(paras)
    for i in range(n):
        if fps[i].startswith("~empty"):
            continue
        for j in range(i + 2, n):
            if fps[i] != fps[j]:
                continue
            k = 0
            while j + k < n and i + k < j and fps[i + k] == fps[j + k]:
                k += 1
            if k < min_paras:
                continue
            chars = sum(len(paras[t]) for t in range(j, j + k))
            if chars < min_chars:
                continue
            if best is None or k > best[0]:
                best = (k, j, chars)
    return None if best is None else (best[1], best[0])


def _drop_near(paras: list[str], fps: list[str], *,
               max_gap: int = 4, min_chars: int = 40) -> tuple[int, int] | None:
    """找「相隔几段就原样重说一遍」的重复块（AA…A 型）。

    rerun58 的收尾就是这样：总结块写完、中间夹一句状态行，然后又原样写一遍。
    与长距离重复不同，这种近距离重复不存在正当理由（正常行文不会在两句之内
    把同一段 ≥100 字的话再说一次），所以段数门槛只要 1 段。
    """
    n = len(paras)
    for i in range(n):
        if fps[i].startswith("~empty"):
            continue
        for d in range(1, max_gap + 1):
            j = i + d
            if j >= n or fps[i] != fps[j]:
                continue
            k = 0
            while j + k < n and fps[i + k] == fps[j + k]:
                k += 1
            chars = sum(len(paras[t]) for t in range(j, j + k))
            if chars >= min_chars:
                return (j, k)
    return None


def dedupe_repeated(text: str, *, min_paras: int = 3, min_chars: int = 200) -> tuple[str, int]:
    """去掉成块重复的段落。返回 (新文本, 被删字符数)。删不动就原样返回。"""
    text = text or ""
    if len(text) < 80:              # 太短谈不上「成块重复」，直接放行
        return text, 0
    paras = re.split(r"\n[ \t]*\n", text)
    if len(paras) < 4:
        return text, 0
    fps = [_fp(p, i) for i, p in enumerate(paras)]
    removed = 0
    for _ in range(8):
        hit = (_drop_near(paras, fps)
               or _drop_gapped(paras, fps, min_paras=min_paras, min_chars=min_chars))
        if not hit:
            break
        j, k = hit
        removed += sum(len(paras[t]) for t in range(j, j + k))
        del paras[j:j + k]
        del fps[j:j + k]
    if not removed:
        return text, 0
    return "\n\n".join(paras), removed


__all__ = ["dedupe_repeated"]
