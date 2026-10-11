from __future__ import annotations

import json
from typing import Any, Iterator

import requests


class ToolCall:
    def __init__(self, id: str, name: str, arguments: dict):
        self.id = id
        self.name = name
        self.arguments = arguments or {}

    def __repr__(self) -> str:  # pragma: no cover
        return f"ToolCall({self.name}, {self.arguments})"


class LLMResult:
    def __init__(self):
        self.content: str = ""
        self.thinking: str = ""
        self.tool_calls: list[ToolCall] = []
        self.eval_count: int = 0
        self.eval_duration_ns: int = 0
        self.prompt_eval_count: int = 0
        self.load_duration_ns: int = 0

    @property
    def tokens_per_second(self) -> float:
        if not self.eval_duration_ns:
            return 0.0
        return self.eval_count / (self.eval_duration_ns / 1e9)


def _merge_tool_calls(acc: dict[int, dict], incoming: list[dict]) -> None:
    """Ollama 流式返回里 tool_calls 可能分片到达，按 index 合并。"""
    for i, tc in enumerate(incoming or []):
        idx = tc.get("index", i)
        fn = tc.get("function", {}) or {}
        entry = acc.setdefault(idx, {"id": tc.get("id", ""), "name": "", "args": {}, "raw": ""})
        if tc.get("id"):
            entry["id"] = tc["id"]
        if fn.get("name"):
            if not entry["name"]:
                entry["name"] = fn["name"]
            elif not entry["name"].startswith(fn["name"]):
                entry["name"] += fn["name"]
        args = fn.get("arguments")
        if isinstance(args, dict):
            entry["args"].update(args)
        elif isinstance(args, str):
            entry["raw"] += args


def _finalize_tool_calls(acc: dict[int, dict]) -> list[ToolCall]:
    out: list[ToolCall] = []
    for idx in sorted(acc):
        e = acc[idx]
        args = dict(e["args"])
        if e["raw"].strip():
            try:
                parsed = json.loads(e["raw"])
                if isinstance(parsed, dict):
                    args.update(parsed)
            except json.JSONDecodeError:
                pass
        out.append(ToolCall(e["id"] or f"call_{idx}", e["name"], args))
    return out


def _tidy_str(s: str) -> str:
    """修正常见的重复分隔符：D:////a////b -> D:/a/b"""
    import re

    s = re.sub(r"/{3,}", "/", str(s))
    s = re.sub(r"\\{3,}", lambda m: "\\", s)
    return s


