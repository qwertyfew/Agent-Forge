"""多 provider 客户端：让付费云端模型和本地 Ollama 走同一套接口。

Agent 主循环只认一个约定：

    chat_stream(...) -> 产出 ("delta", str) / ("thinking", str)，最后产出 ("result", LLMResult)

所以只要把各家接口的差异收在这一层，上层（loop / context / router）就不用改。

覆盖三类：
    OllamaClient    本地 /api/chat（原生 tool_calls + thinking 字段），在 llm.py 里
    OpenAIClient    OpenAI 兼容 /chat/completions：OpenAI、DeepSeek、Kimi、GLM、
                    通义、硅基流动、各类中转站都走这套
    AnthropicClient Claude 官方 /v1/messages：消息结构与工具格式都不一样，需要转换

ClientHub 负责按 ModelSpec.provider 分发。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

import requests

from .config import ModelSpec
from .llm import LLMResult, ThinkFilter, _finalize_tool_calls, _merge_tool_calls


class ProviderError(RuntimeError):
    pass


def _extract_error(resp: requests.Response) -> str:
    """把厂商返回的错误体压成一句人话。"""
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "")[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err:
            return str(err)[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


class OpenAIClient:
    """OpenAI 兼容 /chat/completions 流式客户端。"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 900):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    # ---------- 基础 ----------
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        base = self.base_url
        if not base:
            raise ProviderError("未配置 base_url")
        # 允许用户填 https://host 或 https://host/v1 两种写法
        if base.endswith("/v1") or base.endswith("/v4") or "/compatible-mode" in base:
            return f"{base}{path}"
        return f"{base}/v1{path}"

    def _convert_options(self, options: dict | None, spec: ModelSpec | None) -> dict:
        opts = dict(options or {})
        out: dict[str, Any] = {}
        if "temperature" in opts:
            out["temperature"] = opts["temperature"]
        if "top_p" in opts:
            out["top_p"] = opts["top_p"]
        np = int(opts.get("num_predict") or 0)
        if np <= 0 and spec is not None:
            np = int(getattr(spec, "num_predict", 0) or 0)
        out["max_tokens"] = np if np > 0 else 8192
        return out

    # ---------- 流式 ----------
    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        think: bool | None = None,
        keep_alive: str | None = None,
        spec: ModelSpec | None = None,
    ) -> Iterator[tuple[str, Any]]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._convert_options(options, spec),
        }
        if tools:
            payload["tools"] = tools

        result = LLMResult()
        acc: dict[int, dict] = {}
        started = time.time()
        got_usage = False

        try:
            resp = requests.post(
                self._url("/chat/completions"),
                headers=self._headers(), json=payload,
                stream=True, timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ProviderError(f"请求失败：{e}") from e

        with resp:
            if resp.status_code >= 400:
                raise ProviderError(f"HTTP {resp.status_code}: {_extract_error(resp)}")

            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue

                usage = chunk.get("usage")
                if usage:
                    got_usage = True
                    result.prompt_eval_count = int(usage.get("prompt_tokens") or 0)
                    result.eval_count = int(usage.get("completion_tokens") or 0)

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                text = delta.get("content") or ""
                if text:
                    result.content += text
                    yield ("delta", text)

                # DeepSeek / 部分厂商把思维链放在 reasoning_content
                think_text = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if think_text:
                    result.thinking += think_text
                    yield ("thinking", think_text)

                if delta.get("tool_calls"):
                    _merge_tool_calls(acc, delta["tool_calls"])

        if not got_usage:
            # 有些厂商不回 usage，用粗略估算兜底，保证 stats 有数
            result.eval_count = max(result.eval_count, len(result.content) // 2)
        result.eval_duration_ns = int(max(time.time() - started, 1e-3) * 1e9)
        result.tool_calls = _finalize_tool_calls(acc)
        yield ("result", result)

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             options: dict | None = None, think: bool | None = None,
             keep_alive: str | None = None, spec: ModelSpec | None = None) -> LLMResult:
        result = LLMResult()
        for kind, payload in self.chat_stream(model, messages, tools, options, think, keep_alive, spec):
            if kind == "result":
                result = payload
        return result

    def ping(self) -> bool:
        try:
            r = requests.get(self._url("/models"), headers=self._headers(), timeout=10)
            return r.status_code < 500
        except (requests.RequestException, ProviderError):
            return False


class AnthropicClient:
    """Claude 官方 /v1/messages 客户端（消息与工具格式需转换）。"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 900):
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.version = "2023-06-01"

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            return f"{base}{path}"
        return f"{base}/v1{path}"

    # ---------- 格式转换 ----------
    @staticmethod
    def _convert_tools(tools: list[dict] | None) -> list[dict]:
        out = []
        for t in tools or []:
            fn = t.get("function", {}) or {}
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return out

    @staticmethod
    def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        """把内部历史转成 Anthropic 的 system + messages。

        Anthropic 要求 tool_use 必须被下一条 user 消息里的 tool_result 应答，
        而内部历史里的 tool 消息不带 id，所以这里按出现顺序现场配对。
        """
        system_parts: list[str] = []
        out: list[dict] = []
        pending_ids: list[str] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content")
            if role == "system":
                if isinstance(content, str) and content.strip():
                    system_parts.append(content)
                continue

            if role == "user":
                out.append({"role": "user", "content": [{"type": "text", "text": str(content or "")}]})
                continue

            if role == "assistant":
                blocks: list[dict] = []
                if isinstance(content, str) and content.strip():
                    blocks.append({"type": "text", "text": content})
                for i, tc in enumerate(m.get("tool_calls") or []):
                    fn = tc.get("function", {}) or {}
                    tid = f"toolu_{len(out)}_{i}"
                    pending_ids.append(tid)
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({
                        "type": "tool_use", "id": tid,
                        "name": fn.get("name", ""), "input": args or {},
                    })
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
                continue

            if role == "tool":
                tid = pending_ids.pop(0) if pending_ids else f"toolu_orphan_{len(out)}"
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result", "tool_use_id": tid,
                        "content": str(content or "")[:20000],
                    }],
                })
                continue

        return "\n\n".join(system_parts), out

    # ---------- 流式 ----------
    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
        think: bool | None = None,
        keep_alive: str | None = None,
        spec: ModelSpec | None = None,
    ) -> Iterator[tuple[str, Any]]:
        system, msgs = self._convert_messages(messages)
        opts = dict(options or {})
        np = int(opts.get("num_predict") or getattr(spec, "num_predict", 0) or 0)
        payload: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "max_tokens": np if np > 0 else 8192,
            "stream": True,
            "temperature": opts.get("temperature", 0.6),
        }
        if system:
            payload["system"] = system
        tool_defs = self._convert_tools(tools)
        if tool_defs:
            payload["tools"] = tool_defs
        if think:
            payload["thinking"] = {"type": "enabled", "budget_tokens": 4096}

        result = LLMResult()
        acc: dict[int, dict] = {}
        block_kinds: dict[int, str] = {}
        json_buf: dict[int, str] = {}
        started = time.time()

        try:
            resp = requests.post(
                self._url("/messages"), headers=self._headers(), json=payload,
                stream=True, timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ProviderError(f"请求失败：{e}") from e

        with resp:
            if resp.status_code >= 400:
                raise ProviderError(f"HTTP {resp.status_code}: {_extract_error(resp)}")

            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue

                etype = ev.get("type")
                if etype == "content_block_start":
                    idx = ev.get("index", 0)
                    block = ev.get("content_block") or {}
                    block_kinds[idx] = block.get("type", "")
                    if block.get("type") == "tool_use":
                        acc[idx] = {
                            "id": block.get("id", f"call_{idx}"),
                            "name": block.get("name", ""),
                            "args": dict(block.get("input") or {}),
                            "raw": "",
                        }
                        json_buf[idx] = ""
                elif etype == "content_block_delta":
                    idx = ev.get("index", 0)
                    d = ev.get("delta") or {}
                    dtype = d.get("type")
                    if dtype == "text_delta":
                        t = d.get("text") or ""
                        if t:
                            result.content += t
                            yield ("delta", t)
                    elif dtype == "thinking_delta":
                        t = d.get("thinking") or ""
                        if t:
                            result.thinking += t
                            yield ("thinking", t)
                    elif dtype == "input_json_delta":
                        json_buf[idx] = json_buf.get(idx, "") + (d.get("partial_json") or "")
                elif etype == "message_delta":
                    usage = ev.get("usage") or {}
                    if usage.get("output_tokens"):
                        result.eval_count = int(usage["output_tokens"])
                elif etype == "message_start":
                    usage = (ev.get("message") or {}).get("usage") or {}
                    if usage.get("input_tokens"):
                        result.prompt_eval_count = int(usage["input_tokens"])
                elif etype == "error":
                    err = ev.get("error") or {}
                    raise ProviderError(str(err.get("message") or err))

        # 把流式拼起来的工具参数落成工具调用
        calls: list[dict] = []
        for idx in sorted(acc):
            e = dict(acc[idx])
            buf = json_buf.get(idx, "").strip()
            if buf:
                try:
                    parsed = json.loads(buf)
                    if isinstance(parsed, dict):
                        e["args"].update(parsed)
                except json.JSONDecodeError:
                    pass
            calls.append(e)
        for i, e in enumerate(calls):
            if not result.tool_calls and not calls:
                break
        result.tool_calls = _finalize_tool_calls({i: e for i, e in enumerate(calls)})

        # Anthropic 会把正文全部走 text_delta，这里不需要 ThinkFilter；
        # 但保险起见仍然过一遍，防止 base_url 指向的中转站把思考塞进正文。
        if "<think>" in result.content:
            tf = ThinkFilter()
            vis, hid = tf.feed(result.content)
            vis2, hid2 = tf.flush()
            clean = vis + vis2
            if clean != result.content:
                result.content = clean
                result.thinking += hid + hid2

        if not result.eval_count:
            result.eval_count = max(len(result.content) // 2, 1)
        result.eval_duration_ns = int(max(time.time() - started, 1e-3) * 1e9)
        yield ("result", result)

    def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
             options: dict | None = None, think: bool | None = None,
             keep_alive: str | None = None, spec: ModelSpec | None = None) -> LLMResult:
        result = LLMResult()
        for kind, payload in self.chat_stream(model, messages, tools, options, think, keep_alive, spec):
            if kind == "result":
                result = payload
        return result

    def ping(self) -> bool:
        try:
            r = requests.post(
                self._url("/messages"), headers=self._headers(),
                json={"model": "claude-sonnet-4-5", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "hi"}]},
                timeout=15,
            )
            return r.status_code < 500
        except (requests.RequestException, ProviderError):
            return False


class ClientHub:
    """按 ModelSpec.provider 把请求分发给对应客户端。

    loop / context 只需要拿它当普通 client 用：hub.chat_stream(spec, messages, tools)。
    """

    def __init__(self, cfg, ollama_client):
        self.cfg = cfg
        self.ollama = ollama_client
        self._cache: dict[tuple, Any] = {}

    def client_for(self, spec: ModelSpec):
        provider = getattr(spec, "provider", "ollama") or "ollama"
        if provider == "ollama":
            return self.ollama
        key = (provider, getattr(spec, "base_url", ""), getattr(spec, "api_key", ""))
        if key not in self._cache:
            if provider == "anthropic":
                self._cache[key] = AnthropicClient(spec.base_url, spec.api_key,
                                                   getattr(self.cfg, "request_timeout", 900))
            else:
                self._cache[key] = OpenAIClient(spec.base_url, spec.api_key,
                                                getattr(self.cfg, "request_timeout", 900))
        return self._cache[key]

    def chat_stream(self, spec: ModelSpec, messages: list[dict],
                    tools: list[dict] | None = None, **kw) -> Iterator[tuple[str, Any]]:
        client = self.client_for(spec)
        args = dict(
            model=spec.remote_model,
            messages=messages,
            tools=tools,
            options=spec.options(),
            think=spec.think,
            spec=spec,
        )
        args.update(kw)
        if isinstance(client, (OpenAIClient, AnthropicClient)):
            return client.chat_stream(**args)
        return client.chat_stream(
            model=args["model"], messages=messages, tools=tools,
            options=args["options"], think=args["think"],
        )

    def chat(self, spec: ModelSpec, messages: list[dict],
             tools: list[dict] | None = None, **kw) -> LLMResult:
        result = LLMResult()
        for kind, payload in self.chat_stream(spec, messages, tools, **kw):
            if kind == "result":
                result = payload
        return result

    def ping(self) -> bool:
        return self.ollama.ping()

    def ping_spec(self, spec: ModelSpec) -> tuple[bool, str]:
        """测试某个模型是否可用（设置页的「测试连接」）。"""
        provider = getattr(spec, "provider", "ollama")
        if provider == "ollama":
            try:
                names = self.ollama.list_models()
            except Exception as e:  # noqa: BLE001
                return False, f"Ollama 不可用：{e}"
            if spec.name in names:
                return True, f"本地模型已就绪（{spec.name}）"
            return False, f"Ollama 中没有模型 {spec.name}，请先 ollama pull"
        client = self.client_for(spec)
        try:
            res = client.chat(
                model=spec.remote_model,
                messages=[{"role": "user", "content": "回复两个字：可用"}],
                tools=None,
                options={"num_predict": 16, "temperature": 0},
                spec=spec,
            )
            text = (res.content or "").strip()[:40]
            return True, f"连接成功，模型回复：{text or '(空)'}"
        except Exception as e:  # noqa: BLE001
            return False, f"连接失败：{e}"


__all__ = ["ClientHub", "OpenAIClient", "AnthropicClient", "ProviderError"]
