from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterator

import requests

# 服务端错误（5xx）与网络抖动的自动重试：复测里 14 个子任务死于同一形态的
# Ollama 500——生成正常完成后响应处理阶段崩，同一 prompt 再请求往往就过
# （Leader 的失败重排反复证明了这一点）。重试只发生在**还没吐出任何增量**
# 的阶段（状态行被拒/连接失败），流中途断掉的不重试（无法保证不重复）。
LLM_RETRIES = 2          # 额外重试次数
LLM_RETRY_BACKOFF = 2.0  # 指数退避基数（秒）：2s, 4s


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


class GenerationStopped(Exception):
    """流式生成中途被停止信号打断（见 chat_stream 的 stop_check）。

    result 里带着已生成的半截内容。调用方不要当普通错误处理：
    不重试、不当网络故障，直接按「用户停止」收尾。
    """

    def __init__(self, result: "LLMResult"):
        super().__init__("生成被停止信号打断")
        self.result = result


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


# 本地模型串行锁：避免同一时刻两个任务都在跑本地（Ollama）模型而互相抢 GPU/显存，
# 把彼此拖慢甚至挤爆显存。仅对本地模型生效——OllamaClient.chat_stream 会取这把锁；
# 付费云端模型走各自的 Client（OpenAIClient / AnthropicClient），不受此锁影响，
# 所以「一句话里两个不相关的任务」仍然可以让付费/在线模型并行，本地模型则排队。
_LOCAL_LOCK = threading.Lock()
_LOCAL_SERIAL = True


def set_local_serial(on: bool) -> None:
    """全局开关：本地模型是否串行执行（同一时刻只跑一个本地模型）。"""
    global _LOCAL_SERIAL
    _LOCAL_SERIAL = bool(on)


def local_serial_on() -> bool:
    return _LOCAL_SERIAL


class OllamaClient:
    """Ollama 原生 /api/chat 客户端（含流式与工具调用）。"""

    def __init__(self, host: str, keep_alive: str = "15m", timeout: int = 900):
        self.host = host.rstrip("/")
        self.keep_alive = keep_alive
        self.timeout = timeout

    def _post_chat(self, payload: dict, *, stream: bool):
        """POST /api/chat，带 5xx/网络错误指数退避重试。

        只覆盖「响应还没开始消费」的阶段：状态行被拒（raise_for_status）、
        连接建立失败、超时。流已经吐出增量后的中断不在重试范围——那无法
        保证不重复内容。4xx（模型名错、参数错）重试无意义，直接抛。"""
        last: Exception | None = None
        for attempt in range(1 + LLM_RETRIES):
            try:
                resp = requests.post(
                    f"{self.host}/api/chat", json=payload,
                    stream=stream, timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else None
                if code is not None and code < 500:
                    raise                       # 参数/模型名错：重试无意义
                last = e
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
            if attempt < LLM_RETRIES:
                time.sleep(LLM_RETRY_BACKOFF * (2 ** attempt))   # 2s, 4s
        raise last  # type: ignore[misc]

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

    def warmup(self, model: str, timeout: int = 60) -> tuple[bool, str]:
        """最小生成探活（开局健康门禁用）：对 /api/chat 发一个极简请求，验证模型
        能正常生成、不是只活着却持续 5xx。

        ping() 只打 /api/tags，Ollama 进程在就返回 200，探不出「生成链路挂了」这类
        复测里真实出现的 500；warmup 直接走真实 /api/chat，并把 5xx 交给 _post_chat
        的指数退避重试。返回值：(是否可用, 失败原因非空时才有)。
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        try:
            resp = self._post_chat(payload, stream=False)
            data = resp.json()
            msg = ((data.get("message") or {}).get("content") or "").strip()
            if msg:
                return True, ""
            return False, "模型返回了空响应（可能正处于加载/异常态）"
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code == 404:
                return False, f"Ollama 中找不到模型 {model}（请先 ollama pull）"
            if code is not None and code < 500:
                return False, f"Ollama 返回 {code}（请求参数或模型配置问题）"
            return False, f"Ollama 持续返回 5xx（{code}）"
        except requests.RequestException as e:
            return False, f"无法连接 Ollama（{type(e).__name__}: {e}）"

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

    def delete_model(self, name: str) -> bool:
        """删除本地模型（等价于 `ollama rm`），释放磁盘上的权重文件。

        供「可用模型管理」页的「移除本地模型」用：移除时除了把它从 AgentForge 的
        可用目录里摘掉，还要让 Ollama 把它真正删掉——否则磁盘上的模型文件会一直
        占着空间，下次刷新又会被自动发现、重新出现在列表里。
        """
        try:
            r = requests.post(
                f"{self.host}/api/delete", json={"model": name}, timeout=60
            )
            return r.status_code in (200, 204)
        except requests.RequestException:
            return False

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        think: bool | None = None,
        keep_alive: str | None = None,
        stop_check=None,
    ) -> Iterator[tuple[str, Any]]:
        """产出 ("delta", str) / ("thinking", str) 增量事件，最后产出 ("result", LLMResult)。

        stop_check 非空时在流式循环里逐块检查：返回 True 就抛 GenerationStopped，
        让「正在生成的那一次回复」也能被暂停按钮打断，而不是等它跑完。"""
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

        # 本地模型串行锁：整个生成过程（含流式输出）独占，确保同一时刻只有一个
        # 本地模型在跑。finally 保证无论是正常结束、被 stop_check 打断（抛
        # GenerationStopped），还是调用方提前 close() 生成器，锁都会被释放。
        serial = _LOCAL_SERIAL
        if serial:
            _LOCAL_LOCK.acquire()
        try:
            with self._post_chat(payload, stream=True) as resp:
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if stop_check is not None and stop_check():
                        raise GenerationStopped(result)
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
        finally:
            if serial:
                try:
                    _LOCAL_LOCK.release()
                except RuntimeError:
                    pass

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


def ollama_probe(client: "OllamaClient"):
    """给 ModelStore 用的探测回调：问 Ollama 要某个模型的原生上下文长度。

    只有「Ollama 里有、config.yaml 里没声明」的模型会走到这里，而且每个模型
    只问一次（结果缓存进 models.local.json），所以这点耗时可以接受。这样用户
    pull 完新模型直接勾选就能用，不必自己去查文档填 num_ctx。

    CLI 与 Web 两条路径共用这一份实现——各写一份迟早会漂移。
    """
    def probe(name: str) -> dict:
        try:
            return {"native_ctx": native_context(client.model_info(name))}
        except Exception:  # noqa: BLE001
            return {}
    return probe
