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

import builtins
import contextvars
import io
import threading
import time
import traceback
import types

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
            "脚本里可写 `from tools import read_file`、`tools.read_file(...)`，都等价于 call(...)；"
            "推荐直接用 call(...)。"
            "只读工具自动放行；写/命令类工具仅完全访问权限的运行可用；"
            "不能在脚本里再调 ops_batch / ask_user。"
            "【PTC 常驻】这是你调用任何工具的唯一通道：脚本内 call(\"工具名\", **参数) 可调用全部已注册工具，"
            "名字见系统提示里的「可编入脚本的工具」清单；不知道某工具的参数就先 find_tools 查。"
            "（等价写法 tools.call(...) / `from tools import X` 也可用。）")
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
            # 写类工具的系统回显单独收集，最后统一附在脚本输出**之后**。
            # 为什么要压末尾：rerun23 实测模型在脚本末尾写了无条件的
            # print("Write successful.")，而 write_file 实际被引用核查拒绝。
            # 我们的 ⇳ 回显排在中间，模型只看到自己那句「成功」就宣布完成，
            # 交付物根本没落盘，最终答复还连说 7 次「已写入」。
            # 权威结论必须排在模型自述之后，并用【系统核对】标记区分。
            echo_lines: list[str] = []
            out = io.StringIO()
            box: dict = {"done": False}

            def _print(*a, **kw):
                # 线程安全的输出捕获：不用 redirect_stdout——它是进程级全局，
                # 脚本线程若死循环，主线程的 stdout 会被永久劫持（实测）。
                sep = kw.get("sep", " ")
                end = kw.get("end", "\n")
                out.write(sep.join(str(x) for x in a) + end)

            def _required_params(tool_name: str) -> list[str]:
                """该工具的必填参数名（按声明顺序）——位置参数逐个映射到它们。

                各工具主参数不同（read_file→path、search_code→pattern、
                run_command→command），不能一律当 path。多必填的工具
                （write_file→path,content；edit_file→path,old_text,new_text）
                按声明顺序对齐，这是模型写 call("write_file", path, body) 时的
                自然预期。
                """
                spec = agent.registry.get(tool_name)
                params = getattr(spec, "parameters", None) or {}
                req = [str(x) for x in (params.get("required") or [])]
                if req:
                    return req
                props = params.get("properties") or {}
                return [next(iter(props), "path")]

            def _with_positional(name: str, pos: tuple, kw: dict) -> dict:
                # 容忍位置参数：模型爱写 call("list_dir", "external") 或
                # tools.list_dir("external")。旧实现直接抛「只支持关键字参数」，
                # rerun23 / rerun24 各白烧一轮。纯接口摩擦，壳该迁就调用习惯。
                # rerun27 又栽一次：write_file(path, content) 两个位置参数被
                # 「最多 1 个位置参数」挡掉，白烧一整轮。按必填顺序逐个对齐。
                if not pos:
                    return kw
                req = _required_params(name)
                if len(pos) > len(req):
                    raise TypeError(
                        f"{name}() 最多 {len(req)} 个位置参数（依次映射到 "
                        f"{'、'.join(req)}），其余请用关键字："
                        f"call(\"{name}\", {req[0]}=..., ...)")
                out = dict(kw)
                for key, val in zip(req, pos):
                    if key in out:
                        raise TypeError(f"{name}() 参数重复：{key} 既按位置又按关键字给了")
                    out[key] = val
                return out

            def sdk_call(name: str, *pos, **args) -> str:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"脚本超过 {timeout:.0f}s 预算，已中止")
                args = _with_positional(name, pos, args)
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
                    _txt = str(r)
                    _ok = not (_txt.lstrip().startswith("[错误]")
                               or "未写入" in _txt or _txt.lstrip().startswith("[已拒绝]"))
                    echo_lines.append(f"↳ {name}: {_txt[:800]}")
                    writes_reports.append({
                        "tool": name,
                        "path": str(args.get("path") or args.get("file_path") or ""),
                        "result": _txt[:300],
                        "ok": _ok,
                    })
                return r

            def _make_tools_module():
                """把 `tools` 做成真正的模块对象（PEP 562 模块级 __getattr__）。

                这样 `tools.read_file(...)`、`from tools import read_file` 都直接落到
                sdk_call；rerun22 实测模型写了 6 次 `from tools import list_dir`，
                每次都 ImportError（命中的是仓库根目录那个没有 __init__ 的 tools/
                测试包），白烧 6 轮。壳补上 __getattr__ 后这类写法不再是死路。
                """
                mod = types.ModuleType("tools")
                mod.__doc__ = ("ops_batch 脚本内的工具壳：任何属性都当工具名，"
                               "如 tools.read_file(path=...)。")
                mod.call = sdk_call  # type: ignore[attr-defined]

                def _module_getattr(attr: str):
                    # 魔法属性按常规抛 AttributeError，别让 import 机制/调试工具误判。
                    if attr.startswith("__") and attr.endswith("__"):
                        raise AttributeError(attr)

                    def _bound(*a, **kw):
                        # 位置参数由 sdk_call 统一映射到该工具的主参数。
                        return sdk_call(attr, *a, **kw)

                    _bound.__name__ = attr
                    _bound.__doc__ = f"脚本内调用工具 {attr}（等价 call('{attr}', ...)）"
                    return _bound

                mod.__getattr__ = _module_getattr  # type: ignore[attr-defined]
                return mod

            def _work():
                # 两个等价入口：裸函数 call(...) 为主（模型不爱写 import），
                # tools.call(...) / tools.<工具名>(...) 为别名。
                tools_mod = _make_tools_module()
                # `import tools` 命中的必须是这个壳，而不是仓库根的真实 tools/ 包。
                # 只替换本脚本 builtins 里的 __import__，不写 sys.modules——写全局会把
                # 其他线程/模块的 import 一起劫持，属于进程级污染。
                script_builtins = dict(vars(builtins))
                _orig_import = builtins.__import__

                def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                    if level == 0 and (name == "tools" or name.startswith("tools.")):
                        return tools_mod
                    return _orig_import(name, globals, locals, fromlist, level)

                script_builtins["__import__"] = _guarded_import
                env = {"call": sdk_call, "tools": tools_mod, "print": _print,
                       "__builtins__": script_builtins}
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
            # 权威块：永远排在脚本自身输出之后，且明说是系统核对而非脚本 print。
            tail = ""
            if writes_reports:
                fails = [w for w in writes_reports if not w.get("ok")]
                tail = "\n\n【系统核对·写盘结果】以下由 harness 实测得出，不是脚本里的 print：\n"
                for w in writes_reports:
                    tail += (f"  - {w['tool']} → {w['path'] or '(未给 path)'}: "
                             f"{'已写入磁盘' if w.get('ok') else '写入失败'}\n")
                if fails:
                    tail += ("  ⚠ 存在写盘失败。脚本里任何「已写入 / 成功 / write successful」"
                             "都只是你自己的 print，不代表文件存在。请按上面的错误原因修正后重写。\n")
                else:
                    tail += "  ✓ 以上文件已确认存在于磁盘。\n"
            if echo_lines:
                tail += "\n" + "\n".join(echo_lines) + "\n"
            return _budget_cut(summary + out.getvalue().strip() + tail, budget)

    return [_OpsBatch()]
