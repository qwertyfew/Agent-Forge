"""评测：任务集加载 + 四层不依赖 LLM 的分层判定。

为什么这么做：项目此前的改进全凭感觉（"这个模型好像更强"），没有标尺。
本模块把"任务做没做成"变成可判定的断言，判定链完全走程序、零模型调用：

    1. exact    原文包含：关键字符串一字不差地出现在回复里
    2. norm     归一包含：大小写/全半角/空白归一后再包含（模型爱换格式）
    3. numeric  数值断言：从回复里提取数字，与期望值比对（带容差）
    4. fuzzy    关键词组：每组给若干可接受的写法（同义词），命中比例达标即过
    5. check    产物核验：在沙盒里真跑一条命令（复用 verify.run_verify_cmd），
                退出码 0 且（可选）输出包含指定内容才算过——代码类任务的硬证据

判定按上述顺序执行，**第一个失败的层就是失败归因**（拆解/执行质量/产物
问题分别对应不同层的失败形态）。思考过程不作数：判定只看最终回复与
沙盒里的真实文件——这与项目自己的哲学一致：用"测试会不会红"说话。

任务集是数据不是代码（evals/tasks.yaml）：加一条评测就是加一段 YAML，
不改判定器。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import verify as verify_mod

# 数字提取：负号 + 整数/小数。判定只认阿拉伯数字——中文数字（"七"）不算，
# 任务书会要求模型"直接告诉我数字"。
_NUM_RX = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class EvalCase:
    """一条评测任务。seed 会先写进沙盒，expect 是判定条件（见模块头）。"""

    id: str
    category: str
    prompt: str
    expect: dict = field(default_factory=dict)
    seed: dict[str, str] = field(default_factory=dict)   # 相对路径 -> 内容
    notes: str = ""
    ask_answers: list[str] = field(default_factory=list)  # 澄清弹窗脚本答案（评测模式按序弹出）


def load_cases(path: str | Path) -> list[EvalCase]:
    """从 YAML 加载任务集。格式错误直接抛异常——评测集坏了好过静默漏测。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if isinstance(raw, dict):
        raw = raw.get("cases") or []
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not (item.get("id") and item.get("prompt")):
            raise ValueError(f"任务集第 {i} 条缺少 id/prompt")
        cid = str(item["id"])
        if cid in seen:
            raise ValueError(f"任务 id 重复：{cid}")
        seen.add(cid)
        expect = item.get("expect") or {}
        if not expect:
            raise ValueError(f"任务 {cid} 没有任何判定条件（expect 为空）")
        cases.append(EvalCase(
            id=cid,
            category=str(item.get("category") or "general"),
            prompt=str(item["prompt"]),
            expect=expect,
            seed={str(k): str(v) for k, v in (item.get("seed") or {}).items()},
            notes=str(item.get("notes") or ""),
            ask_answers=[str(x) for x in (item.get("ask_answers") or [])],
        ))
    return cases


