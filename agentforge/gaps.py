"""能力缺口台账：架构层确定性记录「缺了什么」，供技能管理页自动汇总。

三个记录源，全部由程序判定，不靠模型自报：

1. tool_missing        模型调用了一个注册表里不存在的工具（loop._execute）——
                       说明模型认为该有这个能力；
2. capability_missing  任务所需工具/能力在环境里一个都没注册（report_gap 登记，
                       或由诚实守卫在「未达成且不坦白」时归并）；
3. eval_fail           评测集里某条任务失败（bench_tasks 判定后回写）——
                       同类任务反复 fail 在同一层，就是该补 skill 或工具的信号。

台账落 JSON（cfg.state()/gaps.json），按 (kind, key) 合并计数。
记录动作绝不抛异常：台账写不进去不能影响任务本身。
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()

KIND_LABEL = {
    "tool_missing": "工具缺失",
    "capability_missing": "能力缺失",
    "skill_missing": "技能缺失",
    "eval_fail": "评测失败",
    "honesty": "诚实缺失",
    "goal_fidelity": "goal 缩水",
}

_MAX_DETAIL = 300
_MAX_SAMPLES = 5
_MAX_STEP = 80

# ---------------------------------------------------------------------------
# 「小步骤」上下文：缺口登记时要说明缺口发生在哪个步骤上。
#   单任务（拆不动）→ 小步骤 = 去掉客套前缀的任务原文（「帮我在git检索REA」
#                      →「在git检索REA」）；
#   多任务 → 小步骤 = 当前子任务的任务书（如「部署git项目REA到本地」）；
#   多路径重试 → 小步骤 = 正在真跑的实现路径（架构拟出的那步，如「先检索」）。
# 编排层每步开始前 set_step，loop.run 只在没人标记时兜底 ensure_step（不覆盖
# 编排层的更细标记），跑完 reset 防线程复用串味。评测/诚实守卫等脱离该上下文
# 的记录源直接给 record 传 step 参数。
# ---------------------------------------------------------------------------
_CURRENT_STEP: contextvars.ContextVar = contextvars.ContextVar("gap_step",
                                                               default=None)

_POLITE_RX = re.compile(
    r"^(?:麻烦|劳驾|帮忙一下|帮我|帮忙|请你|请|我想|我要|我需要|能不能|可不可以)"
    r"[,，。：:！!？?\s]*")


def norm_step(text, cap: int = _MAX_STEP) -> str:
    """任务原文 → 小步骤口径：压平空白、剥掉开头的客套前缀、截断。

    「帮我在git检索REA」→「在git检索REA」；「请帮我列出项目目录」→「列出项目目录」。
    """
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    prev = None
    while prev != s:                      # 「请帮我…」这类叠词逐层剥
        prev = s
        s = _POLITE_RX.sub("", s).strip()
    return s[:cap]


def set_step(text: str):
    """标记当前小步骤（子任务任务书 / 多路径方案）。返回 token 供 reset_step。"""
    return _CURRENT_STEP.set(norm_step(text) or None)


def reset_step(token) -> None:
    """恢复 set_step 之前的值（token 为 None 表示当时没设过，直接清空）。"""
    if token is not None:
        _CURRENT_STEP.reset(token)
    else:
        _CURRENT_STEP.set(None)


def ensure_step(text: str):
    """只在没人标记时设置（loop.run 兜底用，不覆盖编排层更细的标记）。

    返回 token 或 None（None = 原本就有标记，无需 reset）。
    """
    if _CURRENT_STEP.get() is None:
        return set_step(text)
    return None


def clear_step() -> None:
    """任务收尾清空，防线程复用把旧步骤带进下一个任务的台账。"""
    _CURRENT_STEP.set(None)


def current_step() -> str:
    """当前小步骤（未标记时为空串）。"""
    return _CURRENT_STEP.get() or ""


def _path(cfg) -> Path:
    return Path(cfg.state()) / "gaps.json"


def _empty() -> dict:
    return {"version": 1, "items": {}}


def record(kind: str, key: str, detail: str = "", cfg=None, step: str = "") -> None:
    """记一条缺口。kind/key 见模块 docstring；detail 是一句人话说明；
    step 是当前小步骤——不传就读上下文（set_step / ensure_step 标记的值）。

    步骤记两处：it["last_step"]（该缺口最近发生的小步骤，页面单独展示）+
    每条样例加【小步骤】前缀（样例自描述，回看时不用猜当时在干什么）。
    """
    if cfg is None:
        return
    kind = str(kind or "").strip()
    key = str(key or "").strip()
    if not kind or not key:
        return
    try:
        step = norm_step(step) or current_step()
        with _LOCK:
            p = _path(cfg)
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
                    data = _empty()
            except (OSError, ValueError):
                data = _empty()
            it = data["items"].setdefault(f"{kind}|{key}", {
                "kind": kind, "key": key, "count": 0,
                "first": "", "last": "", "samples": [],
            })
            it["count"] = int(it.get("count") or 0) + 1
            now = time.strftime("%Y-%m-%d %H:%M")
            if not it.get("first"):
                it["first"] = now
            it["last"] = now
            if step:
                it["last_step"] = step
            detail = re.sub(r"\s+", " ", str(detail or "")).strip()[:_MAX_DETAIL]
            if detail:
                if step:
                    detail = f"【{step}】{detail}"
                if detail not in it["samples"]:
                    it["samples"] = (it["samples"] + [detail])[-_MAX_SAMPLES:]
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(p)
    except Exception:  # noqa: BLE001  台账绝不能拖垮任务
        pass


def snapshot(cfg) -> dict:
    """技能管理页要的全量快照：按次数降序，附人话 kind 标签。"""
    try:
        with _LOCK:
            data = json.loads(_path(cfg).read_text(encoding="utf-8"))
        items = [v for v in data.get("items", {}).values()
                 if isinstance(v, dict) and v.get("kind")]
    except (OSError, ValueError):
        items = []
    for it in items:
        it["kind_label"] = KIND_LABEL.get(it.get("kind"), it.get("kind"))
        it["samples"] = (it.get("samples") or [])[:_MAX_SAMPLES]
    items.sort(key=lambda x: (-int(x.get("count") or 0), str(x.get("key"))))
    return {"items": items, "total": sum(int(i.get("count") or 0) for i in items)}


def clear(cfg, kind: str = "", key: str = "") -> int:
    """清空台账；给 kind/key 只清那一条。返回清掉的条数。"""
    try:
        with _LOCK:
            p = _path(cfg)
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return 0
            items = data.get("items") or {}
            if not isinstance(items, dict):
                return 0
            if kind or key:
                drop = [k for k in items
                        if (not kind or items[k].get("kind") == kind)
                        and (not key or items[k].get("key") == key)]
                for k in drop:
                    del items[k]
                removed = len(drop)
            else:
                removed = len(items)
                data["items"] = {}
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(p)
            return removed
    except Exception:  # noqa: BLE001
        return 0


def update(cfg, kind: str, key: str, new_key: str = "",
           samples=None) -> str:
    """手工修订一条记录，只开放 名称(key) 与 样例描述(samples) 两个字段——
    count / first / last 是架构记下的事实，不该由人手改。

    samples 传 None 表示不动，传 list 表示整体替换（逐条压平、截断、去空，
    上限 _MAX_SAMPLES 条）。改名时迁移台账键；目标名已存在则拒绝（不自动合并，
    让前端提示改名）。返回空串表示成功，否则返回给前端展示的失败原因。
    """
    try:
        with _LOCK:
            p = _path(cfg)
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return "台账文件读取失败"
            items = data.get("items") or {}
            if not isinstance(items, dict):
                return "台账文件损坏"
            slot = f"{kind}|{key}"
            it = items.get(slot)
            if it is None:
                return "记录不存在（可能已被删除），刷新后重试"
            nk = str(new_key or "").strip() or key
            nslot = f"{kind}|{nk}"
            if nslot != slot and nslot in items:
                return f"同名记录已存在：{nk}"
            if samples is not None:
                cleaned: list[str] = []
                for s in (samples if isinstance(samples, list) else []):
                    s = re.sub(r"\s+", " ", str(s or "")).strip()[:_MAX_DETAIL]
                    if s:
                        cleaned.append(s)
                it["samples"] = cleaned[:_MAX_SAMPLES]
            it["key"] = nk
            if nslot != slot:
                del items[slot]
                items[nslot] = it
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(p)
            return ""
    except Exception:  # noqa: BLE001  台账操作绝不拖垮页面
        return "写入失败"


__all__ = ["record", "snapshot", "clear", "update", "KIND_LABEL"]
