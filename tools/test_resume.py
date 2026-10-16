"""会话恢复（断点续跑）的回归测试：中断缝隙必须被修复成协议合法的历史。

锁的是这个坑：任务在「assistant 已发出 tool_calls、工具还没执行完」的缝隙里
被打断（服务重启 / 用户停止 / 出错），台账留下的历史里就有一个悬空调用——
没有对应的 tool 应答。直接把这份历史灌回去，下一轮请求会被 Ollama /
OpenAI 兼容接口拒掉（协议要求 assistant 的每个调用都要有应答），
恢复功能整个不可用。

断言写的是：「悬空调用补合成应答（结果未知，不假装成功）」「孤儿 tool 消息
丢弃」「完好的历史原样通过（逐字段不变）」「续跑指令带上原始指令原文」。

运行：.venv/Scripts/python.exe tools/test_resume.py
不需要模型、不需要联网，退出码 0 = 全过。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.resume import INTERRUPTED_TOOL_NOTE, repair_history, resume_brief  # noqa: E402

failed = 0


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  —— {detail}"))
    if not cond:
        global failed
        failed += 1


# ---------- 用例 1：完好的历史原样通过，逐字段不变 ----------
good = [
    {"role": "user", "content": "部署 ERA 项目"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "run_command", "arguments": {"cmd": "git clone ..."}}},
    ]},
    {"role": "tool", "content": "Cloning into 'rea'... done."},
    {"role": "assistant", "content": "部署完成。"},
]
out = repair_history(good)
check("完好历史长度不变", len(out) == len(good), f"got {len(out)}")
check("完好历史逐条相等", out == good)
check("原列表未被修改", good[1].get("tool_calls") is not None and good[3]["content"] == "部署完成。")

# ---------- 用例 2：中断缝隙——assistant 发了调用、工具没应答 ----------
torn = [
    {"role": "user", "content": "改 config.yaml"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "edit_file", "arguments": {"path": "config.yaml"}}},
    ]},
    # 中断在这里：tool 应答缺席
]
out = repair_history(torn)
check("悬空调用补了合成应答", len(out) == 3 and out[-1]["role"] == "tool")
check("合成应答如实说明结果未知", INTERRUPTED_TOOL_NOTE in out[-1]["content"])
check("不假装成功", "成功" not in out[-1]["content"])

# ---------- 用例 3：一次发多个调用、只回来一部分 ----------
torn2 = [
    {"role": "user", "content": "读两个文件"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
        {"function": {"name": "read_file", "arguments": {"path": "b.py"}}},
    ]},
    {"role": "tool", "content": "a.py 的内容"},
    # 第二个调用的应答缺席
]
out = repair_history(torn2)
check("缺几个补几个", len(out) == 4, f"got {len(out)}")
check("补的应答排在真实应答之后", out[-1]["role"] == "tool" and INTERRUPTED_TOOL_NOTE in out[-1]["content"])
check("真实应答未被改动", out[2]["content"] == "a.py 的内容")

# ---------- 用例 4：孤儿 tool 消息（前面没有挂调用的 assistant）丢弃 ----------
orphan = [
    {"role": "tool", "content": "来历不明的工具输出"},
    {"role": "user", "content": "继续"},
]
out = repair_history(orphan)
check("孤儿 tool 消息被丢弃", [m["role"] for m in out] == ["user"], f"got {[m['role'] for m in out]}")
check("user 消息保留", out[0]["content"] == "继续")

# ---------- 用例 5：坏数据不炸（非 dict、未知 role 直接跳过） ----------
junk = [None, "不是字典", {"role": "alien", "content": "?"}, {"role": "user", "content": "正常"}]
out = repair_history(junk)
check("坏数据被过滤", len(out) == 1 and out[0]["role"] == "user", f"got {out}")
check("空历史/None 不炸", repair_history([]) == [] and repair_history(None) == [])  # type: ignore[arg-type]

# ---------- 用例 6：末尾悬空调用（中断恰好卡在决策之后） ----------
tail_torn = [
    {"role": "user", "content": "跑测试"},
    {"role": "assistant", "content": "好的", "tool_calls": [
        {"function": {"name": "run_command", "arguments": {"cmd": "pytest"}}},
    ]},
]
out = repair_history(tail_torn)
check("末尾悬空调用同样补齐", len(out) == 3 and out[-1]["role"] == "tool")

# ---------- 用例 7：续跑指令必须携带原始指令 ----------
brief = resume_brief("部署 git 上的 ERA 项目")
check("续跑指令带原任务原文", "部署 git 上的 ERA 项目" in brief)
check("续跑指令说明先核实再继续", "核实" in brief and "不要重做" in brief)
check("空原文有兜底", "记录缺失" in resume_brief(""))

print()
if failed:
    print(f"FAILED: {failed} 项断言未通过")
    sys.exit(1)
print("ALL PASS")
