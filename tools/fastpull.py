#!/usr/bin/env python
"""AgentForge 模型加速与体检工具

解决三个真实问题：
  1. 国内拉 ollama 官方 registry 慢 -> 自动挑选最快的镜像通道
  2. 权重文件损坏会静默输出乱码 -> 用 sha256 体检，一眼看出好坏
  3. 分片下载中断后的半成品 -> 提供从镜像整包重拉的通路

用法：
  python tools/fastpull.py bench                      测速三大通道，给出推荐
  python tools/fastpull.py verify                     体检本地所有模型
  python tools/fastpull.py verify qwen2.5-coder:14b   体检指定模型
  python tools/fastpull.py pull Qwen/Qwen2.5-Coder-14B-Instruct-GGUF q5_k_m
                                                      从最快镜像拉 GGUF 并导入 ollama

注意：ollama 对“本地已存在”的 blob 会跳过 sha256 校验。因此下载被中断后，
即便文件大小恰好等于目标值，内容也可能有空洞。任何时候怀疑模型输出异常，
先跑一次 verify。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# 三大下载通道。registry 是 ollama 官方源；另两个是国内镜像（实测有效）。
CHANNELS = {
    "registry": "https://registry.ollama.ai/v2/library/qwen2.5-coder/blobs/"
    "sha256:ac9bc7a69dab38da1c790838955f1293420b55ab555ef6b4615efa1c1507b1ed",
    "hf-mirror": "https://hf-mirror.com/Qwen/Qwen2.5-Coder-14B-Instruct-GGUF/"
    "resolve/main/qwen2.5-coder-14b-instruct-q5_k_m.gguf",
    "modelscope": "https://www.modelscope.cn/models/Qwen/Qwen2.5-Coder-14B-Instruct-GGUF/"
    "resolve/master/qwen2.5-coder-14b-instruct-q5_k_m.gguf",
}

# HF_ENDPOINT 让 ollama 的 `hf.co/...` 拉取走镜像而不是直连 huggingface.co
HF_ENDPOINT = "https://hf-mirror.com"


def models_dir() -> Path:
    env = os.environ.get("OLLAMA_MODELS")
    if env:
        return Path(env)
    return Path.home() / ".ollama" / "models"


def http_json(url: str, timeout: int = 25) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer",
            "Accept": "application/vnd.docker.distribution.manifest.v2+json, "
            "application/vnd.ollama.image.manifest+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def local_models() -> list[dict]:
    """读取本机 ollama 已登记的模型。"""
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=10) as r:
            return json.loads(r.read()).get("models", [])
    except Exception as e:
        print(f"无法连接 ollama ({OLLAMA_HOST}): {e}")
        return []


def manifest_layers(model: str, tag: str) -> list[dict]:
    """从官方 registry 取模型清单，返回其中的数据层。"""
    m = http_json(f"https://registry.ollama.ai/v2/library/{model}/manifests/{tag}")
    if not m:
        return []
    return [
        l
        for l in m.get("layers", [])
        if l.get("mediaType", "").startswith("application/vnd.ollama.image")
    ]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 24)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def human(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


# --------------------------------------------------------------------------
# 子命令：bench  测速
# --------------------------------------------------------------------------
def cmd_bench() -> int:
    print("正在测速三大通道（各取 30 秒内的样本）...\n")
    results = []
    for name, url in CHANNELS.items():
        t0 = time.time()
        got = 0
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-30000000"})
            with urllib.request.urlopen(req, timeout=30) as r:
                while time.time() - t0 < 30:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got >= 60_000_000:
                        break
            dt = max(time.time() - t0, 0.001)
            speed = got / dt / 1e6
            results.append((name, speed, True))
        except Exception as e:
            results.append((name, 0.0, False))
            print(f"  {name:<12} 不可用 ({type(e).__name__})")

    results.sort(key=lambda x: -x[1])
    print("\n=== 通道速度排名 ===")
    for name, speed, ok in results:
        if ok:
            print(f"  {name:<12} {speed:6.1f} MB/s")
    best = results[0]
    print(f"\n推荐：{best[0]} ({best[1]:.1f} MB/s)")
    print(
        "\n提示：若要用 hf-mirror 拉 HuggingFace 上的 GGUF，"
        f"重启 ollama 前设置 HF_ENDPOINT={HF_ENDPOINT}"
    )
    return 0


# --------------------------------------------------------------------------
# 子命令：verify  完整性体检
# --------------------------------------------------------------------------
def cmd_verify(target: str | None) -> int:
    mdir = models_dir()
    print(f"模型仓库: {mdir}")
    if not mdir.exists():
        print("仓库不存在，请检查 OLLAMA_MODELS 或默认路径")
        return 1

    models = local_models()
    if target:
        models = [m for m in models if m["name"] == target or m["name"].startswith(target)]
        if not models:
            print(f"未找到模型: {target}")
            return 1

    if not models:
        print("本机没有已登记的模型")
        return 1

    bad = []
    for m in models:
        name = m["name"]
        model, _, tag = name.partition(":")
        tag = tag or "latest"
        layers = manifest_layers(model, tag)
        if not layers:
            print(f"\n{name}: 无法从 registry 取清单（离线或非官方模型），跳过")
            continue

        print(f"\n{name}  ({human(m.get('size', 0))})")
        model_ok = True
        for l in layers:
            dig = l["digest"].split(":")[1]
            path = mdir / "blobs" / f"sha256-{dig}"
            kind = l.get("mediaType", "").split(".")[-1]
            if not path.exists():
                print(f"  {kind:<12} 缺失")
                model_ok = False
                continue
            size = path.stat().st_size
            expect = l.get("size", 0)
            size_ok = size == expect
            digest_ok = sha256_file(path) == dig
            if digest_ok:
                print(f"  {kind:<12} OK  ({human(size)})")
            else:
                print(
                    f"  {kind:<12} 损坏  ({human(size)}"
                    + ("" if size_ok else f"，大小应为 {human(expect)}")
                    + ")"
                )
                model_ok = False
        if model_ok:
            print("  -> 模型完整")
        else:
            print("  -> 模型不可用，请重新下载：")
            print(f"     ollama rm {name} && ollama pull {name}")
            bad.append(name)

    print("\n=== 汇总 ===")
    if bad:
        print(f"损坏 {len(bad)} 个: {', '.join(bad)}")
        return 1
    print("全部模型校验通过")
    return 0


# --------------------------------------------------------------------------
# 子命令：pull  从镜像拉 GGUF 并导入
# --------------------------------------------------------------------------
def cmd_pull(repo: str, quant: str) -> int:
    """从最快镜像下载单个 GGUF 文件，再用 ollama create 导入。

    只支持单文件量化；分片 GGUF（如多数 14B 以上的 q4_k_m）ollama 无法直接导入。
    """
    name = repo.split("/")[-1]
    fname = f"{name.lower()}-{quant}.gguf"
    tmp = Path(os.environ.get("TEMP", ".")) / fname
    print(f"目标文件: {fname}")

    # 选出可用且最快的镜像
    candidates = [
        f"https://hf-mirror.com/{repo}/resolve/main/{fname}",
        f"https://www.modelscope.cn/models/{repo}/resolve/master/{fname}",
        f"https://huggingface.co/{repo}/resolve/main/{fname}",
    ]
    chosen = None
    for url in candidates:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as r:
                total = int(r.headers.get("Content-Length", 0))
            if total > 0:
                chosen = (url, total)
                print(f"可用镜像: {url.split('/')[2]}  大小 {human(total)}")
                break
        except Exception:
            continue
    if not chosen:
        print("没有可用镜像。可能该量化是分片包，请换一个量化或改用 ollama pull。")
        return 1

    url, total = chosen
    # 支持断点续传
    done = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    mode = "ab" if done else "wb"
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        with open(tmp, mode) as f:
            while True:
                chunk = r.read(1 << 22)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                pct = done / total * 100
                el = max(time.time() - t0, 0.001)
                print(
                    f"\r  {human(done)} / {human(total)}  {pct:5.1f}%  "
                    f"{done / el / 1e6:5.1f} MB/s",
                    end="",
                    flush=True,
                )
    print()
    if tmp.stat().st_size != total:
        print("下载不完整，请重跑本命令续传")
        return 1

    model_name = f"{name.lower()}-{quant}"
    modelfile = tmp.with_suffix(".Modelfile")
    modelfile.write_text(
        f"FROM {tmp.as_posix()}\n"
        "PARAMETER num_ctx 16384\n"
        "PARAMETER temperature 0.2\n"
        "PARAMETER repeat_penalty 1.1\n",
        encoding="utf-8",
    )
    print(f"导入 ollama: {model_name}")
    r = subprocess.run(["ollama", "create", model_name, "-f", str(modelfile)])
    if r.returncode == 0:
        print(f"完成: ollama run {model_name}")
    return r.returncode


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "bench":
        return cmd_bench()
    if cmd == "verify":
        return cmd_verify(sys.argv[2] if len(sys.argv) > 2 else None)
    if cmd == "pull":
        if len(sys.argv) < 4:
            print("用法: fastpull.py pull <hf_repo> <quant>")
            return 1
        return cmd_pull(sys.argv[2], sys.argv[3])
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
