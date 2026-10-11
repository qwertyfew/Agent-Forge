"""自我扩展工具族：让 Agent 在能力不足时自行查找、安装、热加载 skill 与 tool。

- skill_search       在 GitHub 上搜索现成的 Agent Skills
- skill_install      从 git 仓库 / 本地路径安装 skill 到 skills/ 目录
- tool_install       把 Agent 自己编写的工具代码装成插件（plugins/ 目录）
- reload_extensions  热加载：不重启服务，立即生效新装的 skill 和 tool

安全设计：
- skill_install / tool_install 属于 EXEC，默认人工审批
- tool_install 安装前会做导入验证（build_tools 必须存在且返回合法 Tool 列表）
- 安装来源的 SKILL.md 必须能解析出 name/description 才会入库
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from .base import EXEC, SAFE, make_tool

_FM_NAME_RE = re.compile(r"^name:\s*(.+)$", re.M)
_FM_DESC_RE = re.compile(r"^description:\s*(.+)$", re.M)
_NAME_OK = re.compile(r"[a-z][a-z0-9-]{1,40}")


def _read_skill_meta(md: Path) -> tuple[str, str] | None:
    """从 SKILL.md 提取 (name, description)；解析不出 description 视为无效。"""
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FM_DESC_RE.search(raw)
    if not m:
        return None
    desc = m.group(1).strip().strip("\"'")
    nm = _FM_NAME_RE.search(raw)
    name = nm.group(1).strip() if nm else md.parent.name
    return name, desc


def build_ext_tools(guard, cfg, skill_registry=None, holder: dict | None = None):
    """holder: 可选的 {"registry": ToolRegistry} 可变引用。

    build_registry 会在创建完 registry 后把它填进 holder，
    这样 tool_install / reload_extensions 才能把插件工具热加载进 live registry。
    """
    max_out = cfg.max_tool_output_chars
    skills_root = Path(cfg.workspace_root) / "skills"
    holder = holder if holder is not None else {}
    _loaded_plugin_names: list[str] = []

    def _hot_load_plugins() -> list[str]:
        """重扫 plugins/ 并把工具注册进 live registry（替换旧插件工具）。"""
        from . import load_plugin_tools  # 延迟导入避免循环依赖

        reg = holder.get("registry")
        if reg is None:
            return []
        if _loaded_plugin_names:
            reg.unregister(_loaded_plugin_names)
            _loaded_plugin_names.clear()
        fresh = load_plugin_tools(guard, cfg)
        _loaded_plugin_names.extend(reg.register(fresh))
        return reg.names()

    # ---------- skill_search ----------
    def skill_search(query: str, max_results: int = 8) -> str:
        """GitHub 仓库搜索：找现成的 Agent Skills / 工具插件。"""
        q = f"{query} skill in:name,description,readme"
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "per_page": max(1, min(int(max_results), 20))},
                timeout=20,
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            items = r.json().get("items", [])
        except requests.RequestException as e:
            return f"[错误] GitHub 搜索失败: {e}\n提示: 也可用 web_search 搜索，找到仓库地址后直接 skill_install。"
        if not items:
            return f"[搜索] 没有找到与“{query}”相关的 skill 仓库。可换关键词，或自己用 tool_install 编写工具。"
        lines = [f"[skill_search] {query} — 前 {len(items)} 个仓库:"]
        for i, it in enumerate(items, 1):
            desc = (it.get("description") or "")[:120]
            lines.append(
                f"{i}. {it['full_name']}  ⭐{it.get('stargazers_count', 0)}\n"
                f"   安装: skill_install(source=\"{it['full_name']}\")\n"
                f"   {desc}"
            )
        return "\n".join(lines)[:max_out]

    # ---------- skill_install ----------
    def _resolve_source(source: str) -> tuple[Path, str] | str:
        """把 source 归一化成可用的目录。返回 (目录, 描述) 或错误字符串。"""
        src = source.strip()
        # 本地路径
        p = Path(src).expanduser()
        if p.is_dir():
            return p, f"本地目录 {p}"
        # owner/repo 简写 → https 克隆
        if re.fullmatch(r"[\w.-]+/[\w.-]+", src):
            url = f"https://github.com/{src}.git"
        elif src.startswith(("http://", "https://")) and (src.endswith(".git") or "github.com" in src):
            url = src
        else:
            return f"[错误] 无法识别的来源: {src}（支持 本地路径 / owner/repo / git URL）"
        tmp = Path(tempfile.mkdtemp(prefix="af_skill_"))
        code = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", url, str(tmp)],
            capture_output=True, text=True, timeout=180,
        )
        if code.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return (
                f"[错误] 克隆失败: {url}\n{code.stderr.strip()[:400]}\n"
                "提示: 网络不通时可让用户手动下载后给本地路径。"
            )
        return tmp, url

    def skill_install(source: str, force: bool = False) -> str:
        r = _resolve_source(source)
        if isinstance(r, str):
            return r
        src_dir, desc = r
        try:
            found = [md.parent for md in sorted(src_dir.rglob("SKILL.md"))]
            if not found:
                return f"[错误] {desc} 里没有找到任何 SKILL.md，这不是一个 skill 仓库。\n可改用 tool_install 自己编写工具。"
            skills_root.mkdir(parents=True, exist_ok=True)
            installed, skipped = [], []
            for d in found:
                meta = _read_skill_meta(d / "SKILL.md")
                if not meta:
                    skipped.append(f"{d.name}（SKILL.md 缺 name/description）")
                    continue
                name = _NAME_OK.match(meta[0].lower().replace("_", "-"))
                if not name:
                    skipped.append(f"{d.name}（名字不合法: {meta[0]}）")
                    continue
                dest = skills_root / name.group()
                if dest.exists() and not force:
                    skipped.append(f"{name.group()}（已存在，force=true 可覆盖）")
                    continue
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(d, dest, ignore=shutil.ignore_patterns(".git"))
                installed.append(f"{name.group()}: {meta[1][:80]}")
            out = []
            if installed:
                out.append("已安装 skill:")
                out += [f"  + {s}" for s in installed]
            if skipped:
                out.append("跳过:")
                out += [f"  - {s}" for s in skipped]
            if installed and skill_registry is not None:
                skill_registry.scan()
                out.append("已热加载，立即可用（list_skills 可确认）。")
            elif installed:
                out.append("注意: 本次启动未接入 skill 注册表，重启后生效。")
            return "\n".join(out)
        finally:
            if desc.startswith("http") and src_dir.is_dir():
                shutil.rmtree(src_dir, ignore_errors=True)

    # ---------- tool_install ----------
    def tool_install(name: str, code: str) -> str:
        from . import PLUGINS_DIR, Tool  # 延迟导入避免循环依赖

        if not _NAME_OK.match(name):
            return f"[错误] 插件名不合法（只允许小写字母/数字/连字符）: {name}"
        if "def build_tools" not in code:
            return (
                "[错误] 代码里必须定义 build_tools(guard, cfg) 函数并返回 Tool 列表。\n"
                "模板:\n"
                "from agentforge.tools.base import SAFE, make_tool\n"
                "def build_tools(guard, cfg):\n"
                "    def my_tool(...):\n"
                "        ...\n"
                "    return [make_tool(\"my_tool\", \"描述\", {schema}, my_tool, SAFE)]"
            )
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        py = PLUGINS_DIR / f"{name}.py"
        py.write_text(code, encoding="utf-8")
        # 导入验证：不合法就回滚
        try:
            spec = importlib.util.spec_from_file_location(f"af_validate_{name}", py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            built = mod.build_tools(guard, cfg)
            if not isinstance(built, list) or not all(isinstance(t, Tool) for t in built):
                raise ValueError("build_tools 必须返回 Tool 列表")
            tool_names = [t.name for t in built]
        except Exception as e:  # noqa: BLE001
            py.unlink(missing_ok=True)
            return f"[错误] 代码验证失败，已回滚不安装:\n{type(e).__name__}: {e}"
        # 立即热加载进注册表
        _hot_load_plugins()
        return f"插件已安装并热加载: {py}\n新增工具: {', '.join(tool_names)}"

    # ---------- reload_extensions ----------
    def reload_extensions() -> str:
        if skill_registry is not None:
            skill_registry.scan()
            n_skills = len(skill_registry.names())
        else:
            n_skills = -1
        names = _hot_load_plugins()
        msg = [f"skill 重扫完成: {n_skills} 个" if n_skills >= 0 else "skill 注册表未接入"]
        msg.append(f"当前工具总数: {len(names)}（含插件热加载）")
        return "\n".join(msg)

    return [
        make_tool(
            "skill_search",
            "在 GitHub 搜索现成的 Agent Skills 仓库。发现能力缺口时第一步先用它。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如 pdf、excel、翻译"},
                    "max_results": {"type": "integer", "description": "返回条数，默认 8"},
                },
                "required": ["query"],
            },
            skill_search,
            SAFE,
        ),
        make_tool(
            "skill_install",
            "安装 skill 到 skills/ 目录并热加载。source 支持 owner/repo、git URL 或本地路径。装完立即可用。",
            {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "owner/repo 简写、git URL 或本地目录"},
                    "force": {"type": "boolean", "description": "同名 skill 已存在时是否覆盖，默认 false"},
                },
                "required": ["source"],
            },
            skill_install,
            EXEC,
        ),
        make_tool(
            "tool_install",
            "把自己编写的工具代码安装为插件并热加载。code 必须定义 build_tools(guard, cfg) 返回 Tool 列表；验证失败自动回滚。",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "插件名（小写字母/数字/连字符）"},
                    "code": {"type": "string", "description": "完整 Python 源码，必须含 build_tools(guard, cfg)"},
                },
                "required": ["name", "code"],
            },
            tool_install,
            EXEC,
        ),
        make_tool(
            "reload_extensions",
            "热加载：重扫 skills/ 目录与 plugins/ 插件目录，让新装的 skill/tool 立即生效，无需重启。",
            {"type": "object", "properties": {}, "required": []},
            reload_extensions,
            SAFE,
        ),
    ]
