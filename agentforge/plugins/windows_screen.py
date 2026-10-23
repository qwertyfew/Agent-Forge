"""Windows-MCP 接入插件：让 AgentForge 知道「屏幕内容」。

底层复用 D:\\AI_develop\\project\\windows-MCP 里的 MCP 服务（stdio 传输，JSON-RPC 2.0）。
插件只做三件事：

1. 管好那个子进程（懒启动、断线重连、进程退出时清理）；
2. 把 MCP 的 content 数组翻译成 AgentForge 能吃的字符串（图片落盘成文件再给路径）；
3. 把最常用的几个动作包成扁平参数的工具，小模型不用猜嵌套结构。

环境变量 WINDOWS_MCP_HOME 可指向别的部署目录；WINDOWS_MCP_PYTHON 可直接指定解释器。
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from agentforge.tools.base import EXEC, SAFE, make_tool

DEFAULT_HOME = Path(r"D:\AI_develop\project\windows-MCP")
PROTOCOL_VERSION = "2025-06-18"


def _home() -> Path:
    return Path(os.environ.get("WINDOWS_MCP_HOME") or DEFAULT_HOME)


class McpStdioClient:
    """一个够用的 MCP stdio 客户端（同步版本）。

    只实现 initialize / notifications/initialized / tools/list / tools/call，
    对本项目来说够用，也不引入额外依赖。
    """

    def __init__(self, home: Path | None = None, timeout: float = 180.0) -> None:
        self.home = home or _home()
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._id = 0
        self._ready = False

    # ---------- 进程管理 ----------

    def _python(self) -> str:
        override = os.environ.get("WINDOWS_MCP_PYTHON")
        if override and Path(override).exists():
            return override
        cand = self.home / ".venv" / "Scripts" / "python.exe"
        if cand.exists():
            return str(cand)
        return sys.executable

    def _spawn(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        exe = self._python()
        if not Path(exe).exists():
            raise RuntimeError(f"找不到 Windows-MCP 的解释器：{exe}")
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        self._proc = subprocess.Popen(
            [exe, "-m", "windows_mcp", "serve"],
            cwd=str(self.home),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            bufsize=0,
        )
        self._q = queue.Queue()

        def _reader(proc=self._proc, q=self._q) -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    q.put(line)
            q.put(None)  # 进程结束

        threading.Thread(target=_reader, name="wmc-reader", daemon=True).start()
        self._ready = False

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._ready = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:  # noqa: BLE001
            pass

    # ---------- JSON-RPC ----------

    def _write(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _read_until(self, want_id: int, timeout: float) -> dict:
        deadline = timeout
        while True:
            try:
                line = self._q.get(timeout=deadline)
            except queue.Empty:
                raise TimeoutError(f"Windows-MCP 在 {timeout:.0f}s 内没有响应") from None
            if line is None:
                raise RuntimeError("Windows-MCP 进程已退出")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 的输出（少数库会往 stdout 打日志）直接跳过
            if msg.get("id") != want_id:
                continue  # 通知 / 别人的响应
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
            return msg.get("result") or {}

    def _call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            for attempt in (0, 1):
                try:
                    self._spawn()
                    if not self._ready:
                        self._handshake()
                    rid = self._next_id()
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "method": method,
                            "params": params or {},
                        }
                    )
                    return self._read_until(rid, timeout)
                except (RuntimeError, TimeoutError, OSError):
                    # 第一次失败就当进程坏了，重来一次（只重试一次，避免死循环）
                    self._kill()
                    if attempt == 1:
                        raise
        raise RuntimeError("unreachable")

    def _handshake(self) -> None:
        rid = self._next_id()
        self._write(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "agentforge", "version": "1.0"},
                },
            }
        )
        self._read_until(rid, timeout=90)
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._ready = True

    # ---------- 对外 ----------

    def list_tools(self) -> list[dict]:
        return self._call("tools/list").get("tools", [])

    def call(self, name: str, args: dict | None = None, timeout: float | None = None) -> tuple[str, bool]:
        result = self._call("tools/call", {"name": name, "arguments": args or {}}, timeout)
        text = _render(result, name)
        return text, bool(result.get("isError"))


_client: McpStdioClient | None = None
_client_lock = threading.Lock()


def client() -> McpStdioClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = McpStdioClient()
        return _client


def _shot_dir() -> Path:
    d = Path(os.environ.get("WINDOWS_MCP_SHOTS") or (Path.home() / ".agentforge" / "screens"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unwrap(text: str) -> str:
    """部分工具（如 Snapshot）返回的是 list[str]，MCP 会把它序列化成一段 JSON 字符串。

    直接丢给模型会看到一堆 \\n 和引号，这里还原成普通多行文本。
    """
    s = text.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            v = json.loads(s)
        except json.JSONDecodeError:
            return text
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return "\n".join(v)
    return text


def _render(result: dict, tool: str) -> str:
    """把 MCP 的 content 数组压成纯文本；图片存成 png 再把路径交出去。"""
    blocks = result.get("content") or []
    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        kind = b.get("type")
        if kind == "text":
            parts.append(_unwrap(b.get("text", "")))
        elif kind == "image":
            try:
                raw = base64.b64decode(b.get("data", ""))
                ext = (b.get("mimeType") or "image/png").split("/")[-1]
                path = _shot_dir() / f"{tool.lower()}-{os.getpid()}-{threading.get_ident()}-{len(os.listdir(_shot_dir())) + 1}.{ext}"
                path.write_bytes(raw)
                parts.append(f"[图片已保存：{path}（{len(raw) // 1024} KB）]")
            except Exception as e:  # noqa: BLE001
                parts.append(f"[图片解码失败：{type(e).__name__}: {e}]")
        else:
            parts.append(json.dumps(b, ensure_ascii=False)[:2000])
    if not parts:
        return "（工具没有返回内容）"
    return "\n".join(parts)


def _fmt(text: str, maxlen: int = 12000) -> str:
    text = text.strip()
    if len(text) > maxlen:
        return text[:maxlen] + f"\n…（已截断，原始长度 {len(text)} 字符）"
    return text


# ---------------- 工具定义 ----------------

_SCREEN_HINT = (
    "用于「看屏幕」。当需要知道用户当前屏幕上有什么窗口、什么按钮、什么文字，"
    "或者需要操作 GUI（点击、输入、快捷键）时使用。"
)


def _screen_state(use_dom: bool = False, region: str = "", display: str = "") -> str:
    args: dict = {"use_vision": False, "use_ui_tree": True}
    if use_dom:
        args["use_dom"] = True
    if region:
        try:
            args["region"] = json.loads(region) if isinstance(region, str) else region
        except json.JSONDecodeError:
            return "region 需要写成 JSON 数组，例如 [0,0,800,600]。"
    if display:
        try:
            args["display"] = json.loads(display) if isinstance(display, str) else display
        except json.JSONDecodeError:
            return "display 需要写成 JSON 数组，例如 [0]。"
    text, is_err = client().call("Snapshot", args)
    return _fmt(text)


def _screen_screenshot(region: str = "") -> str:
    args: dict = {}
    if region:
        try:
            args["region"] = json.loads(region)
        except json.JSONDecodeError:
            return "region 需要写成 JSON 数组，例如 [0,0,800,600]。"
    text, is_err = client().call("Screenshot", args)
    return _fmt(text)


def _screen_click(x: int, y: int, clicks: int = 1, button: str = "left") -> str:
    text, _ = client().call(
        "Click", {"loc": [int(x), int(y)], "clicks": int(clicks), "button": button}
    )
    return _fmt(text)


def _screen_type(text: str, x: int | None = None, y: int | None = None,
                clear: bool = False, press_enter: bool = False) -> str:
    args: dict = {"text": text, "clear": bool(clear), "press_enter": bool(press_enter)}
    if x is not None and y is not None:
        args["loc"] = [int(x), int(y)]
    out, _ = client().call("Type", args)
    return _fmt(out)


def _screen_shortcut(shortcut: str) -> str:
    out, _ = client().call("Shortcut", {"shortcut": shortcut})
    return _fmt(out)


def _screen_windows_list() -> str:
    # Snapshot 关掉 UI 树提取就是「窗口清单 + 光标位置」，比整棵树便宜得多
    out, _ = client().call("Snapshot", {"use_ui_tree": False, "use_vision": False})
    return _fmt(out, maxlen=6000)


def _screen_tool_raw(name: str, arguments: str = "{}") -> str:
    try:
        args = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except json.JSONDecodeError as e:
        return f"arguments 不是合法 JSON：{e}"
    available = [t.get("name") for t in client().list_tools()]
    if name not in available:
        return f"没有这个工具：{name}。可用的有：{', '.join(map(str, available))}"
    out, is_err = client().call(name, args)
    return ("[工具报错] " if is_err else "") + _fmt(out)


def build_tools(guard, cfg):  # noqa: ARG001 - 插件签名固定
    tools = [
        make_tool(
            name="screen_state",
            description=(
                f"{_SCREEN_HINT}返回当前桌面状态：焦点窗口、所有窗口、以及可交互元素的无障碍树"
                "（按钮/输入框/菜单，带坐标和编号）。这是最省 token 也最准的「看屏幕」方式，"
                "先调它再决定点哪里。浏览器页面可加 use_dom=true 拿到网页元素。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "use_dom": {
                        "type": "boolean",
                        "description": "只看浏览器网页内容（过滤掉浏览器自身 UI），默认 false",
                    },
                    "region": {
                        "type": "string",
                        "description": "只截某块区域，JSON 数组 [left,top,right,bottom]，默认整个屏幕",
                    },
                    "display": {
                        "type": "string",
                        "description": "只截某个显示器，JSON 数组如 [0]，默认全部",
                    },
                },
                "required": [],
            },
            fn=_screen_state,
            risk=SAFE,
        ),
        make_tool(
            name="screen_screenshot",
            description=(
                f"{_SCREEN_HINT}抓一张真实截图为 PNG 文件，返回文件路径。"
                "无障碍树读不到内容时（游戏、Canvas、自定义绘制的界面）才用它。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": "只截某块区域，JSON 数组 [left,top,right,bottom]",
                    }
                },
                "required": [],
            },
            fn=_screen_screenshot,
            risk=SAFE,
        ),
        make_tool(
            name="screen_click",
            description="在屏幕绝对坐标处点击鼠标（坐标取自 screen_state 的元素 (x,y)）。",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标"},
                    "y": {"type": "integer", "description": "纵坐标"},
                    "clicks": {"type": "integer", "description": "1=单击，2=双击，0=只悬停"},
                    "button": {"type": "string", "description": "left / right / middle"},
                },
                "required": ["x", "y"],
            },
            fn=_screen_click,
            risk=EXEC,
        ),
        make_tool(
            name="screen_type",
            description="输入文字。给了 x,y 就先点过去再输入；press_enter=true 输入完直接回车提交。",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文字"},
                    "x": {"type": "integer", "description": "可选，输入前点击的横坐标"},
                    "y": {"type": "integer", "description": "可选，输入前点击的纵坐标"},
                    "clear": {"type": "boolean", "description": "是否先清空原有内容"},
                    "press_enter": {"type": "boolean", "description": "输入后是否回车"},
                },
                "required": ["text"],
            },
            fn=_screen_type,
            risk=EXEC,
        ),
        make_tool(
            name="screen_shortcut",
            description='发送快捷键，用 + 连接，例如 "ctrl+c"、"alt+tab"、"win+r"、"win"、"ctrl+shift+esc"。',
            parameters={
                "type": "object",
                "properties": {"shortcut": {"type": "string", "description": "快捷键组合"}},
                "required": ["shortcut"],
            },
            fn=_screen_shortcut,
            risk=EXEC,
        ),
        make_tool(
            name="screen_windows",
            description="列出当前打开的窗口与可启动的应用（看屏幕内容的轻量替代，不返回元素树）。",
            parameters={"type": "object", "properties": {}, "required": []},
            fn=_screen_windows_list,
            risk=SAFE,
        ),
        make_tool(
            name="screen_tool",
            description=(
                "Windows-MCP 的通用出口：直接调用它任意一个原生工具。"
                "可用工具：Snapshot / Screenshot / Click / Type / Scroll / Move / Shortcut / Wait / WaitFor / "
                "Scrape / MultiSelect / MultiEdit / Clipboard / App / PowerShell / FileSystem / Process / "
                "Registry / Notification / DisplayInventory / ControlStatus。"
                "特殊参数（滚动、拖拽、等待某段文字出现、剪贴板读写）走这个。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "原生工具名，如 Scroll"},
                    "arguments": {"type": "string", "description": '参数 JSON 字符串，如 {"loc":[800,500],"amount":3}'},
                },
                "required": ["name"],
            },
            fn=_screen_tool_raw,
            risk=EXEC,
        ),
    ]
    return tools


@atexit.register
def _shutdown() -> None:
    if _client is not None:
        _client._kill()
