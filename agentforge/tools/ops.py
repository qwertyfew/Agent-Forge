# -*- coding: utf-8 -*-
"""ops_batch：程序化工具调用（PTC，Programmatic Tool Calling）。

token 经济诊断（docs/changes/token-economy-20261019.md）的 T1：一次分析往往要
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
import os
import re
import threading
import time
import traceback
import types

from pathlib import Path

from typing import Any

from ..tooltrace import names_from_script
from ..permissions import OUTDIR_CTX
from .base import Tool, SAFE, WRITE

# 当前运行的 agent 实例（loop.run 设置）。工具在 _execute 里同线程执行，
# contextvar 可见；脚本工作线程通过参数显式传递，不依赖 contextvar。
OPS_AGENT_CTX: contextvars.ContextVar[Any] = contextvars.ContextVar("ops_agent", default=None)

# 脚本内禁止调用的工具：ops_batch 嵌套（上下文混乱）、ask_user（脚本线程里
# 弹窗必死锁）。其余按上面的 SAFE/完全访问分级放行。
_DENIED_IN_SCRIPT = {"ops_batch", "ask_user"}

# P29-M2：run_command 回执里的退出码标记（[exit] N），用于命令报告判成败。
_EXIT_RX = re.compile(r"\[exit\]\s*(-?\d+)")

# 参数名别名表（只收**纯改名**，见 sdk_call 里的 _fix_kwargs）。
# 这些写法在各种编辑/文件 API 里都出现过（str_replace、apply_patch、各家 SDK），
# 模型照先验写出来是必然的；壳替它改一个名字，省一整轮。
_KW_RENAMES = {
    "old_string": "old_text", "new_string": "new_text",
    "old_str": "old_text", "new_str": "new_text",
    "old_content": "old_text", "new_content": "new_text",
    "file_path": "path", "filepath": "path", "filename": "path",
}

# 脚本内禁止直连网络（P5：soak04 / soak08 的 h4）。工具层取数会带「[实测 · API]」
# 来源标记，闸门的引用核对全靠它；脚本自己 requests 拿到的东西没有标记，
# 证据链在这里**静默断开**（表面看一切正常）。禁 import 比事后检测彻底。
_NET_MODULES = frozenset({
    "socket", "ssl", "requests", "urllib", "urllib3", "http", "httpx",
    "aiohttp", "ftplib", "telnetlib", "smtplib", "poplib", "imaplib",
    "xmlrpc", "websocket", "websockets", "pycurl",
})
_NET_BLOCK_MSG = (
    "[脚本拦截] 脚本内禁止直连网络（import {name}）。要取网络数据请用 "
    "fetch_url / fetch_json / web_search / github_search：它们走工具层，"
    "结果带「[实测]」来源标记；直连拿到的数据没有标记，等于没核实过。"
)

_DEFAULT_BUDGET = 2400
_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 180.0

# 模型控制标记：<|channel|> / <|im_start|> / <channel|> / <|channel> 等。
# rerun56 实证：模型把整段「思考通道」连同这些标记一起写进了 script 文本——
# `<|channel>thought ... <channel|>call("web_search", ...)`，编译直接
# `SyntaxError: unterminated string literal`，第一次 ops_batch 因此 0 次工具调用。
# 不是它不会写脚本，是脚本文本里混进了不该出现的控制标记 + 思考散文。
_CTRL_TOKEN_RX = re.compile(
    r"<\|[^|>\n]{0,40}\|>"           # <|channel|> / <|im_start|>
    r"|<\|[^|>\n]{0,40}>"            # <|channel>（缺右侧竖线）
    r"|<[A-Za-z_][\w:.-]{0,20}\|>"   # <channel|>
)


def _compiles(src: str) -> bool:
    try:
        compile(src, "<ops_batch>", "exec")
        return True
    except SyntaxError:
        return False


def _looks_like_code(line: str) -> bool:
    """这一行像不像 Python 代码（用来定位混入的思考散文边界）。

    只看「结构信号」不看语义：关键字开头、缩进续行、赋值、函数调用。
    注意 `thought` 这种裸词是合法表达式语句（能编译、运行时才 NameError），
    所以不能拿「能不能编译」当唯一判据——必须先把散文行认出来。
    """
    s = line.strip()
    if not s:
        return False
    if line[:1] in (" ", "\t"):        # 缩进续行 / 块体内容
        return True
    if s.startswith("#"):
        return True
    if re.match(r"^(import|from|def|class|for|while|if|elif|else|try|except"
                r"|finally|with|return|yield|assert|del|global|nonlocal"
                r"|lambda|async|await|@|pass|break|continue|raise)\b", s):
        return True
    if re.match(r"^[A-Za-z_]\w*\s*(=|\+=|-=|\*=|/=|\()", s):
        return True
    if re.match(r"^[A-Za-z_][\w.]*\s*\(", s):   # call(...) / tools.call(...) / print(...)
        return True
    return False


def _has_code(inner: str) -> bool:
    """文本里是否含真正的代码结构（import / 函数调用）。"""
    return bool(re.search(r"^\s*(?:import|from)\s+\w", inner, re.M)
                or re.search(r"^\s*[A-Za-z_][\w.]*\s*\(", inner, re.M))


_TRIPLE_WRAP_RX = re.compile(r'^\s*("""|\'\'\')(.*)\1\s*$', re.S)

