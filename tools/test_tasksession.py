#!/usr/bin/env python3
"""任务现场（tasksession）离线测试：Event Log 追加性、Surface 重建、
worker_brief 事实注入、澄清自动答复、批内依赖侦测、interact 钩子。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from agentforge.tasksession import (  # noqa: E402
    TaskSession, batch_dependency_reason, extract_paths)
from agentforge import interact  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


print("== Event Log：追加写、seq 单调、落盘可读 ==")
with tempfile.TemporaryDirectory() as td:
    s = TaskSession("t1", td)
    s.step_start(1, "l4", "分析 learn-claude-code", "将结果写入 evals/analysis.json")
    s.step_end(1, "分析 learn-claude-code", "已提取 10 个对比点",
               ["evals/analysis.json"], failed=False)
    # 让登记的产出在磁盘上真实存在（auto_answer 的「已生成」分支需要）
    (Path(td) / "evals").mkdir(exist_ok=True)
    (Path(td) / "evals" / "analysis.json").write_text("[]", encoding="utf-8")
    s2 = TaskSession("t1", td)          # 同 sid 重跑 → 另开目录不混流
    check("同 sid 重开新目录", s2.path != s.path)
    lines = s.path.read_text(encoding="utf-8").strip().splitlines()
    check("日志两行", len(lines) == 2, f"got {len(lines)}")
    evs = [__import__("json").loads(x) for x in lines]
    check("seq 单调", [e["seq"] for e in evs] == [1, 2])
    check("带时间戳", all(e.get("ts") for e in evs))

    print("== Surface：worker_brief 事实注入 ==")
    s.step_start(2, "l5", "生成差距清单", "读取 evals/analysis.json 与 evals/other.md 对比")
    s.step_end(2, "生成差距清单", "输入文件不存在，未产出", [], failed=False)
    brief = s.worker_brief()
    check("包含已完成步骤标题", "分析 learn-claude-code" in brief)
    check("包含真实产出文件", "evals/analysis.json" in brief)
    check("产出存在性核验（磁盘实查）", "产出文件核验" in brief)
    check("硬规则入块", "一律当作不存在" in brief)

    print("== 澄清自动答复 ==")
    a1 = s.auto_answer("未找到文件 evals/analysis.json，请问该文件的正确名称或路径是？")
    check("已产出文件 → 答复已生成", a1 and "已由本任务前面的步骤生成" in a1, repr(a1))
    a2 = s.auto_answer("未找到文件 evals/other.md，请问该文件在何处？")
    check("上游 goal 提到但未产出 → 答复步骤归属", a2 and "第 2 步" in a2, repr(a2))
    a3 = s.auto_answer("你说的 agent loop 是指哪个项目？")
    check("与文件无关的追问 → 不拦截", a3 is None, repr(a3))
    a4 = s.auto_answer("evals/missing.json 在哪？")
    check("从未提及的路径 → 不拦截（真问用户）", a4 is None, repr(a4))

print("== interact 钩子：自动答复优先于弹窗 ==")
with tempfile.TemporaryDirectory() as td:
    s3 = TaskSession("t3", td)
    s3.step_start(1, "l4", "生成 JSON", "写入 evals/out.json")
    s3.step_end(1, "生成 JSON", "完成", ["evals/out.json"], failed=False)
    (Path(td) / "evals").mkdir()
    (Path(td) / "evals" / "out.json").write_text("[]", encoding="utf-8")
    answers = []

    def hook(q, o):
        answers.append(q)
        return s3.auto_answer(q)

    interact.set_auto_answer(hook)
    try:
        got = interact.ask("evals/out.json 在哪里？", ["它在别的目录下"])
        check("钩子拦截成功", "已由本任务前面的步骤生成" in got, repr(got))
        check("钩子确实被询问", answers)
    finally:
        interact.clear_auto_answer()
    # 摘掉后脚本模式照常（不挂 interactive，返回无用户提示，不阻塞）
    got2 = interact.ask("另一个问题？", [])
    check("摘掉钩子后恢复原流程", "无用户作答" in got2 or "评测环境" in got2, repr(got2))

print("== 批内依赖侦测 ==")


class T:
    def __init__(self, index, goal):
        self.index, self.goal = index, goal


r1 = batch_dependency_reason([
    T(1, "将结果写入 evals/analysis.json"),
    T(2, "读取已完成的 evals/analysis.json 生成差距清单"),
])
check("路径共享 → 串行", r1 and "analysis.json" in r1, repr(r1))
r2 = batch_dependency_reason([
    T(1, "分析 mini-swe-agent 仓库"),
    T(2, "假设已存在 learn-claude-code 分析，对比两者"),
])
check("依赖措辞 → 串行", r2 and "依赖措辞" in r2, repr(r2))
r3 = batch_dependency_reason([
    T(1, "分析 repoA 的 agent loop"),
    T(2, "分析 repoB 的权限系统"),
])
check("真独立 → 放行并行", r3 is None, repr(r3))
r4 = batch_dependency_reason([T(1, "单任务不适用")])
check("单任务不适用", r4 is None, repr(r4))

print("== extract_paths 边界 ==")
check("普通路径", extract_paths("读取 evals/foo.json 和 agentforge/loop.py") ==
      ["evals/foo.json", "agentforge/loop.py"])
check("不误伤普通词", extract_paths("the agent loop and tools") == [])
check("空文本", extract_paths("") == [])

print(f"\n结果：{PASS} 过 / {FAIL} 挂")
sys.exit(1 if FAIL else 0)
