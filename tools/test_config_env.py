"""容器部署的环境变量覆盖（config.py / webui.serve）回归。

钉住四件事：AGENTFORGE_OLLAMA_HOST 覆盖配置文件、auto_start 的真/假解析、
不设环境变量时本机行为不变。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentforge.config import Config


def check(name, cond, detail=""):
    mark = "ok" if cond else "FAIL"
    print(f"  [{_n[0] + 1}] {mark}  {name}" + (f"  {detail}" if detail and not cond else ""))
    _n[0] += 1
    if not cond:
        _fails.append(name)


_n = [0]
_fails: list = []

# E1 不设环境变量：行为与旧版一致（读 config.yaml 的 host）
os.environ.pop("AGENTFORGE_OLLAMA_HOST", None)
os.environ.pop("AGENTFORGE_OLLAMA_AUTO_START", None)
cfg = Config.load(ROOT / "config.yaml")
check("E1 无环境变量 → 用 config.yaml 的 host",
      cfg.ollama_host == "http://localhost:11434", cfg.ollama_host)
check("E2 无环境变量 → auto_start 保持配置值", cfg.ollama_auto_start is True, "")

# E3 host 被环境变量覆盖（容器/云端换上游不改镜像里的配置文件）
os.environ["AGENTFORGE_OLLAMA_HOST"] = "http://ollama:11434/"
cfg = Config.load(ROOT / "config.yaml")
check("E3 AGENTFORGE_OLLAMA_HOST 覆盖生效且去掉尾斜杠",
      cfg.ollama_host == "http://ollama:11434", cfg.ollama_host)

# E4 auto_start=false → ensure 只探测不拉起（容器里没有 Ollama 可执行文件）
os.environ["AGENTFORGE_OLLAMA_AUTO_START"] = "false"
cfg = Config.load(ROOT / "config.yaml")
check("E4 auto_start=false 生效", cfg.ollama_auto_start is False, "")
os.environ["AGENTFORGE_OLLAMA_AUTO_START"] = "1"
cfg = Config.load(ROOT / "config.yaml")
check("E5 auto_start=1 → True", cfg.ollama_auto_start is True, "")

for k in ("AGENTFORGE_OLLAMA_HOST", "AGENTFORGE_OLLAMA_AUTO_START"):
    os.environ.pop(k, None)

print(f"\n共 {_n[0]} 项断言"
      + ("，全部通过。" if not _fails else f"，失败 {len(_fails)} 条：{_fails}"))
sys.exit(1 if _fails else 0)
