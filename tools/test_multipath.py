"""多路径尝试（orchestrator.run_task multi_path）回归。

设计口径：一步失败后，先让模型拟几条互不相同的实现路径，再逐条真跑
（换「路」不换模型），直到某条成功或试完。用替身 agent + 替身 hub 钉住：
拟路径的调用形态、逐条尝试的顺序与提示词、成功即停、全败如实上报、
拟路径失败退回内置路径、关闭时不重试。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.orchestrator import FALLBACK_APPROACHES, run_task


class Entry:
    def __init__(self, key, name, scores, total, kind="local"):
        self.id = key
        self.key = key
        self.display = name
        self.scores = scores
        self.total = total
        self.kind = kind
        self.available = True
        self.context_window = 32768


class Store:
    def __init__(self, entries):
        self._e = entries

    def usable(self):
        return self._e

    def fallback_pool(self):
        return self._e

    def find_any(self, key):
        return next((e for e in self._e if e.key == key), None)

    def combo_info(self, pool):
        return {}


class Cfg:
    def __init__(self, root):
        self.workspace_root = str(root)
        self.relay_brief = False
        self.models = {"a": object()}


class Res:
    def __init__(self, content):
        self.content = content


class Hub:
    """拟路径替身：返回固定几条路径；bad=True 时返回垃圾内容。"""

    def __init__(self, bad=False):
        self.bad = bad
        self.calls = 0

    def chat(self, spec, messages, tools=None, options=None, think=False):
        self.calls += 1
        assert messages[0]["role"] == "system" and "实现路径" in messages[0]["content"]
        if self.bad:
            return Res("好的，我不会执行，只给路径。")
        return Res("1. 用 list_dir 查清现状后手工改文件\n"
                   "2. 写一个 python 脚本一次性完成\n"
                   "3. 拆成三小步逐个验证\n"
                   "4. 第四条应该被截掉")


class FlakyAgent:
    """前 n_fail 次 run 报 error，之后正常收尾。"""

    def __init__(self, root, n_fail=1, hub=None):
        self.cfg = Cfg(root)
        self.model_key = "a"
        self.hub = hub or Hub()
        # 开局健康门禁（orchestrator.ollama_health_gate）要读 agent.client；
        # 桩 Cfg 没有 model() 会让 _plan_local_keys 判定「无本地模型」而提前返回，
        # 所以 client 给 None 即可——这里只钉多路径逻辑，不探活。
        self.client = None
        self.n_fail = n_fail
        self.runs = []

    def set_model(self, key):
        self.model_key = key

    @property
    def model_spec(self):
        return object()

    def run(self, hist, prompt, **kw):
        self.runs.append({"model": self.model_key, "prompt": prompt})
        if len(self.runs) <= self.n_fail:
            yield {"type": "error", "message": "模拟失败"}
            return
        hist = [*hist, {"role": "user", "content": prompt},
                {"role": "assistant", "content": "这回成了"}]
        yield {"type": "assistant", "content": "这回成了", "history": hist}


def check(name, cond, detail=""):
    mark = "ok" if cond else "FAIL"
    print(f"  [{_n[0] + 1}] {mark}  {name}" + (f"  {detail}" if detail and not cond else ""))
    _n[0] += 1
    if not cond:
        _fails.append(name)


_n = [0]
_fails: list = []

with tempfile.TemporaryDirectory() as td:
    store = Store([Entry("a", "ModelA", {"coding": 78}, 80)])

    # M1 开启多路径：先拟路径，再逐条尝试；第 2 次执行成功即停
    ag = FlakyAgent(td, n_fail=1, hub=Hub())
    evs = list(run_task(ag, store, [], "你好", multi_path=True))
    kinds = [e["type"] for e in evs]
    mp = [e for e in evs if e["type"] == "multipath"]
    check("M1 有 multipath 事件且先 plan 后 try",
          [e.get("phase") for e in mp][:2] == ["plan", "try"], str(mp))
    check("M2 拟了 3 条路径（第 4 条被截掉）且只试到成功那第 1 条",
          [e.get("approach_index") for e in mp if e.get("phase") == "try"] == [1],
          str([e.get("approach") for e in mp]))
    check("M3 跑了两次：原路径 + 方案 1，提示词含路径内容",
          len(ag.runs) == 2 and "方案 1/3" in ag.runs[1]["prompt"]
          and "list_dir 查清现状" in ag.runs[1]["prompt"], str(ag.runs))
    check("M4 模型不变（换路不换模型）",
          all(r["model"] == "a" for r in ag.runs), str([r["model"] for r in ag.runs]))
    check("M5 最终正常收尾", "complete" in kinds, str(kinds))

    # M6 全部路径都失败：如实上报，试满所有路径后收场
    ag2 = FlakyAgent(td, n_fail=99, hub=Hub())
    evs2 = list(run_task(ag2, store, [], "你好", multi_path=True))
    tries = [e for e in evs2 if e["type"] == "multipath" and e.get("phase") == "try"]
    check("M6 三条路径都试过", [e.get("approach_index") for e in tries] == [1, 2, 3],
          str([e.get("approach_index") for e in tries]))
    check("M7 全败不伪装成完成", "complete" not in [e["type"] for e in evs2],
          str([e["type"] for e in evs2]))

    # M8 模型拟不出路径：退回内置三条
    ag3 = FlakyAgent(td, n_fail=1, hub=Hub(bad=True))
    list(run_task(ag3, store, [], "你好", multi_path=True))
    check("M8 拟路径失败退回内置路径",
          len(ag3.runs) == 2 and FALLBACK_APPROACHES[0][:20] in ag3.runs[1]["prompt"],
          ag3.runs[1]["prompt"][-120:] if len(ag3.runs) > 1 else "")

    # M9 关闭多路径：失败即如实上报，不重试
    ag4 = FlakyAgent(td, n_fail=99)
    evs4 = list(run_task(ag4, store, [], "你好"))
    check("M9 关闭时不重试", len(ag4.runs) == 1
          and not [e for e in evs4 if e["type"] == "multipath"], str(ag4.runs))

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
