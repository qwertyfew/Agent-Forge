"""本地模型性能基准：加载耗时 / 提示处理速度 / 生成速度 / 显存占用。

数据来源是 Ollama 自己的计时字段，不是拿秒表掐的——`/api/generate` 在最后
一个响应里会带着这些纳秒级计数：

    load_duration          模型加载进显存/内存用了多久
    prompt_eval_count      提示吃了多少 token
    prompt_eval_duration   处理这些提示花了多久
    eval_count             新生成多少 token
    eval_duration          生成这些 token 花了多久

于是：

    生成速度 = eval_count / eval_duration        解码速度，决定"读起来快不快"
    提示处理 = prompt_eval_count / prompt_eval_duration   预填充速度，决定"首字等多久"

这两个数不含排队与网络开销，跨模型横向比较是公平的。

用法：
    python tools/bench.py                 # 测所有已安装的本地模型
    python tools/bench.py gpt-oss:20b     # 只测指定模型
    python tools/bench.py --ctx 262144    # 指定窗口，默认取各模型配置里的值
    python tools/bench.py --tools         # 顺带做一次工具调用冒烟测试
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "http://127.0.0.1:11434"

WARM_PROMPT = "你好"
GEN_PROMPT = "用中文写一段约 300 字的说明文，介绍中国茶文化的历史与主要茶类，要求条理清晰。"
GEN_NUM_PREDICT = 320
DEFAULT_CTX = 8192

# 工具调用冒烟测试：给一个最简单的工具，看模型会不会正确地"调工具"而不是用嘴回答
TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某个城市的当前天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}]
TOOL_PROMPT = "北京今天天气怎么样？请调用工具查询。"


# ------------------------------------------------------------------ HTTP
def _req(path: str, payload: dict | None = None, timeout: int = 900) -> dict:
    url = HOST + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def installed() -> list[str]:
    try:
        return [m["name"] for m in (_req("/api/tags").get("models") or [])]
    except Exception:  # noqa: BLE001
        return []


def ctx_of_models() -> dict[str, int]:
    """各模型该开多大窗口：config.yaml 显式声明的优先，其次是自动探测结果。"""
    out: dict[str, int] = {}
    local = ROOT / "models.local.json"
    if local.exists():
        try:
            for name, v in (json.loads(local.read_text(encoding="utf-8")) or {}).items():
                if isinstance(v, dict) and v.get("num_ctx"):
                    out[str(name)] = int(v["num_ctx"])
        except (json.JSONDecodeError, OSError):
            pass
    try:
        sys.path.insert(0, str(ROOT))
        from agentforge.config import Config  # noqa: E402
        for spec in Config.load().models.values():
            if getattr(spec, "kind", "") != "paid" and getattr(spec, "num_ctx", 0):
                out[spec.name] = int(spec.num_ctx)
    except Exception:  # noqa: BLE001
        pass
    return out


_PS_RE = re.compile(r"(\d+%(?:/\d+%)?\s*(?:CPU|GPU)(?:/CPU|/GPU)?)")


def ps_split() -> dict[str, str]:
    """`ollama ps` 的 PROCESSOR 列：模型名 -> 原始串（如 `100% GPU`、`18%/82% CPU/GPU`）。

    原样透出而不拆成数字：标着 CPU/GPU 的那一列，数字顺序与标签顺序的对应关系
    各版本略有出入，拆错了反而误导，不如让人自己看。
    """
    out: dict[str, str] = {}
    try:
        txt = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        ).stdout
    except Exception:  # noqa: BLE001
        return out
    for line in txt.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        m = _PS_RE.search(line)
        if m:
            out[line.split()[0]] = m.group(1)
    return out


# ------------------------------------------------------------------ 单项
def bench(model: str, num_ctx: int, with_tools: bool = False, cold: bool = False) -> dict:
    res: dict = {"model": model, "num_ctx": num_ctx}

    # 0) --cold：先把模型从显存里卸掉，这样下一步的加载耗时才是真的冷加载
    if cold:
        try:
            _req("/api/generate", {
                "model": model, "prompt": "", "stream": False, "keep_alive": 0,
            })
        except Exception:  # noqa: BLE001
            pass

    # 1) 热身：把模型真正加载进显存，顺便量出加载耗时
    t0 = time.time()
    warm = _req("/api/generate", {
        "model": model, "prompt": WARM_PROMPT, "stream": False, "keep_alive": "15m",
        "options": {"num_ctx": num_ctx, "num_predict": 8},
    })
    res["load_s"] = (warm.get("load_duration") or 0) / 1e9
    res["cold_s"] = time.time() - t0          # 含排队/调度的实际等待

    # 2) 正式跑一次：取 prefill / decode 的真实速率
    r = _req("/api/generate", {
        "model": model, "prompt": GEN_PROMPT, "stream": False, "keep_alive": "15m",
        "options": {"num_ctx": num_ctx, "num_predict": GEN_NUM_PREDICT, "temperature": 0.6},
    })
    pc, pd = int(r.get("prompt_eval_count") or 0), (r.get("prompt_eval_duration") or 0) / 1e9
    ec, ed = int(r.get("eval_count") or 0), (r.get("eval_duration") or 0) / 1e9
    res["prompt_tokens"], res["prompt_s"] = pc, pd
    res["gen_tokens"], res["gen_s"] = ec, ed
    res["prompt_tps"] = pc / pd if pd else 0.0
    res["gen_tps"] = ec / ed if ed else 0.0
    res["wall_s"] = (r.get("total_duration") or 0) / 1e9

    # 3) 工具调用冒烟：模型能不能吐出 tool_calls（harness 的关键能力）
    if with_tools:
        try:
            t = _req("/api/chat", {
                "model": model, "stream": False, "keep_alive": "15m",
                "messages": [{"role": "user", "content": TOOL_PROMPT}],
                "tools": TOOL_SCHEMA,
                "options": {"num_ctx": num_ctx, "num_predict": 128},
            })
            calls = (t.get("message") or {}).get("tool_calls") or []
            res["tool_ok"] = bool(calls)
            res["tool_call"] = (calls[0].get("function") or {}).get("name", "") if calls else ""
        except Exception as e:  # noqa: BLE001
            res["tool_ok"] = False
            res["tool_call"] = f"错误: {e}"

    return res


# ------------------------------------------------------------------ 输出
def _dw(s: str) -> int:
    """字符串的显示宽度：中文/全角字符占两列，其余占一列。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


