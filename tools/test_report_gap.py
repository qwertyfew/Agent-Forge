"""缺口登记工具（agentforge.tools.gap.report_gap）与接线回归。

口径：调用即证据——report_gap 的登记动作全在架构侧（gaps.record 原样入账），
返回值是写死的后续动作指令；kind=tool 记 capability_missing、kind=skill 记
skill_missing；非法 kind / 空 name 拒绝且不入账。同时锁定三处接线：
toolselect.DEFAULT_CORE 常驻、preflight 邮件能力项、系统提示词第 8 条。
删掉任何一条断言，对应行为坏了这里必须变红。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge import gaps, preflight
from agentforge.toolselect import DEFAULT_CORE
from agentforge.tools.base import SAFE
from agentforge.tools.gap import build_gap_tools

# loop.py 只取 SYSTEM_TEMPLATE 文本做断言，不触发完整导入链的重活
import importlib
loop = importlib.import_module("agentforge.loop")


class StubCfg:
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
    tools = build_gap_tools(guard=None, cfg=cfg)
    check("R1 build_gap_tools 返回且工具名为 report_gap",
          len(tools) == 1 and tools[0].name == "report_gap", str([t.name for t in tools]))
    check("R2 report_gap 是 SAFE 风险级（登记不碰任何资源）", tools[0].risk == SAFE, "")
    tool = tools[0]

    # R3 kind=tool → capability_missing + 返回值含「如实」「不许编造」指令
    out = tool.run(kind="tool", name="send_email",
                   spec="给定收件人/主题/正文，通过 SMTP 发送邮件")
    snap = gaps.snapshot(cfg)
    check("R3 缺工具登记为 capability_missing",
          len(snap["items"]) == 1 and snap["items"][0]["kind"] == "capability_missing"
          and snap["items"][0]["key"] == "send_email", str(snap))
    check("R4 spec 原样入账", "SMTP" in snap["items"][0]["samples"][0], "")
    check("R5 工具类返回指令要求如实说明、禁止编造",
          "如实" in out and "编造" in out, out[:80])

    # R6 kind=skill → skill_missing + 返回值要求先检索再总结流程
    out = tool.run(kind="skill", name="excel 批量处理",
                   spec="批量读写 xlsx 并保留格式的可复用流程")
    snap = gaps.snapshot(cfg)
    sk = next(i for i in snap["items"] if i["kind"] == "skill_missing")
    check("R6 缺技能登记为 skill_missing", sk["key"] == "excel 批量处理", str(sk))
    check("R7 技能类返回指令要求检索资料并总结流程",
          "检索" in out and "流程" in out, out[:80])

    # R8 非法 kind 拒绝且不入账
    before = len(gaps.snapshot(cfg)["items"])
    out = tool.run(kind="model", name="gpt5")
    check("R8 非法 kind 拒绝", out.startswith("[错误]")
          and len(gaps.snapshot(cfg)["items"]) == before, out[:60])

    # R9 空 name 拒绝
    out = tool.run(kind="tool", name="  ")
    check("R9 空 name 拒绝", out.startswith("[错误]")
          and len(gaps.snapshot(cfg)["items"]) == before, out[:60])

    # R10 name 归一化：连续空白压成一个空格，超长截断
    tool.run(kind="tool", name="a" * 100 + "   b")
    snap = gaps.snapshot(cfg)
    key = next(i["key"] for i in snap["items"] if i["key"].startswith("a"))
    check("R10 name 截断到 80 字符", len(key) <= 80, str(len(key)))

    # R11 无 spec 时给兜底说明而不是空串
    n_before = len(gaps.snapshot(cfg)["items"])
    tool.run(kind="tool", name="no_spec_cap")
    snap = gaps.snapshot(cfg)
    it = next(i for i in snap["items"] if i["key"] == "no_spec_cap")
    check("R11 无 spec 有兜底说明", bool(it["samples"] and it["samples"][0].strip()), "")

# R12 常驻工具名单里有 report_gap（模型随时要能登记，不能靠按需检索才出现）
check("R12 DEFAULT_CORE 含 report_gap", "report_gap" in DEFAULT_CORE, "")

# R13 预检能力表包含邮件：信号命中且没有 send_email → 缺失
miss = preflight.assess("帮我给张三发一封邮件约明天开会", [])
check("R13 邮件意图命中且缺工具 → 判定缺失",
      any(m["key"] == "email" for m in miss), str(miss))

# R14 环境里有 send_email → 不缺失（能力在就闭嘴）
check("R14 有 send_email 不通告", preflight.assess("发一封邮件", ["send_email"]) == [], "")

# R15 预检通告指路 report_gap（预检缺能力时也要落进缺口栏目）
with tempfile.TemporaryDirectory() as td2:
    note = preflight.note_for("发一封邮件", [], StubCfg(Path(td2)))
    check("R15 预检通告指路 report_gap", "report_gap" in note, note[:80])
    snap = gaps.snapshot(StubCfg(Path(td2)))
    check("R16 预检缺失也入台账", any(i["key"] == "email" for i in snap["items"]), "")

# R17 系统提示词第 8 条：缺能力就登记
check("R17 系统提示含「缺能力就登记」规则",
      "缺能力就登记" in loop.SYSTEM_TEMPLATE and "report_gap" in loop.SYSTEM_TEMPLATE, "")

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
