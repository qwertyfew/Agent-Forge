# -*- coding: utf-8 -*-
"""ops_batch：程序化工具调用（PTC，Programmatic Tool Calling）。

token 经济诊断（docs/token-economy-20261019.md）的 T1：一次分析往往要
「读 N 个文件 + 检索 M 次 + 汇总」，逐个工具调用意味着 N+M 轮 LLM 往返，
每轮都重发整个上下文（实测 rerun14：prompt:completion ≈ 20:1，130 轮里
九成轮次的模型输出只是几十 token 的工具调用）。

PTC 把这个循环从模型上下文里搬出去：模型写**一段脚本**，脚本在本地自由
组合工具调用（循环/条件/批量），**只有最终紧凑结果回给模型**——
「读 10 个文件 + grep 5 次 + 汇总」从 15+ 轮压成 1 轮。dsh-ops 同思路，
实测省 65–97% token。

管线纪律（与逐个调工具完全一致，不是旁路）：
- SDK 每次调用都走 Agent._execute()——审批、证据台账（read 硬闸照样记账）、
  file_log、越界权限全部继承；
- 脚本内自动放行只读（SAFE）工具；写/命令类工具要求本次运行本身就是
  完全访问权限，否则明确拒绝——避免「批一次审一层」变成「批一次审全部」；
- 结果预算：默认 2400 字符（超了掐头留尾加说明），模型按需再用 read_file
  取全文；不把大段原文重新灌回上下文正是本工具存在的意义。
"""
from __future__ import annotations

import contextvars
import io
import threading
import time
import traceback

from typing import Any

from .base import Tool, SAFE

# 当前运行的 agent 实例（loop.run 设置）。工具在 _execute 里同线程执行，
# contextvar 可见；脚本工作线程通过参数显式传递，不依赖 contextvar。
OPS_AGENT_CTX: contextvars.ContextVar[Any] = contextvars.ContextVar("ops_agent", default=None)

# 脚本内禁止调用的工具：ops_batch 嵌套（上下文混乱）、ask_user（脚本线程里
# 弹窗必死锁）。其余按上面的 SAFE/完全访问分级放行。
_DENIED_IN_SCRIPT = {"ops_batch", "ask_user"}

_DEFAULT_BUDGET = 2400
_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 180.0


def _budget_cut(text: str, budget: int) -> str:
    """超预算掐中段留头尾——开头是总览、结尾常是汇总，中间最可弃。"""
    if len(text) <= budget:
        return text
    head = int(budget * 0.6)
    tail = budget - head
    omitted = len(text) - head - tail
    return (text[:head]
            + f"\n…（中段省略 {omitted} 字符，如需全文请用 read_file 或缩小脚本输出范围）…\n"
            + text[-tail:])