def _pad(s: str, width: int, right: bool = False) -> str:
    s = str(s)
    gap = width - _dw(s)
    if gap <= 0:
        return s
    return (" " * gap + s) if right else (s + " " * gap)


_COLS = ["模型", "窗口", "加载s", "提示tok/s", "生成tok/s", "生成tok", "总耗时s"]
_WIDTHS = [24, 8, 9, 12, 12, 10, 9]
_RIGHT = [False, True, True, True, True, True, True]


def _row(r: dict) -> str:
    vals = [
        r["model"], str(r["num_ctx"]), f"{r['load_s']:.1f}",
        f"{r['prompt_tps']:.0f}", f"{r['gen_tps']:.1f}",
        str(r["gen_tokens"]), f"{r['wall_s']:.1f}",
    ]
    return "".join(_pad(v, w, rt) for v, w, rt in zip(vals, _WIDTHS, _RIGHT))


def main() -> int:
    ap = argparse.ArgumentParser(description="本地模型性能基准")
    ap.add_argument("models", nargs="*", help="要测的模型名，留空则测所有已安装模型")
    ap.add_argument("--ctx", type=int, default=0, help="统一指定窗口大小")
    ap.add_argument("--tools", action="store_true", help="附加工具调用冒烟测试")
    ap.add_argument("--cold", action="store_true",
                    help="先卸载模型再测，量真实冷加载耗时（默认复用已加载的模型）")
    args = ap.parse_args()

    names = args.models or installed()
    if not names:
        print("没有可测的模型（Ollama 没启动？先用 `ollama list` 确认）")
        return 1

    ctxs = ctx_of_models()
    print(f"Ollama: {HOST}\n")
    header = "".join(_pad(c, w, rt) for c, w, rt in zip(_COLS, _WIDTHS, _RIGHT))
    print(header)
    print("-" * _dw(header))

    rows = []
    for name in names:
        ctx = args.ctx or ctxs.get(name) or DEFAULT_CTX
        print(f"  ... {name} 运行中", flush=True)
        try:
            r = bench(name, ctx, with_tools=args.tools, cold=args.cold)
        except Exception as e:  # noqa: BLE001
            print(f"{name:<22} 失败: {e}")
            continue
        rows.append(r)
        print("\r" + _row(r))

    if rows and not args.cold and min(r["load_s"] for r in rows) < 0.5:
        print("\n注：加载s 接近 0 说明该模型已在显存里（前面刚跑过）；要测真实冷加载加 --cold。")

    if args.tools:
        print("\n工具调用冒烟：")
        for r in rows:
            mark = "通过" if r.get("tool_ok") else "未通过"
            print(f"  {_pad(r['model'], 24)}{mark}  {r.get('tool_call', '')}")

    split = ps_split()
    if split:
        print("\n当前显存/算力分布（ollama ps）：")
        for name, where in split.items():
            print(f"  {_pad(name, 24)}{where}")

    if rows:
        best = max(rows, key=lambda r: r["gen_tps"])
        print(f"\n最快生成：{best['model']} —— {best['gen_tps']:.1f} tok/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