# P24：脚本线程 chdir 到工作区根时的进程级互斥（cwd 是全局状态）。
_CWD_LOCK = threading.Lock()


def _unwrap_triple_quoted(raw: str) -> str | None:
    """整段脚本被包成三引号字符串时，剥出内部代码。

    rerun57 实证：模型把脚本写成 `\"\"\"\\nfrom tools import ...\\n\"\"\"`——这在语法上
    完全合法（一个字符串字面量），编译通过、执行 **0 次工具调用**，于是它以为成功、
    连续重试 19 次都没察觉。这类「静默空转」编译检查抓不到，必须专门识别。
    """
    m = _TRIPLE_WRAP_RX.match(raw or "")
    if not m:
        return None
    inner = m.group(2)
    if not _has_code(inner):
        return None
    return inner


def sanitize_script(script: str) -> tuple[str, str]:
    """剥掉混进脚本里的控制标记/思考文本/外层字符串，返回 (可用脚本, 处理说明)。

    只在「原样不能正常执行」时才动手——能正常跑的脚本一个字都不改。
      - rerun56：`<|channel>thought` + 推理散文 + `<channel|>call(...)` 混进来 →
        编译 SyntaxError，第一次 ops_batch 因此 0 次调用。
      - rerun57：整段代码被包在 `\"\"\"...\"\"\"` 里 → 编译通过但零执行（静默空转）。
    """
    raw = script or ""
    if not raw.strip():
        return raw, ""
    # P29-M1：抢救产物必须保全源码里的全部工具调用。c5-fib n1064 实证：模型在
    # content="..." 字符串里写了裸换行（语法非法），逐行抢救只留下末行
    # run_command、把 write_file 静默丢掉——模型随后被告知「已写入磁盘」，
    # 连撒 6 轮谎。宁可原样返回让它 SyntaxError（回执有 traceback），也不能
    # 带着丢失的调用「修好」。
    raw_calls = set(names_from_script(raw))
    unwrapped = _unwrap_triple_quoted(raw)
    if unwrapped is not None and _compiles(unwrapped):
        return unwrapped, ("已剥掉包在脚本外层的三引号字符串"
                           "（原写法整段是一个字符串字面量，什么都不会执行）")
    if _compiles(raw):
        return raw, ""
    cleaned = _CTRL_TOKEN_RX.sub("", raw)
    if cleaned != raw and _compiles(cleaned) and set(names_from_script(cleaned)) >= raw_calls:
        return cleaned, "已剥离模型控制标记（如 <|channel|>）"
    lines = cleaned.splitlines()
    # 从「第一行真正的代码」起截，并剥掉尾部散落的思考行
    for i, ln in enumerate(lines):
        if not _looks_like_code(ln):
            continue
        block = list(lines[i:])
        while block and block[-1].strip() and not _looks_like_code(block[-1]):
            block.pop()
        cand = "\n".join(block)
        if (cand.strip() and _compiles(cand)
                and set(names_from_script(cand)) >= raw_calls):
            return cand, "已剥离控制标记与混入的思考文本"
    return raw, ""


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
            "find_files / search_code 支持 fmt=\"json\"（返回机器可读 JSON），"
            "脚本里优先用它并 json.loads——别用字符串解析散文回执。"
            "脚本内 read_file 自动返回**原始文本**（无行号装饰），可直接喂 csv/json 解析或计数——"
            "要统计行数、排序、解析结构化数据就用它，别再用展示格式。"
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
            # contextvars 不跨线程：脚本线程看不到 OUTDIR_CTX，这里先取快照
            # （P28：脚本 cwd 要落交付区 output/，靠的就是这个值）
            outdir_snap = OUTDIR_CTX.get()

            # 脚本预处理：把混进来的控制标记/思考文本剥掉（rerun56 首次调用因
            # `<|channel>thought` 混入脚本而 SyntaxError，白烧一整轮）。
            script, _fix_note = sanitize_script(script)

            budget = max(400, min(int(budget or _DEFAULT_BUDGET), 16000))
            timeout = max(5.0, min(float(timeout or _DEFAULT_TIMEOUT), _MAX_TIMEOUT))
            full = bool(getattr(agent, "full_access", False))
            file_log = getattr(agent, "_file_log", None)
            deadline = time.monotonic() + timeout
            stats: list[str] = []
            writes_reports: list[dict] = []  # 脚本内写类工具调用结果，供 loop 做契约对账
            cmd_reports: list[dict] = []     # P29-M2：命令类调用单独记账，永不冒充写盘
            # 写类工具的系统回显单独收集，最后统一附在脚本输出**之后**。
            # 为什么要压末尾：rerun23 实测模型在脚本末尾写了无条件的
            # print("Write successful.")，而 write_file 实际被引用核查拒绝。
            # 我们的 ⇳ 回显排在中间，模型只看到自己那句「成功」就宣布完成，
            # 交付物根本没落盘，最终答复还连说 7 次「已写入」。
            # 权威结论必须排在模型自述之后，并用【系统核对】标记区分。
            echo_lines: list[str] = []
            out = io.StringIO()
            if _fix_note:
                # 明确告诉模型「你刚才提交的不是纯代码」，否则它会以为语法错在自己逻辑上。
                out.write(f"[脚本预处理] {_fix_note}。请只提交纯 Python 代码，"
                          f"不要带思考文本或 <|channel|> 这类通道标记。\n")
            box: dict = {"done": False}
            # 源码里写了哪些内层调用（只用于「零执行」时的归因，P11）：
            # 写了却没执行到 ≠ 压根没写，两种错法要给的提示完全不同。
            _planned_calls = [n for n in names_from_script(script)
                              if n not in _DENIED_IN_SCRIPT]

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

            def _fix_kwargs(name: str, kw: dict) -> dict:
                """把**纯改名**的参数别名就地改成真名，并说明改了什么。

                为什么只做改名：`old_string` / `old_content` / `file_path` 这些写法
                和 `old_text` / `path` 表达的是同一件事，改过去行为不变——31 轮里
                3 次 `old_content=` 就是这么白烧了一整轮。而 `edit_file(content=)`
                明确**不映射**：`content` 是「整篇内容」的意思，硬塞给 `new_text`
                会把整篇文档当成一处替换，语义完全不同，只能报错讲清楚。
                """
                spec = agent.registry.get(name)
                props = set(((getattr(spec, "parameters", None) or {}).get("properties") or {}))
                if not props:
                    return kw
                fixed: dict[str, str] = {}
                for key in list(kw):
                    if key in props:
                        continue
                    good = _KW_RENAMES.get(key)
                    if good and good in props and good not in kw:
                        kw[good] = kw.pop(key)
                        fixed[key] = good
                if fixed:
                    _print("[参数] 已按同名参数处理："
                           + "、".join(f"{a} → {b}" for a, b in fixed.items()))
                return kw

            def sdk_call(name: str, *pos, **args) -> str:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"脚本超过 {timeout:.0f}s 预算，已中止")
                args = _with_positional(name, pos, args)
                args = _fix_kwargs(name, args)
                # P31：脚本内 read_file 自动降级 raw。脚本上下文是程序化处理，
                # 展示格式的头行+行号前缀会被模型/解析器当成内容（n3 归因：
                # f2 数出 8=1+7、a1 csv 解析 0.0、c7 把展示文本写盘）。顶层
                # 对话场景保留展示格式——引用核查需要行号。模型显式传 fmt 则尊重。
                if name == "read_file":
                    args.setdefault("fmt", "raw")
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
                    # 三态：落盘 / 跳过（内容与磁盘一致，幂等层没让它落盘）/ 失败。
                    # 以前只有两态，跳过的写被算成「已写入磁盘」，于是权威块一边说
                    # 写成功、一边在下一行说「未重复落盘」——自相矛盾，也让
                    # event_audit 把一次没发生的写计成重复覆盖（fix01 的 w5 实测）。
                    _skip = "已是最新内容" in _txt and "未重复落盘" in _txt
                    _ok = (not _skip) and not (
                        _txt.lstrip().startswith("[错误]")
                        or "未写入" in _txt or _txt.lstrip().startswith("[已拒绝]"))
                    echo_lines.append(f"↳ {name}: {_txt[:800]}")
                    if spec.risk == WRITE:
                        writes_reports.append({
                            "tool": name,
                            "path": str(args.get("path") or args.get("file_path") or ""),
                            "result": _txt[:300],
                            "ok": _ok,
                            "skipped": _skip,
                        })
                    else:
                        # P29-M2：命令类（run_command 等）不是写文件，绝不进「写盘」
                        # 报告。n1064/c5-fib 实证：exit=2 的失败命令被 _ok 误判成
                        # True，权威块印出「已写入磁盘 ✓ 已确认存在于磁盘」——模型
                        # 收到假权威信息后宣称成功。命令单独报 exit 状态。
                        _m = _EXIT_RX.search(_txt)
                        _cmd_ok = (int(_m.group(1)) == 0) if _m else _ok
                        cmd_reports.append({
                            "tool": name,
                            "result": _txt[:200],
                            "ok": _cmd_ok,
                            "exit": int(_m.group(1)) if _m else None,
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
                    head = str(name or "").split(".")[0]
                    if level == 0 and head in _NET_MODULES:
                        box.setdefault("blocked", []).append(str(name))
                        raise ImportError(_NET_BLOCK_MSG.format(name=name))
                    return _orig_import(name, globals, locals, fromlist, level)

                script_builtins["__import__"] = _guarded_import
                # `__name__` 必须显式给成 "__main__"：exec 的环境里查不到这个名字时
                # 会回落到 builtins 模块的字典（值是 "builtins"），于是模型写的
                # `if __name__ == "__main__": main()` 恒为假——脚本"成功"、零调用、
                # 零报错（P9，soak16 连丢两轮）。这是标准 Python 写法，不该被坑。
                env = {"call": sdk_call, "tools": tools_mod, "print": _print,
                       "__name__": "__main__",
                       "__builtins__": script_builtins}
                # P24：脚本内的裸 IO（open/os.listdir）走进程 cwd，而工具走沙盒
                # 边界——同一个脚本两套文件系统视角（p23a 的 f3：os.listdir()
                # 列出的是项目根，模型在项目根找不到沙盒里的种子文件）。P28 起
                # 脚本 cwd 落到交付区 output/（种子已拷入、交付物写在这），
                # 没有产出目录时退回工作区根。cwd 是进程级全局状态，用锁串行化
                # 并发脚本；锁获取带超时——死循环脚本线程永不结束也就永不还锁，
                # 后续脚本降级为「不切换」而不是一起卡死。
                ws_raw = getattr(cfg, "workspace_root", None)
                ws = Path(str(ws_raw)).resolve() if ws_raw else None
                # OUTDIR_CTX 不跨线程传播：在 run() 里（context 还活着时）先取好
                _outdir = outdir_snap
                if ws is not None and _outdir:
                    _out = ws / _outdir / "output"
                    if _out.is_dir():
                        ws = _out
                prev_cwd = os.getcwd()
                locked = False
                try:
                    if ws is not None and ws.is_dir():
                        locked = _CWD_LOCK.acquire(timeout=10)
                        if locked:
                            os.chdir(str(ws))
                            box["cwd_from"] = prev_cwd
                            out.write(f"[script cwd] {ws}\n")
                        else:
                            out.write("[script cwd] 未切换（另一脚本占用中），"
                                      "裸 IO 沿用进程 cwd\n")
                    exec(compile(script, "<ops_batch>", "exec"), env)  # noqa: S102
                except BaseException as e:  # noqa: BLE001 —— 脚本异常要回给模型，不是崩溃
                    box["exc"] = f"{type(e).__name__}: {e}"
                    tb = traceback.format_exc().strip().splitlines()
                    out.write(f"\n[脚本异常] {type(e).__name__}: {e}\n"
                              + "\n".join(tb[-4:]))
                finally:
                    if locked:
                        if os.getcwd() == str(ws):
                            os.chdir(prev_cwd)
                        _CWD_LOCK.release()
                    box["done"] = True

            t = threading.Thread(target=_work, daemon=True, name="ops-batch")
            t.start()
            t.join(timeout + 2)
            if not box["done"]:
                # 死循环脚本线程还活着、可能还占着 cwd——主线程兜底恢复，
                # 不让一个跑飞的脚本把整个进程的 cwd 拖走。
                cf = box.get("cwd_from")
                if cf:
                    try:
                        os.chdir(cf)
                    except OSError:
                        pass
                return ("[错误] 脚本超时未结束（可能存在不含工具调用的死循环）。"
                        "已回传的部分输出如下，请缩小脚本范围重试：\n"
                        + _budget_cut(out.getvalue(), budget))

            if stats:
                summary = f"[ops_batch] {len(stats)} 次工具调用（" \
                          + "、".join(f"{n}×{stats.count(n)}" for n in dict.fromkeys(stats)) \
                          + "）\n"
            elif box.get("exc"):
                # P11：三种「0 次工具调用」以前长一个样，处置完全不同。异常中断
                # 是最常见的（名字没导入、变量没赋值、被禁的 import），单独标出来，
                # 免得模型（和台账规则）把它跟「脚本被包成字符串」混为一谈。
                _exc = str(box["exc"])
                summary = (f"[ops_batch] 0 次工具调用 · 脚本异常中断"
                           f"（{_exc[:160]}）—— 上面的 traceback 就是从你脚本里"
                           f"抛出来的那一行，先改它再重试。本轮没有拿到任何数据。\n")
                # P29-M1b：c5-fib n1064 实证的最高频成因——长字符串参数里写裸换行。
                if "SyntaxError" in _exc and "unterminated" in _exc.lower():
                    summary += (
                        "最常见成因：call(...) 的长字符串参数（如 content=）里直接按了"
                        "回车。Python 字符串字面量不能跨行——要么整段写在一行并用 \\n "
                        "表示换行，要么改用三引号字符串。写文件调用因此一次都没执行，"
                        "文件并不存在。\n")
            elif _planned_calls:
                # 源码里明明写了 call(...)，却一次都没执行到：写在函数/if 分支里没人调，
                # 或者被 `if __name__ == "__main__"` 这类守卫挡住（本环境已保证
                # `__name__ == "__main__"`，但更稳的写法是摊平成顶层语句）。
                summary = ("[ops_batch] 0 次工具调用 · 脚本里写了 "
                           + "、".join(_planned_calls[:6])
                           + "，但一次都没执行到。多半是写在函数或 if 分支里而没有被调用，"
                             "或者整段被包成了字符串。请把调用写成**顶层、直接执行**的语句"
                             "后重试。本轮没有拿到任何数据。\n")
            else:
                # rerun57 实证：脚本被包成三引号字符串 → 编译通过、零执行，模型以为
                # 成功，连续重试 19 次。这里必须明确说「本轮没拿到任何数据 + 常见原因」，
                # 否则它会把这句「0 次」当成正常返回继续往下编。
                summary = (
                    "[ops_batch] 0 次工具调用 —— 脚本执行了，但里面没有任何一次 "
                    "tools.call(...) / web_search(...) / fetch_url(...) 真正被调用，"
                    "**本轮没有拿到任何数据**。\n"
                    "常见原因：① 整段脚本被包在 `\"\"\" ... \"\"\"` 或代码围栏里，"
                    "成了一个字符串（语法合法但什么都不执行）；② 通篇只有注释/说明文字；"
                    "③ 调用写在了未被执行的函数或 if 分支里；④ 工具名写错导致没有生效。\n"
                    "请把脚本改成可直接执行的语句后重试。\n")
            # 把脚本内写类工具调用结果挂到 agent，供 loop 在 ops_batch 执行后做
            # 契约对账（PTC 路径下的收尾诚信核查，对应 T4 契约闸门下沉）。
            try:
                agent._ops_write_report = writes_reports
            except Exception:
                pass
            # 权威块：永远排在脚本自身输出之后，且明说是系统核对而非脚本 print。
            tail = ""
            if writes_reports:
                fails = [w for w in writes_reports
                         if not w.get("ok") and not w.get("skipped")]
                tail = "\n\n【系统核对·写盘结果】以下由 harness 实测得出，不是脚本里的 print：\n"
                for w in writes_reports:
                    if w.get("skipped"):
                        state = "已存在未变（跳过，未重复落盘）"
                    elif w.get("ok"):
                        state = "已写入磁盘"
                    else:
                        state = "写入失败"
                    tail += (f"  - {w['tool']} → {w['path'] or '(未给 path)'}: "
                             f"{state}\n")
                if fails:
                    tail += ("  ⚠ 存在写盘失败。脚本里任何「已写入 / 成功 / write successful」"
                             "都只是你自己的 print，不代表文件存在。请按上面的错误原因修正后重写。\n")
                elif all(w.get("skipped") for w in writes_reports):
                    tail += ("  ✓ 文件本来就是这个内容（跳过的是重复写，不是失败）。"
                             "不要再写一遍，直接给结论。\n")
                else:
                    tail += "  ✓ 以上文件已确认存在于磁盘。\n"
            if cmd_reports:
                # P29-M2：命令类单独一块，只报执行状态，与「文件是否写入」脱钩。
                _cmd_fails = [c for c in cmd_reports if not c.get("ok")]
                tail += ("\n\n【系统核对·命令执行】以下由 harness 实测得出，不是脚本里的 print：\n")
                for c in cmd_reports:
                    _state = ("执行完成（exit=0）" if c.get("ok")
                              else f"执行失败（exit={c.get('exit')}）")
                    tail += f"  - {c['tool']}: {_state}\n"
                if _cmd_fails:
                    tail += ("  ⚠ 有命令执行失败：失败命令的输出不可作为任务完成的证据，"
                             "也不要把它当成文件已写入。需要落盘请用 "
                             "call('write_file', path=..., content=...)。\n")
            if echo_lines:
                tail += "\n" + "\n".join(echo_lines) + "\n"
            return _budget_cut(summary + out.getvalue().strip() + tail, budget)

    return [_OpsBatch()]
