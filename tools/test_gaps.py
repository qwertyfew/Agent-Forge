"""能力缺口台账（agentforge.gaps）回归。

口径：记录/合并计数/快照/清空；记录动作绝不抛异常；并发安全不在此测
（threading.Lock + 原子替换已覆盖，构造竞态的测试本身不稳定）。
删掉任何一条断言，对应行为坏了这里必须变红。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import gaps


class StubCfg:
    """只提供 state() 的最小替身：台账落临时目录。"""

    def __init__(self, root: Path):
        self._root = root

    def state(self) -> Path:
        return self._root


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "ok" if cond else "FAIL"
    print(f"  [{_n[0] + 1}] {mark}  {name}" + (f"  {detail}" if detail and not cond else ""))
    _n[0] += 1
    if not cond:
        _fails.append(name)


_n = [0]
_fails: list = []

with tempfile.TemporaryDirectory() as td:
    cfg = StubCfg(Path(td))

    # G1 空快照安全
    snap = gaps.snapshot(cfg)
    check("G1 空台账快照安全", snap == {"items": [], "total": 0}, str(snap))

    # G2 记录 + 合并计数
    gaps.record("tool_missing", "web_search_x", "第一次", cfg)
    gaps.record("tool_missing", "web_search_x", "第二次", cfg)
    snap = gaps.snapshot(cfg)
    check("G2 同 key 合并计数", len(snap["items"]) == 1 and snap["items"][0]["count"] == 2,
          json.dumps(snap, ensure_ascii=False))
    check("G3 样本去重且保序", snap["items"][0]["samples"] == ["第一次", "第二次"],
          str(snap["items"][0]["samples"]))
    check("G4 人话标签", snap["items"][0]["kind_label"] == "工具缺失", "")

    # G5 不同 key 分条；total = 次数和
    gaps.record("capability_missing", "github", "意图命中但工具全缺", cfg)
    snap = gaps.snapshot(cfg)
    check("G5 不同 key 分条、total 汇总",
          len(snap["items"]) == 2 and snap["total"] == 3,
          json.dumps(snap, ensure_ascii=False))

    # G6 按次数降序
    check("G6 按次数降序", snap["items"][0]["key"] == "web_search_x", "")

    # G7 空参数拒绝
    gaps.record("", "", "空参数", cfg)
    snap2 = gaps.snapshot(cfg)
    check("G7 空 kind/key 不入库", len(snap2["items"]) == 2, "")

    # G8 cfg=None 不抛异常（台账绝不能拖垮任务）
    try:
        gaps.record("tool_missing", "whatever", "", None)
        check("G8 cfg=None 安全", True)
    except Exception as e:  # noqa: BLE001
        check("G8 cfg=None 安全", False, str(e))

    # G9 detail 截断到上限
    gaps.record("eval_fail", "coding/long", "x" * 500, cfg)
    snap3 = gaps.snapshot(cfg)
    d = next(i for i in snap3["items"] if i["key"] == "coding/long")
    check("G9 detail 截断", len(d["samples"][0]) <= gaps._MAX_DETAIL, "")

    # G10 单条清空
    n = gaps.clear(cfg, kind="eval_fail", key="coding/long")
    snap4 = gaps.snapshot(cfg)
    check("G10 按 kind+key 单条清空", n == 1 and len(snap4["items"]) == 2, "")

    # G11 全部清空
    n = gaps.clear(cfg)
    check("G11 全部清空", n == 2 and gaps.snapshot(cfg) == {"items": [], "total": 0}, "")

    # G12 文件内容损坏时恢复为空台账而不是崩
    p = Path(td) / "gaps.json"
    p.write_text("not json", encoding="utf-8")
    snap5 = gaps.snapshot(cfg)
    check("G12 损坏文件恢复安全", snap5 == {"items": [], "total": 0}, "")

    # G13-G19 手工修订（update）：只开放名称与样例，count/first/last 不开放
    gaps.record("tool_missing", "send_sms", "调用 send_sms 失败", cfg)
    err = gaps.update(cfg, kind="tool_missing", key="send_sms",
                      new_key="send_sms_v2",
                      samples=["新版调用失败  ", "again   fail"])
    snap6 = gaps.snapshot(cfg)
    check("G13 改名+改样例成功且返回空串", err == "", err)
    check("G14 改名后台账键迁移、样例逐条压平替换",
          len(snap6["items"]) == 1 and snap6["items"][0]["key"] == "send_sms_v2"
          and snap6["items"][0]["samples"] == ["新版调用失败", "again fail"],
          json.dumps(snap6, ensure_ascii=False))
    check("G15 次数与首末时间不被 update 触碰",
          snap6["items"][0]["count"] == 1 and bool(snap6["items"][0]["first"]), "")
    gaps.record("tool_missing", "other", "", cfg)
    err = gaps.update(cfg, kind="tool_missing", key="send_sms_v2", new_key="other")
    check("G16 目标名已存在时拒绝改名", err != "", err)
    err = gaps.update(cfg, kind="tool_missing", key="no_such", new_key="x")
    check("G17 记录不存在返回人话原因", err != "", err)
    err = gaps.update(cfg, kind="tool_missing", key="send_sms_v2", samples=None)
    snap7 = gaps.snapshot(cfg)
    check("G18 samples=None 不动样例、只改名成功",
          err == "" and len(snap7["items"]) == 2
          and any(i["key"] == "send_sms_v2" and i["samples"] == ["新版调用失败", "again fail"]
                  for i in snap7["items"]), json.dumps(snap7, ensure_ascii=False))
    err = gaps.update(cfg, kind="tool_missing", key="send_sms_v2", samples=[])
    snap8 = gaps.snapshot(cfg)
    check("G19 samples=[] 显式清空样例",
          err == "" and any(i["key"] == "send_sms_v2" and i["samples"] == []
                            for i in snap8["items"]), json.dumps(snap8, ensure_ascii=False))

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
