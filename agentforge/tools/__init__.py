from __future__ import annotations

import importlib.util
from pathlib import Path

from .base import EXEC, SAFE, WRITE, Tool
from .ext import build_ext_tools
from .fs import build_fs_tools
from .git import build_git_tools
from .memory import build_memory_tools
from .novel import build_novel_tools
from .remote import build_remote_tools
from .search import build_search_tools
from .shell import build_shell_tools
from .skill import build_skill_tools
from .web import build_web_tools

# 动态插件目录：每个 *.py 暴露 build_tools(guard, cfg) -> list[Tool]
PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


def load_plugin_tools(guard, cfg) -> list[Tool]:
    """扫描 plugins/ 目录，加载所有合法插件的工具。坏插件跳过不阻塞启动。"""
    tools: list[Tool] = []
    if not PLUGINS_DIR.is_dir():
        return tools
    for py in sorted(PLUGINS_DIR.glob("*.py")):
        if py.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"af_plugin_{py.stem}", py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            built = mod.build_tools(guard, cfg)
            if isinstance(built, list) and all(isinstance(t, Tool) for t in built):
                tools += built
        except Exception as e:  # noqa: BLE001
            print(f"[插件加载失败] {py.name}: {type(e).__name__}: {e}")
    return tools


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def register(self, tools: list[Tool]) -> list[str]:
        """运行时注入工具（插件热加载用）。返回实际注册的名字。"""
        added = []
        for t in tools:
            if t.name in self._tools:
                continue
            self._tools[t.name] = t
            added.append(t.name)
        return added

    def unregister(self, names: list[str]) -> None:
        for n in names:
            self._tools.pop(n, None)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def describe(self) -> str:
        lines = []
        for t in self._tools.values():
            tag = {"safe": "只读", "write": "写文件", "exec": "执行命令"}.get(t.risk, t.risk)
            lines.append(f"  {t.name:<16} [{tag}]  {t.description.splitlines()[0]}")
        return "\n".join(lines)


def build_registry(guard, cfg, enable_web: bool = True, skill_registry=None) -> ToolRegistry:
    tools: list[Tool] = []
    tools += build_fs_tools(guard, cfg)
    tools += build_search_tools(guard, cfg)
    tools += build_shell_tools(guard, cfg)
    tools += build_git_tools(guard, cfg)
    tools += build_memory_tools(guard, cfg)
    tools += build_remote_tools(guard, cfg)
    tools += build_novel_tools(guard, cfg)
    if enable_web:
        tools += build_web_tools(guard, cfg)
    tools += load_plugin_tools(guard, cfg)
    if skill_registry is not None:
        tools += build_skill_tools(skill_registry)
    # 自我扩展工具族：holder 让它们能拿到创建后的 live registry，实现热加载
    holder: dict = {}
    tools += build_ext_tools(guard, cfg, skill_registry, holder)
    reg = ToolRegistry(tools)
    holder["registry"] = reg
    return reg


__all__ = [
    "Tool",
    "ToolRegistry",
    "build_registry",
    "SAFE",
    "WRITE",
    "EXEC",
]
