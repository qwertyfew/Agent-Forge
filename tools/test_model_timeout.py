"""模型流式读取的「空闲超时」自测（P8）。

现场：soak32 的 q1 卡了 10 分钟，journal 里只有 run_start + system 两条事件，
一个 token 都没回来、也没有任何报错。根因不是模型慢，是**我们没给"不回包"设上限**：
- `llm.OllamaClient` 用 `timeout=900` 当总超时，对"TCP 还在、就是不回包"太宽；
- 读流出错只冒一行裸 traceback，看不出是端侧卡住；
- bench 的 `--timeout` 写在事件循环体里，没有事件就永远走不到（这条在 bench 侧修）。

这个套件钉住三件事：空闲超时真的存在、超时被翻译成人话、超时不进重试分支。
运行：python tools/test_model_timeout.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests                                            # noqa: E402

from agentforge import llm, providers                      # noqa: E402


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(name)


class _StalledResp:
    """状态 200，但读流时一直读不到东西，直到 read timeout。"""

    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = False):
        raise requests.Timeout("Read timed out. (read timeout=180)")
        yield ""  # pragma: no cover —— 让它成为生成器

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_idle_lines_translates() -> None:
    try:
        list(llm._idle_lines(_StalledResp(), 30))
    except llm.ModelIdleTimeout as e:
        msg = str(e)
        check("空闲超时被翻译成 ModelIdleTimeout", True)
        check("措辞里带秒数与人话", "30s" in msg and "没有返回任何新内容" in msg, msg[:120])
        check("明确说「不要原样重试」", "重试" in msg, msg[:120])
    else:  # pragma: no cover
        check("空闲超时被翻译成 ModelIdleTimeout", False, "没有抛异常")

    # 关键：不能是 requests.Timeout 的子类，否则会被 chat_stream 的重试分支吞掉，
    # 端侧已经卡住时重试只是把等待翻倍。
    check("ModelIdleTimeout 不是 requests.Timeout 子类",
          not issubclass(llm.ModelIdleTimeout, requests.Timeout))


def test_post_uses_idle_read_timeout() -> None:
    seen: dict = {}

    class _FakeRequests:
        @staticmethod
        def post(url, **kw):
            seen.update(kw)
            return _StalledResp()

    real = llm.requests
    llm.requests = _FakeRequests
    try:
        client = llm.OllamaClient("http://127.0.0.1:11434", "15m", 900, idle_timeout=180)
        client._post_chat({"model": "x"}, stream=True)
    finally:
        llm.requests = real
    check("POST 的 timeout 是 (connect, read) 元组",
          isinstance(seen.get("timeout"), tuple) and seen["timeout"] == (10, 180),
          str(seen.get("timeout")))
    check("空闲上限默认 180s", llm.OllamaClient("http://x").idle_timeout == 180)
    check("空闲上限有下限（1s 会被抬到 10s）",
          llm.OllamaClient("http://x", idle_timeout=1).idle_timeout == 10)
    check("传 0 视为未设置（回落到默认）",
          llm.OllamaClient("http://x", idle_timeout=0).idle_timeout == 180)


def test_remote_providers_idle() -> None:
    oa = providers.OpenAIClient("https://api.example.com", "k", 900, idle_timeout=60)
    an = providers.AnthropicClient("https://api.anthropic.com", "k", 900, idle_timeout=60)
    check("OpenAI 客户端读超时 = 空闲上限", oa._read_timeout() == 60)
    check("Anthropic 客户端读超时 = 空闲上限", an._read_timeout() == 60)
    # 远端流式读取同样要翻译成 ProviderError（远端路径的异常类型是它）
    try:
        list(providers._guarded_lines(_StalledResp(), 45))
    except providers.ProviderError as e:
        check("远端流空闲超时 → ProviderError", "45s" in str(e), str(e)[:120])
    else:  # pragma: no cover
        check("远端流空闲超时 → ProviderError", False, "没有抛异常")


def test_config_exposes_knob() -> None:
    from agentforge.config import Config
    cfg = Config.load(ROOT / "config.yaml")
    check("配置里有 stream_idle_timeout", hasattr(cfg, "stream_idle_timeout"),
          str(getattr(cfg, "stream_idle_timeout", None)))
    check("默认值不超过 300s（超过就不算「发现卡住」）",
          int(cfg.stream_idle_timeout) <= 300, str(cfg.stream_idle_timeout))


def main() -> None:
    test_idle_lines_translates()
    test_post_uses_idle_read_timeout()
    test_remote_providers_idle()
    test_config_exposes_knob()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