def build_ops_tools(guard, cfg) -> list[Tool]:
    class _OpsBatch(Tool):
        name = "ops_batch"
        risk = SAFE  # 由脚本内工具分级 + 上层审批控制，本壳本身无副作用
        description = (
            "程序化工具调用（PTC）：提交一段 Python 脚本，在本地沙箱里批量组合调用其他工具，"
            "只有 print 出来的最终结果返回给模型。适合：批量读文件并提炼、多轮检索汇总、"
            "跨文件比对——替代「逐个工具调用」的多轮往返，大幅节省 token。"
            "脚本环境：call(\"read_file\", path=\"a.py\") 返回工具结果字符串；"
            "（也可写作 tools.call(...)，两者等价）"
            "print(text) 是唯一回传通道（不 print 就拿不到）；可用循环/条件/正则等全部 Python。"
            "切勿在脚本里写 `import tools`——这会覆盖内置的 call 对象，导致调用失败。"
            "只读工具自动放行；写/命令类工具仅完全访问权限的运行可用；"
            "不能在脚本里再调 ops_batch / ask_user。"
            "【PTC 常驻】这是你调用任何工具的唯一通道：脚本内 call(\"工具名\", **参数) 可调用全部已注册工具，"
            "名字见系统提示里的「可编入脚本的工具」清单；不知道某工具的参数就先 find_tools 查。"
            "（等价写法 tools.call(\"工具名\", ...) 也可用，但别 import tools。）")
        parameters = {
            "type": "object",
            "properties": {
                "script": {"type": "string",
                           "description": "Python 脚本。call(工具名, **参数) 调工具（或 tools.call(...)），print() 输出结果"},
                "budget": {"type": "integer",
                           "description": f"返回结果最大字符数，默认 {_DEFAULT_BUDGET}"},
                "timeout": {"type": "integer",
                            "description": f"脚本超时秒数，默认 {_DEFAULT_TIMEOUT:.0f}，上限 {_MAX_TIMEOUT:.0f}"},
            },
            "required": ["script"],
        }

        def run(self, script: str = "", budget: int = _DEFAULT_BUDGET,
                timeout: float = _DEFAULT_TIMEOUT, **_) -> str:
            agent = OPS_AGENT_CTX.get()
            if agent is None:
                return "[错误] ops_batch 只能在 agent 运行中使用（找不到执行上下文）"
            if not (script or "").strip():
                return "[错误] ops_batch 需要 script 参数：一段调用 tools.call(...) 的 Python 代码"

            budget = max(400, min(int(budget or _DEFAULT_BUDGET), 16000))
            timeout = max(5.0, min(float(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
            full = bool(getattr(agent, "full_access", False))
            file_log = getattr(agent, "_file_log", None)
            deadline = time.monotonic() + timeout
            stats: list[str] = []
            writes_reports: list[dict] = []  # 脚本内写类工具调用结果，供 loop 做契约对账
            out = io.StringIO()
            box: dict = {"done": False}

            def _print(*a, **kw):
                # 线程安全的输出捕获：不用 redirect_stdout——它是进程级全局，
                # 脚本线程若死循环，主线程的 stdout 会被永久劫持（实测）。
                sep = kw.get("sep", " ")
                end = kw.get("end", "\n")
                out.write(sep.join(str(x) for x in a) + end)

            def sdk_call(name: str, **args) -> str:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"脚本超过 {timeout:.0f}s 预算，已中止")
                if name in _DENIED_IN_SCRIPT:
                    r = f"[已拒绝] 脚本内不能调用 {name}"
                    _print(r)
                    return r
                spec = agent.registry.get(name)
                if spec is None:
                    r = f"[错误] 未知工具 {name}，可用：{', '.join(agent.registry.names())[:150]}"
                    _print(r)
                    return r
                if spec.risk != SAFE and not full:
                    r = (f"[已拒绝] {name} 是{'写' if spec.risk == 'write' else '命令'}类工具，"
                         "脚本内只自动放行只读工具；写/命令请在脚本外单独调用。")
                    _print(r)
                    return r
                stats.append(name)
                r = agent._execute(name, args, True, file_log)
                # 写/命令类工具的结果（成功路径 or 引用核查拒绝等）必须回显：脚本没 print
                # 时模型也能看到，否则会以为没写而用相同脚本重试、被去重守卫拦死。
                # 只读工具不回显——它们通常内容很大，模型自己 print 即可，避免截断。
                if spec.risk != SAFE:
                    _print(f"↳ {name}: {str(r)[:800]}")
                    writes_reports.append({
                        "tool": name,
                        "path": str(args.get("path") or args.get("file_path") or ""),
                        "result": str(r)[:300],
                    })
                return r

            class _Tools:
                call = staticmethod(sdk_call)

            def _work():
                # 注入两个等价入口：裸函数 call(...) 为主（模型不会去 import call），
                # tools.call(...) 为别名。这样即便脚本里写了 `import tools`（把名字
                # tools 重新绑到真正的 agentforge.tools 模块），call(...) 仍是可用的，
                # 不会再出现 rerun18 那种 "module 'tools' has no attribute 'call'"。
                env = {"call": sdk_call, "tools": _Tools(), "print": _print}
                try:
                    exec(compile(script, "<ops_batch>", "exec"), env)  # noqa: S102
                except BaseException as e:  # noqa: BLE001 —— 脚本异常要回给模型，不是崩溃
                    tb = traceback.format_exc().strip().splitlines()
                    out.write(f"\n[脚本异常] {type(e).__name__}: {e}\n"
                              + "\n".join(tb[-4:]))
                finally:
                    box["done"] = True

            t = threading.Thread(target=_work, daemon=True, name="ops-batch")
            t.start()
            t.join(timeout + 2)
            if not box["done"]:
                return ("[错误] 脚本超时未结束（可能存在不含工具调用的死循环）。"
                        "已回传的部分输出如下，请缩小脚本范围重试：\n"
                        + _budget_cut(out.getvalue(), budget))

            summary = f"[ops_batch] {len(stats)} 次工具调用（" \
                      + "、".join(f"{n}×{stats.count(n)}" for n in dict.fromkeys(stats)) \
                      + "）\n" if stats else "[ops_batch] 0 次工具调用\n"
            # 把脚本内写类工具调用结果挂到 agent，供 loop 在 ops_batch 执行后做
            # 契约对账（PTC 路径下的收尾诚信核查，对应 T4 契约闸门下沉）。
            try:
                agent._ops_write_report = writes_reports
            except Exception:
                pass
            return _budget_cut(summary + out.getvalue().strip(), budget)

    return [_OpsBatch()]
