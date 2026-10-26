"""后果定档路由（swe-router 思路）的自测：planner.stakes_of + Router.capability。

运行：python tools/test_router_stakes.py
全部断言内建，退出码 0 = 全过。纯程序测试，不需要模型在线。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.core.config import Config                # noqa: E402
from agentforge.core.planner import stakes_of            # noqa: E402
from agentforge.core.router import Router                # noqa: E402

_n = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    _n[0] += 1
    tag = "ok" if cond else "FAIL"
    print(f"  [{_n[0]:>2}] {tag}  {name}" + (f"  —— {detail}" if (detail and not cond) else ""))
    if not cond:
        raise AssertionError(name)


class FakeEntry:
    def __init__(self, key, display, scores, kind="local", enabled=True):
        self.key, self.display, self.scores = key, display, scores
        self.kind, self.enabled = kind, enabled
        self.available = True
        self.total = max(scores.values()) if scores else 0


class FakeStore:
    def __init__(self, entries):
        self._catalog = entries

    def usable(self):
        return [e for e in self._catalog if e.enabled]

    def usable(self):
        return self._catalog


def make_store():
    return FakeStore([
        # q4 模拟「装了但没勾选」：存在更强（更省）候选，但改用它需要授权
        FakeEntry("q4", "Qwen-4B",
                  {"coding": 60, "reasoning": 62, "writing": 58, "chinese": 60, "cost": 96},
                  enabled=False),
        FakeEntry("gemma", "Gemma-12B",
                  {"coding": 78, "reasoning": 82, "writing": 80, "chinese": 62, "cost": 95}),
        FakeEntry("paid1", "DeepSeek",
                  {"coding": 91, "reasoning": 90, "writing": 88, "chinese": 80, "cost": 40},
                  kind="paid", enabled=False),
    ])



def router(allow_upgrade=False, allow_paid=False) -> Router:
    cfg = Config()
    cfg.routing_allow_upgrade = allow_upgrade
    cfg.routing_allow_paid_upgrade = allow_paid
    cfg.routing_stakes_tiers = {"low": 0, "medium": 78, "high": 86}
    return Router(cfg)


print("== stakes_of：后果分档 ==")
check("高后果：数据库 → high", stakes_of("帮我迁移生产数据库的表结构")[0] == "high")
check("高后果：合同 → high", stakes_of("写一份给客户的合同附件")[0] == "high")
check("低风险：草稿 → low", stakes_of("随便写个 demo 玩玩")[0] == "low")
check("低风险：先看看 → low", stakes_of("先看看目录里有什么")[0] == "low")
check("默认：常规 → medium", stakes_of("把这段话总结成三点")[0] == "medium")
check("同时命中时 high 优先",
      stakes_of("写个 demo 脚本清理生产数据库")[0] == "high",
      "误把高后果当低风险的代价远大于反过来")
check("空文本安全", stakes_of("") == ("medium", stakes_of("")[1]))

print("\n== capability：满足下限的最便宜模型 ==")
store = make_store()

r = router()  # allow_upgrade=False, allow_paid=False
cap = r.capability("随便写个 demo 脚本，打印 hello", store)
check("低风险档 floor=0", cap["stakes"] == "low" and cap["floor"] == 0)
check("未授权时实际用已启用池最强", cap["chosen_key"] == "gemma", cap["chosen_key"])
check("未授权但有更省候选 → 提示", "更省候选" in cap["notice"], cap["notice"])
check("绝不擅自用付费模型", cap["chosen_key"] != "paid1")

r2 = router(allow_upgrade=True)
cap2 = r2.capability("随便写个 demo 脚本，打印 hello", store)
check("授权后低风险任务自动改用最便宜", cap2["chosen_key"] == "q4", cap2["chosen_key"])
check("auto 标志打开", cap2["auto"] and cap2["upgrade"])

cap3 = r.capability("修复这个 python 函数里的 bug", store)
check("常规档 floor=78：4B 不达标", cap3["floor"] == 78 and cap3["chosen_key"] == "gemma",
      f"{cap3['floor']} {cap3['chosen_key']}")

cap4 = r.capability("这是生产数据库的迁移脚本，帮我检查", store)
check("高后果档 floor=86：本地最高 78 不达标 → 兜底",
      cap4["floor"] == 86 and cap4["chosen_key"] == "gemma" and cap4["shortfall"] == 8,
      f"floor={cap4['floor']} shortfall={cap4['shortfall']}")
check("下限未达时明说", "没有达标模型" in cap4["notice"], cap4["notice"])
check("高后果也不擅自上付费", cap4["chosen_key"] != "paid1")

r3 = router(allow_upgrade=True, allow_paid=True)
cap5 = r3.capability("这是生产数据库的迁移脚本，帮我检查", store)
check("全授权 + 高后果 → 自动上最强", cap5["chosen_key"] == "paid1" and cap5["auto"],
      cap5["chosen_key"])
check("达标时 shortfall=0", cap5["shortfall"] == 0)

cap6 = r.capability("写一份合同附件，总结条款要点", store)
check("写作类看 writing+chinese 取最小值",
      cap6["dimension"] == "writing" and cap6["chosen_score"] == 62,
      f"{cap6['dimension']} {cap6['chosen_score']}")
check("返回结构带 stakes/floor 字段",
      all(k in cap6 for k in ("stakes", "floor", "shortfall", "reason", "notice")))

print(f"\n全部断言通过（{_n[0]} 项）。")
