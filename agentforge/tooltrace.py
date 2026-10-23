"""从轨迹里读出「模型到底调了哪些工具」——含 PTC（ops_batch）脚本里的内层调用。

为什么单独一层：PTC 常驻后，顶层 `tool_calls` 只有一个 `ops_batch`，任何「按工具名
判定证据」的闸门都会把真实发生过的事读成没发生。rerun65 实测的两个后果：

- `sources.ran_local_inspection` → w5 明明 `read_file` 了三次，仍被判「整个任务没有
  调用过任何读取工作区的工具」，于是给一份已经写进 `reports/outline.md` 的答复盖了
  「未经核实」——**误伤**；
- `sources.ran_search` → 恒为 False，`needs_external_note` 的前置条件直接不成立，
  外部事实核查（「转述 · 未经核实」）**整条静默失效**，模型写了没核实的数字也没人管。

口径（与评测侧 `tools/bench_tasks.py` 完全一致，避免两套解析各说各话）：

1. 顶层名字照录；
2. `ops_batch` 优先读**工具层自己印的执行小结**（`[ops_batch] 2 次工具调用（a×1、b×2）`）
   ——它是执行后统计的，最权威；
3. 没有小结（历史被压缩 / 旧记录 / 脚本零调用）时退回解析脚本源码，四种合法写法都认：
   `call("x")`、`tools.call("x")`、`from tools import x`、`import tools; tools.x(...)`。
"""
from __future__ import annotations

import re

_CALL_RX = re.compile(r"""(?:tools\s*\.\s*)?call\(\s*['"]([a-z_][a-z0-9_]*)['"]""")
_IMPORT_RX = re.compile(r"^\s*from\s+tools\s+import\s+([^\n#]+)", re.M)
_MODULE_RX = re.compile(r"\btools\.(?!call\b)([a-z_][a-z0-9_]*)\s*\(")
_SUMMARY_RX = re.compile(r"\[ops_batch\]\s*\d+\s*次工具调用（([^）]+)）")


def names_from_script(script: str) -> list[str]:
    """解析 ops_batch 脚本源码里的内层工具调用名（去重保序）。"""
    text = str(script or "")
    names = _CALL_RX.findall(text)
    for block in _IMPORT_RX.findall(text):
        for part in block.replace("(", " ").replace(")", " ").split(","):
            fn = part.strip().split(" as ")[0].strip()
            if re.fullmatch(r"[a-z_][a-z0-9_]*", fn or ""):
                names.append(fn)
    names += _MODULE_RX.findall(text)
    return list(dict.fromkeys(names))


def summary_names(output: str) -> list[str]:
    """从 ops_batch 的执行小结里取内层工具名（`a×1、b×2`）。"""
    m = _SUMMARY_RX.search(str(output or ""))
    if not m:
        return []
    out = [part.split("×")[0].strip() for part in m.group(1).split("、")]
    return list(dict.fromkeys(n for n in out if n))


def _call_args(tc: dict):
    fn = tc.get("function") or {}
    args = fn.get("arguments", tc.get("arguments"))
    if isinstance(args, dict):
        # 内部历史里 arguments 是 dict；脚本通常放在 script（也兼容 code）
        return args.get("script") or args.get("code") or ""
    return args or ""


def iter_names(convo: list[dict] | None):
    """按发生顺序产出工具名：顶层名 + ops_batch 的内层名。"""
    pending: list[tuple[str, str]] = []       # (name, script)
    for m in convo or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in (m.get("tool_calls") or []):
                name = ((tc.get("function") or {}).get("name")
                        or tc.get("name") or "")
                if not name:
                    continue
                pending.append((name, str(_call_args(tc) or "")))
                yield name
        elif role == "tool":
            name, script = pending.pop(0) if pending else ("", "")
            if name != "ops_batch":
                continue
            inner = summary_names(str(m.get("content") or ""))
            # 小结说「0 次工具调用」时不能退回解析脚本——那是脚本没执行成，
            # 源码里的名字不代表真调用过（例如整段被包进三引号）。
            if not inner and "[ops_batch] 0 次工具调用" not in str(m.get("content") or ""):
                inner = names_from_script(script)
            for n in inner:
                yield n


def tool_names(convo: list[dict] | None) -> list[str]:
    """轨迹里出现过的工具名（去重保序），含 PTC 内层调用。"""
    return list(dict.fromkeys(n for n in iter_names(convo) if n))


def called_any(convo: list[dict] | None, wanted) -> bool:
    want = set(wanted or ())
    return bool(want & set(tool_names(convo)))


__all__ = ["names_from_script", "summary_names", "iter_names", "tool_names", "called_any"]
