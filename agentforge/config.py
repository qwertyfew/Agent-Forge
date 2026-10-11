from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


def _expand(value: str) -> str:
    return os.path.expandvars(str(value))


def _expand_signals(items: list) -> list[str]:
    """信号词在 config.yaml 里按语义分组写成一行，用 " - " 分隔，这里拆成单个模式。

    例如 "- 翻译 - 润色 - 改写" 会被拆成三个独立的正则模式。
    """
    out: list[str] = []
    for raw in items or []:
        for part in str(raw).split(" - "):
            part = part.strip()
            if part:
                out.append(part)
    return out


@dataclass
class ModelSpec:
    key: str
    name: str
    num_ctx: int = 8192
    temperature: float = 0.6
    top_p: float = 0.9
    # 1.0 = 不惩罚。代码模型建议 1.05~1.15，可避免陷入重复循环
    repeat_penalty: float = 1.0
    # 0 = 不限制单次生成长度。设上限可防止模型失控时无限输出
    num_predict: int = 0
    think: bool | None = None
    desc: str = ""

    # ---- 多 provider 支持 ----
    # ollama = 本机 Ollama；openai = OpenAI 兼容接口；anthropic = Claude 官方接口
    provider: str = "ollama"
    base_url: str = ""          # 付费模型的接口地址（openai 兼容 / anthropic）
    api_key: str = ""           # 付费模型的密钥
    model_id: str = ""          # 厂商侧的模型号，例如 gpt-5.5 / claude-sonnet-4-5
    label: str = ""             # 界面上展示的名字（付费模型用）
    entry_id: str = ""          # 可用模型目录里的 id
    kind: str = "local"         # local | paid

    @property
    def display(self) -> str:
        return self.label or self.name

    @property
    def remote_model(self) -> str:
        """真正发给接口的模型号。"""
        return self.model_id or self.name

    def options(self) -> dict:
        opts = {
            "num_ctx": self.num_ctx,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        if self.repeat_penalty and self.repeat_penalty != 1.0:
            opts["repeat_penalty"] = self.repeat_penalty
        if self.num_predict and self.num_predict > 0:
            opts["num_predict"] = self.num_predict
        return opts


@dataclass
class Config:
    ollama_host: str = "http://localhost:11434"
    keep_alive: str = "15m"
    request_timeout: int = 900

    models: dict[str, ModelSpec] = field(default_factory=dict)
    routing_default: str = "general"
    code_signals: list[str] = field(default_factory=list)
    language_signals: list[str] = field(default_factory=list)

    workspace_root: Path = field(default_factory=Path.cwd)
    allow_outside: bool = False
    git_root: Path | None = None      # git 操作的工作目录，默认同工作区根

    require_approval: list[str] = field(default_factory=list)
    command_blocklist: list[str] = field(default_factory=list)

    max_steps: int = 40
    max_tool_output_chars: int = 12000
    keep_recent_tool_results: int = 6
    stream: bool = True

    # ---- 上下文压缩 ----
    compact_threshold: int = 60_000      # 超过这个字符数就触发压缩
    compact_keep_chars: int = 20_000     # 压缩后至少保留的近期内容
    compact_summary_chars: int = 1500    # 摘要的目标长度
    compact_auto: bool = True            # 是否自动压缩

    # ---- Skill 系统 ----
    skill_dirs: list[Path] = field(default_factory=list)
    skill_auto_load: bool = True         # 任务命中时自动注入 skill 正文
    skill_min_score: int = 3             # 自动注入所需的最低匹配分

    session_dir: Path = field(default_factory=lambda: ROOT / ".sessions")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

        cfg = cls()

        o = raw.get("ollama", {}) or {}
        cfg.ollama_host = _expand(o.get("host", cfg.ollama_host)).rstrip("/")
        cfg.keep_alive = str(o.get("keep_alive", cfg.keep_alive))
        cfg.request_timeout = int(o.get("request_timeout", cfg.request_timeout))

        for key, spec in (raw.get("models", {}) or {}).items():
            cfg.models[key] = ModelSpec(
                key=key,
                name=spec.get("name", key),
                num_ctx=int(spec.get("num_ctx", 8192)),
                temperature=float(spec.get("temperature", 0.6)),
                top_p=float(spec.get("top_p", 0.9)),
                repeat_penalty=float(spec.get("repeat_penalty", 1.0)),
                num_predict=int(spec.get("num_predict", 0)),
                think=spec.get("think", None),
                desc=spec.get("desc", ""),
            )

        r = raw.get("routing", {}) or {}
        cfg.routing_default = r.get("default", "general")
        cfg.code_signals = _expand_signals(r.get("code_signals", []) or [])
        cfg.language_signals = _expand_signals(r.get("language_signals", []) or [])

        w = raw.get("workspace", {}) or {}
        cfg.workspace_root = Path(_expand(w.get("root", str(ROOT))))
        cfg.allow_outside = bool(w.get("allow_outside", False))
        gr = w.get("git_root")
        cfg.git_root = Path(_expand(gr)) if gr else None

        p = raw.get("permissions", {}) or {}
        cfg.require_approval = list(p.get("require_approval", []) or [])
        cfg.command_blocklist = [c.lower() for c in (p.get("command_blocklist", []) or [])]

        a = raw.get("agent", {}) or {}
        cfg.max_steps = int(a.get("max_steps", 40))
        cfg.max_tool_output_chars = int(a.get("max_tool_output_chars", 12000))
        cfg.keep_recent_tool_results = int(a.get("keep_recent_tool_results", 6))
        cfg.stream = bool(a.get("stream", True))

        c = raw.get("compact", {}) or {}
        cfg.compact_threshold = int(c.get("threshold", cfg.compact_threshold))
        cfg.compact_keep_chars = int(c.get("keep_chars", cfg.compact_keep_chars))
        cfg.compact_summary_chars = int(c.get("summary_chars", cfg.compact_summary_chars))
        cfg.compact_auto = bool(c.get("auto", cfg.compact_auto))

        sk = raw.get("skills", {}) or {}
        cfg.skill_auto_load = bool(sk.get("auto_load", cfg.skill_auto_load))
        cfg.skill_min_score = int(sk.get("min_score", cfg.skill_min_score))
        cfg.skill_dirs = [
            Path(_expand(d)) for d in (sk.get("dirs", []) or [])
        ]

        s = raw.get("session", {}) or {}
        cfg.session_dir = Path(_expand(s.get("dir", str(ROOT / ".sessions"))))
        if not cfg.session_dir.is_absolute():
            cfg.session_dir = ROOT / cfg.session_dir

        return cfg

    def model(self, key: str) -> ModelSpec:
        if key not in self.models:
            raise KeyError(f"未定义的模型 key: {key}，可选: {list(self.models)}")
        return self.models[key]

    def register_model(self, spec: ModelSpec) -> None:
        """运行时注册模型（自动发现的本地模型、用户添加的付费模型都走这里）。"""
        self.models[spec.key] = spec

    def unregister_model(self, key: str) -> None:
        self.models.pop(key, None)
