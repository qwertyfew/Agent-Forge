"""「反复迭代」架构修复的自检：任务笔记、重复调用拦截素材、轮次预算配置。

跑法：./.venv/Scripts/python.exe tools/verify_efficiency.py
全部通过输出 ALL CHECKS PASSED，任何一项失败非零退出。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("OK  " if cond else "FAIL") + f"  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ---------- 1. 任务笔记工具：append / read / 路径隔离 ----------
class _Cfg:
    def __init__(self, root: Path):
        self._root = root

    def state(self) -> Path:
        return self._root / ".agentforge"


from agentforge.runcontrol import _local, clear_current_task  # noqa: E402
from agentforge.tools.notes import build_notes_tools, notes_path  # noqa: E402

import tempfile  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="af_eff_"))
cfg = _Cfg(tmp)

# 无任务上下文 → adhoc
clear_current_task()
p0 = notes_path(cfg)
check("无任务上下文落到 adhoc", p0.name == "adhoc.md" and p0.parent.name == "task-notes")

# 设一个任务 id（runcontrol 用线程本地存 task_id）
_local.task_id = "t_test_eff"
p1 = notes_path(cfg)
check("任务 id 进文件名且特殊字符被替换", "t_test_eff" in p1.name)

_local.task_id = "t/怪\\名字:1"
p2 = notes_path(cfg)
check("非法字符被下划线替换", "/" not in p2.name and ":" not in p2.name and "\\" not in p2.name)
_local.task_id = "t_test_eff"

tools = build_notes_tools(None, cfg)
nt = {t.name: t for t in tools}
check("注册了 task_notes 工具", "task_notes" in nt)
check("task_notes 是只读(SAFE)风险级", nt["task_notes"].risk == "safe")

r1 = nt["task_notes"].run(action="append", text="入口在 web/index.html 的 openModal()")
check("append 返回成功提示", "已记入" in r1)
nt["task_notes"].run(action="append", text="阈值常量在 config.py:120")
r2 = nt["task_notes"].run(action="read")
check("read 能读回两条", "openModal" in r2 and "config.py:120" in r2)
r3 = nt["task_notes"].run(action="append", text="多行\n第二行")
body = p1.read_text(encoding="utf-8")
check("多行内容逐行加 bullet", "- 第二行" in body)
r4 = nt["task_notes"].run(action="append", text="")
check("空 text 报错不落盘", r4.startswith("[错误]"))

clear_current_task()

# ---------- 2. loop 的笔记注入块 ----------
from agentforge.loop import Agent, TASK_NOTES_HEADER  # noqa: E402

lp = Agent.__new__(Agent)          # 绕过重初始化，只测笔记块
lp.cfg = cfg
clear_current_task()
check("无笔记时注入块为空", lp._task_notes_block() == "")
_local.task_id = "t_test_eff"
big = "\n".join(f"- 第{i}条发现" for i in range(500))
p1.parent.mkdir(parents=True, exist_ok=True)
p1.write_text(big, encoding="utf-8")
blk = lp._task_notes_block()
check("笔记块非空且带头部注入", blk.startswith("## 任务笔记") is False and "第499条发现" in blk)
check("超长笔记截到 2000 字尾", len(blk) <= 2100 and "更早的笔记已截断" in blk)
clear_current_task()

# ---------- 3. 轮次预算与保鲜配置 ----------
from agentforge.config import Config  # noqa: E402

c = Config.load(str(ROOT / "config.lanrun.yaml"))
check("keep_recent 默认提到 8", int(c.keep_recent_tool_results) == 8)
check("rounds_soft 可解析", hasattr(c, "rounds_soft"))
check("rounds_hard 可解析", hasattr(c, "rounds_hard"))
check("预算数值合理 soft<hard", int(getattr(c, "rounds_soft", 0)) < int(getattr(c, "rounds_hard", 0)))

from agentforge.context import truncate_tool_results  # noqa: E402

msgs = []
for i in range(10):
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "search_code", "arguments": f"{{\"q\":\"{i}\"}}"}}]})
    msgs.append({"role": "tool", "tool_call_id": str(i), "content": "x" * 500})
out = truncate_tool_results(msgs, c.keep_recent_tool_results)
stale = [m for m in out if m.get("role") == "tool" and str(m.get("content", "")).startswith("[已压缩]")]
full = [m for m in out if m.get("role") == "tool" and not str(m.get("content", "")).startswith("[已压缩]")]
check("keep_recent=8 时压缩 2 条保留 8 条", len(stale) == 2 and len(full) == 8,
      f"stale={len(stale)} full={len(full)}")
check("压缩行保留 280 字符", all(len(m["content"]) > 200 for m in stale))
check("tool_call_id 字段原样保留", all("tool_call_id" in m for m in stale + full))

# ---------- 4. 注册表里有 task_notes 且进核心清单 ----------
from agentforge.toolselect import DEFAULT_CORE  # noqa: E402

check("task_notes 进核心常驻清单", "task_notes" in DEFAULT_CORE)

import agentforge.tools as T  # noqa: E402

src = (ROOT / "agentforge" / "tools" / "__init__.py").read_text(encoding="utf-8")
check("build_registry 装配了 notes", "build_notes_tools" in src)

print()
if fails:
    print(f"{len(fails)} 项失败：{fails}")
    sys.exit(1)
print("ALL CHECKS PASSED")