def parse_text_tool_calls(text: str, valid_names: list[str] | None = None) -> list[ToolCall]:
    """从正文里抢救被写成文本的工具调用。

    并非所有模型都会走 Ollama 的 tool_calls 字段。实测 qwen2.5-coder:14b 会把
    调用直接写成正文 JSON，例如：
        {"name": "list_dir", "arguments": {"path": "D:/project"}}
    若不解析，Agent 就只会把这段 JSON 当成最终答案念给用户，工具永远不会执行。

    valid_names 传入已知工具名白名单，避免把普通回答里的 JSON 误判成调用。
    """
    import re

    if not text or not text.strip():
        return []

    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = blocks + re.findall(r"(\{(?:[^{}]|\{[^{}]*\})*\})", text, re.S)

    out: list[ToolCall] = []
    seen: set[tuple] = set()
    for raw in cands:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        if not name:
            continue
        name = str(name).strip()
        if valid_names and name not in valid_names:
            continue
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        args = {k: (_tidy_str(v) if isinstance(v, str) else v) for k, v in args.items()}
        key = (name, json.dumps(args, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(ToolCall(f"text_{len(out)}", name, args))
    return out


class ThinkFilter:
    """剥离模型混在正文里的 <think>...</think> 片段。

    有些模型（或关闭 think 开关时）会把推理过程以 <think> 标签写进 content，
    直接展示会污染输出和历史。这里做流式状态机过滤：标签内的内容转入 thinking，
    标签外的内容正常输出。标签可能被流式切分，故保留一小段尾部缓冲。
    """

    OPEN = "<think>"
    CLOSE = "</think>"
    KEEP = 12  # 保留的尾部长度，需大于最长标签长度

    def __init__(self):
        self.buf = ""
        self.in_think = False

    def feed(self, text: str) -> tuple[str, str]:
        """返回 (可见正文, 思考内容)"""
        visible: list[str] = []
        hidden: list[str] = []
        self.buf += text
        while True:
            if self.in_think:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    if len(self.buf) > self.KEEP:
                        hidden.append(self.buf[: -self.KEEP])
                        self.buf = self.buf[-self.KEEP :]
                    break
                hidden.append(self.buf[:i])
                self.buf = self.buf[i + len(self.CLOSE) :]
                self.in_think = False
                continue
            i_open = self.buf.find(self.OPEN)
            i_close = self.buf.find(self.CLOSE)
            # 只有闭合标签而没有开标签：说明开标签被 Ollama 的提示词模板吃掉了，
            # 前面的内容同样是思考过程，一并丢弃。
            if i_close >= 0 and (i_open < 0 or i_close < i_open):
                hidden.append(self.buf[:i_close])
                self.buf = self.buf[i_close + len(self.CLOSE) :]
                continue
            if i_open < 0:
                if len(self.buf) > self.KEEP:
                    visible.append(self.buf[: -self.KEEP])
                    self.buf = self.buf[-self.KEEP :]
                break
            visible.append(self.buf[:i_open])
            self.buf = self.buf[i_open + len(self.OPEN) :]
            self.in_think = True
        return "".join(visible), "".join(hidden)

    def flush(self) -> tuple[str, str]:
        if self.in_think:
            hidden, self.buf, self.in_think = self.buf, "", False
            return "", hidden
        visible, self.buf = self.buf, ""
        return visible, ""


def native_context(info: dict) -> int:
    """从 /api/show 的返回里挖出模型的**原生上下文长度**。

    字段名随架构而变（llama.context_length / gemma3.context_length / qwen2.context_length…），
    所以不写死键名，扫所有以 .context_length 结尾的项，取其中最大的。
    """
    model_info = (info or {}).get("model_info") or {}
    best = 0
    for k, v in model_info.items():
        if str(k).endswith(".context_length") and isinstance(v, int) and v > best:
            best = v
    return best


class OllamaClient:
    """Ollama 原生 /api/chat 客户端（含流式与工具调用）。"""

    def __init__(self, host: str, keep_alive: str = "15m", timeout: int = 900):
        self.host = host.rstrip("/")
        self.keep_alive = keep_alive
        self.timeout = timeout

    def ping(self) -> bool:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def list_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=10)
            r.raise_for_status()
            return [m["name"] for m in (r.json().get("models") or [])]
        except requests.RequestException:
            return []

    def model_info(self, name: str) -> dict:
        """向 Ollama 询问模型元信息（原生上下文长度、参数量、架构等）。

        一个本地新模型该开多大窗口，只有模型自己知道。发现新模型时读一次，
        就不用让用户自己去 config.yaml 里翻文档填了。
        """
        try:
            r = requests.post(f"{self.host}/api/show", json={"model": name}, timeout=15)
            r.raise_for_status()
            return r.json() or {}
        except requests.RequestException:
            return {}

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        think: bool | None = None,
        keep_alive: str | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """产出 ("delta", str) / ("thinking", str) 增量事件，最后产出 ("result", LLMResult)。"""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive or self.keep_alive,
        }
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = bool(think)

        result = LLMResult()
        acc: dict[int, dict] = {}
        tf = ThinkFilter()

        def emit(vis: str, hid: str):
            if vis:
                result.content += vis
                yield ("delta", vis)
            if hid:
                result.thinking += hid
                yield ("thinking", hid)

        with requests.post(
            f"{self.host}/api/chat", json=payload, stream=True, timeout=self.timeout
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = chunk.get("message", {}) or {}
                text = msg.get("content") or ""
                if text:
                    vis, hid = tf.feed(text)
                    for ev in emit(vis, hid):
                        yield ev
                th = msg.get("thinking") or ""
                if th:
                    result.thinking += th
                    yield ("thinking", th)
                if msg.get("tool_calls"):
                    _merge_tool_calls(acc, msg["tool_calls"])
                if chunk.get("done"):
                    result.prompt_eval_count = int(chunk.get("prompt_eval_count") or 0)
                    result.eval_count = int(chunk.get("eval_count") or 0)
                    result.eval_duration_ns = int(chunk.get("eval_duration") or 0)
                    result.load_duration_ns = int(chunk.get("load_duration") or 0)
                    break

        vis, hid = tf.flush()
        for ev in emit(vis, hid):
            yield ev
        result.tool_calls = _finalize_tool_calls(acc)
        yield ("result", result)

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        think: bool | None = None,
        keep_alive: str | None = None,
    ) -> LLMResult:
        result = LLMResult()
        for kind, payload in self.chat_stream(
            model, messages, tools, options, think, keep_alive
        ):
            if kind == "result":
                result = payload
        return result