def _norm(text: str) -> str:
    """归一：NFKC（全半角统一）→ 小写 → 连续空白压成单个空格。

    模型输出爱在格式上自由发挥（全角冒号、多余空格、大小写），内容对就算对。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.casefold()
    return re.sub(r"\s+", " ", s).strip()


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM_RX.finditer(str(text or "")):
        try:
            out.append(float(m.group(0)))
        except ValueError:  # pragma: no cover - 正则保证可解析
            continue
    return out


def _group_hit(groups: list, output_norm: str) -> tuple[int, int, list[int]]:
    """关键词组命中统计。每组是若干可接受写法（命中任一即该组命中）。

    groups 兼容两种写法：["a", "b"]（一组一词）与 [["a","b"], ["c"]]（一组多词）。
    返回 (命中数, 总组数, 未命中组的下标)。
    """
    hit, miss = 0, 0
    miss_idx: list[int] = []
    for i, g in enumerate(groups or []):
        alts = g if isinstance(g, list) else [g]
        if any(_norm(a) in output_norm for a in alts):
            hit += 1
        else:
            miss += 1
            miss_idx.append(i)
    return hit, hit + miss, miss_idx


def judge(case: EvalCase, output: str, sandbox: str | Path,
          check_timeout: int = 60) -> dict:
    """按层判定一次任务产出。返回：

        verdict  pass | fail
        layer    通过时 = 实际跑到的最后一层；失败时 = 第一个失败的层（归因）
        detail   人话说明（缺了什么 / 差多少）
        data     各层的原始结果（给 bench_compare 做二次分析）
    """
    out_raw = str(output or "")
    out_norm = _norm(out_raw)
    exp = case.expect or {}
    data: dict = {}

    # ---- 1. exact -------------------------------------------------------
    need = [str(x) for x in (exp.get("contains") or [])]
    if need:
        missing = [s for s in need if s not in out_raw]
        data["exact"] = {"missing": missing}
        if missing:
            return {"verdict": "fail", "layer": "exact", "detail": f"缺少原文片段：{missing}", "data": data}

    # ---- 2. norm --------------------------------------------------------
    need_norm = [str(x) for x in (exp.get("contains_norm") or [])]
    if need_norm:
        missing = [s for s in need_norm if _norm(s) not in out_norm]
        data["norm"] = {"missing": missing}
        if missing:
            return {"verdict": "fail", "layer": "norm", "detail": f"归一后缺少：{missing}", "data": data}

    # ---- 3. numeric ------------------------------------------------------
    nums_spec = exp.get("numbers") or []
    if nums_spec:
        got = _numbers(out_raw)
        misses = []
        for spec in nums_spec:
            want = float(spec["value"])
            tol = abs(float(spec.get("tol", 0.01)))
            if not any(abs(g - want) <= tol for g in got):
                misses.append({"want": want, "tol": tol})
        data["numeric"] = {"found": got[:20], "missing": misses}
        if misses:
            return {"verdict": "fail", "layer": "numeric",
                    "detail": f"数值不符：期望 {misses}，提取到 {got[:10]}", "data": data}

    # ---- 4. fuzzy --------------------------------------------------------
    groups = exp.get("keywords") or []
    if groups:
        min_ratio = float(exp.get("min_ratio", 0.7))
        hit, total, miss_idx = _group_hit(groups, out_norm)
        ratio = (hit / total) if total else 1.0
        data["fuzzy"] = {"hit": hit, "total": total, "ratio": round(ratio, 2)}
        if ratio < min_ratio:
            return {"verdict": "fail", "layer": "fuzzy",
                    "detail": f"关键词组命中 {hit}/{total}（要求 ≥{min_ratio}），"
                              f"未命中组：{[groups[i] for i in miss_idx]}",
                    "data": data}

    # ---- 5. must_not（所有"该有的"都齐了再看"不该有的"）-----------------
    forbidden = [str(x) for x in (exp.get("must_not") or [])]
    if forbidden:
        hit_bad = [s for s in forbidden if _norm(s) in out_norm]
        data["must_not"] = {"hit": hit_bad}
        if hit_bad:
            return {"verdict": "fail", "layer": "must_not",
                    "detail": f"出现了禁止内容：{hit_bad}", "data": data}

    # ---- 6. check：真跑命令，硬证据 --------------------------------------
    check = str(exp.get("check") or "").strip()
    if check:
        res = verify_mod.run_verify_cmd(check, sandbox, timeout=check_timeout)
        want_out = exp.get("check_contains")
        ok = bool(res.get("ok"))
        if ok and want_out:
            ok = _norm(want_out) in _norm(res.get("output") or "")
        data["check"] = {"cmd": check, "ok": bool(res.get("ok")),
                         "code": res.get("code"), "output": (res.get("output") or "")[:400]}
        if not ok:
            return {"verdict": "fail", "layer": "check",
                    "detail": f"核验命令未过：{check}（exit={res.get('code')}）"
                              f" 输出片段：{(res.get('output') or '')[:200]!r}",
                    "data": data}

    last = "check" if check else ("must_not" if forbidden else
                                  ("fuzzy" if groups else ("numeric" if nums_spec else
                                                           ("norm" if need_norm else "exact"))))
    return {"verdict": "pass", "layer": last, "detail": "通过", "data": data}


__all__ = ["EvalCase", "load_cases", "judge", "_norm", "_numbers"]
