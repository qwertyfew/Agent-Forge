"""能力缺口台账：架构层确定性记录「缺了什么」，供技能管理页自动汇总。

三个记录源，全部由程序判定，不靠模型自报：

1. tool_missing        模型调用了一个注册表里不存在的工具（loop._execute）——
                       说明模型认为该有这个能力；
2. capability_missing  预检发现任务意图需要某能力，但对应工具一个都没注册
                       （preflight.missing_capabilities）；
3. eval_fail           评测集里某条任务失败（bench_tasks 判定后回写）——
                       同类任务反复 fail 在同一层，就是该补 skill 或工具的信号。

台账落 JSON（cfg.state()/gaps.json），按 (kind, key) 合并计数。
记录动作绝不抛异常：台账写不进去不能影响任务本身。
"""
from __future__ import annotations

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
}

_MAX_DETAIL = 300
_MAX_SAMPLES = 5


def _path(cfg) -> Path:
    return Path(cfg.state()) / "gaps.json"


def _empty() -> dict:
    return {"version": 1, "items": {}}


def record(kind: str, key: str, detail: str = "", cfg=None) -> None:
    """记一条缺口。kind/key 见模块 docstring；detail 是一句人话说明。"""
    if cfg is None:
        return
    kind = str(kind or "").strip()
    key = str(key or "").strip()
    if not kind or not key:
        return
    try:
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
            detail = re.sub(r"\s+", " ", str(detail or "")).strip()[:_MAX_DETAIL]
            if detail and detail not in it["samples"]:
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
