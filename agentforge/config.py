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
    # 这个模型单次调用后把权重在显存里留多久（空 = 用 ollama.keep_alive 的全局值）。
    # 决策模型这类「调用一次就闲着」的模型可以设 "0"，用完立刻释放显存，
    # 给执行模型腾地方——16GB 卡上两个本地模型常驻会互相挤。
    keep_alive: str = ""

    # ---- 多 provider 支持 ----
    # ollama = 本机 Ollama；openai = OpenAI 兼容接口；anthropic = Claude 官方接口
    provider: str = "ollama"
    base_url: str = ""          # 付费模型的接口地址（openai 兼容 / anthropic）
    api_key: str = ""           # 付费模型的密钥
    model_id: str = ""          # 厂商侧的模型号，例如 gpt-5.5 / claude-sonnet-4-5
    label: str = ""             # 界面上展示的名字（付费模型用）
    entry_id: str = ""          # 可用模型目录里的 id
    kind: str = "local"         # local | paid
    # 这台机器上现在能不能真的跑起来：本地模型要看 Ollama 装没装，
    # 付费模型要看 API Key 填没填。由 ModelStore.catalog() 在登记模型时写入，
    # 抢不到信息的场合保持 True（宁可试一下报错，也不要凭空禁用）。
    available: bool = True

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
    # 本地模型串行执行：同一时刻只跑一个本地模型，避免两个并发任务同时压 Ollama
    # 抢 GPU/显存互相拖累。付费/在线模型不受此限制，可并行。
    local_serial: bool = True
    # Ollama 没起来时自动拉起（见 agentforge/ollamaguard.py）。
    # 本机长期不关机，Ollama 可能被手动退出或升级时重启，本地模型会静默失效。
    ollama_auto_start: bool = True
    # 拉起后最多等多少秒才判定失败。冷启动要加载模型索引，别给太短。
    ollama_wait: int = 45

    models: dict[str, ModelSpec] = field(default_factory=dict)
    routing_default: str = "general"
    code_signals: list[str] = field(default_factory=list)
    language_signals: list[str] = field(default_factory=list)

    # ---- 能力路由 ----
    # 按任务所需的能力维度（编码/推理/写作/中文…）从可用模型里挑最合适的，
    # 并在默认模型偏弱时给出升级建议（可选自动升级到更强的可用模型）。
    routing_capability: bool = True          # 是否开启能力路由
    routing_quality_threshold: int = 78     # 所需维度低于此分视为「偏弱」，触发升级评估
    routing_allow_upgrade: bool = False      # 命中更强本地模型时是否自动改用（默认只提示）
    routing_allow_paid_upgrade: bool = False # 是否允许自动改用付费模型（默认绝不，避免偷偷花钱）

    workspace_root: Path = field(default_factory=Path.cwd)
    allow_outside: bool = False
    git_root: Path | None = None      # git 操作的工作目录，默认同工作区根
    # 运行期状态目录（长期记忆 memory.md、上传的附件）。默认 <工作区>/.agentforge；
    # 想让它跟着项目走而不落在工作区里，在 config 里指定 state_dir 即可。
    state_dir: Path | None = None

    require_approval: list[str] = field(default_factory=list)
    command_blocklist: list[str] = field(default_factory=list)

    max_steps: int = 40
    max_tool_output_chars: int = 12000
    keep_recent_tool_results: int = 8
    # 轮次预算：软预算 = 从这轮起每轮提醒「该收敛了」；硬上限 = 撤掉工具强制交卷。
    # 反复迭代的主要出口：不给预算，小模型能把一个简单任务探索 50+ 轮。
    rounds_soft: int = 30
    rounds_hard: int = 60
    stream: bool = True
    relay_brief: bool = True           # 多步任务交接时只传「计划+摘要+文件」，不传全量历史
    # 任务收尾的记忆自动沉淀（规则抽取 + 模型提炼）。关掉后仍保留两条手动通道：
    # 模型主动调 remember 工具、侧栏「记忆管理」手工增删。
    memory_autonote: bool = True
    # 收尾验证闸门（agentforge/verify.py）：任务书声明了验证命令（「验证：xxx」）时，
    # 任务结束前由系统亲自运行该命令、把真实结果附进最终答复；模型只有解读权。
    verify_gate: bool = True
    # 一手来源核查（agentforge/sources.py）：用过联网搜索、却全程没抓过一手来源
    # （fetch_url / fetch_json / browser_snapshot），而总结里又出现数字 / 版本 / 日期
    # 这类外部事实时，由程序在答复末尾追加「转述 · 未经核实」声明。
    sources_gate: bool = True

    # ---- 多 agent 调度（Dispatcher）----
    # 由一个本地决策模型把请求拆成【语义独立】的任务、指派执行 agent、维护共享进度，
    # 任务在 agent 之间接力流转。模型不可用或输出无法解析时自动回退到规则拆解。
    dispatcher_enabled: bool = True
    # 决策/调度模型。默认 Qwen3-14B：拆解与交接能力强、中文好，Q4 量化约 9GB，
    # 16GB 显存能跑（32B 的 Q4 约 20GB，16GB 卡放不下）。可在「可用模型管理」里
    # 换成任意本地或在线模型（留空=关闭调度，回退到规则拆解）。
    dispatcher_model: str = "qwen3.5:4b"
    dispatcher_max_tasks: int = 6
    # 独立任务并行执行（互不相关的任务同时跑，减少模型闲置）。只在「完全访问权限」
    # 下生效——并发任务各自弹审批框会互相打架。本地模型仍受 ollama.local_serial 串行约束，
    # 所以并行的收益主要在在线/付费模型上。
    dispatcher_parallel: bool = True
    dispatcher_translate: bool = True      # 弱中文模型做翻译桥接（先译入、结果再译回）
    # 中文维度低于这个分的执行模型才做翻译桥接。桥接要多花两次调度调用，只有真
    # 的不会中文的模型才值得；阈值定太高会让每个本地模型都白白绕一圈。
    dispatcher_bridge_threshold: int = 55
    # 是否允许决策模型自己动手执行「简单对话」类任务（闲聊/解释，不碰工具不做计算）。
    # 工具/代码/数学/写作/分析这类要真本事的任务无论开关如何都不会派给决策模型。
    dispatcher_execute_simple: bool = True
    dispatcher_synthesis: bool = True      # 收尾由决策模型把各 agent 结果汇总成一份答复

    # ---- 工具按需下发（省固定开销，见 agentforge/toolselect.py）----
    tools_lazy: bool = True            # True = 只下发核心 + 关键词命中的工具
    tools_core: list[str] = field(default_factory=list)   # 常驻工具名，空则用内置清单
    tools_limit: int = 16              # 单轮最多下发几个 schema
    tools_min_score: int = 2           # 关键词命中的最低分

    # ---- 上下文压缩 ----
    compact_threshold: int = 60_000      # 超过这个字符数就触发压缩
    compact_keep_chars: int = 20_000     # 压缩后至少保留的近期内容
    compact_summary_chars: int = 1500    # 摘要的目标长度
    compact_auto: bool = True            # 是否自动压缩

    # ---- Skill 系统 ----
    skill_dirs: list[Path] = field(default_factory=list)
    skill_auto_load: bool = True         # 任务命中时自动注入 skill 正文
    skill_min_score: int = 3             # 自动注入所需的最低匹配分
    skill_auto_max_chars: int = 1600     # 自动注入时正文截断长度（完整性用 use_skill 取）

    session_dir: Path = field(default_factory=lambda: ROOT / ".sessions")
    # 加载时所用配置文件的路径（运行时回写路由开关用，如 config.yaml / config.lanrun.yaml）
    source_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

        cfg = cls()
        cfg.source_path = path

        o = raw.get("ollama", {}) or {}
        cfg.ollama_host = _expand(o.get("host", cfg.ollama_host)).rstrip("/")
        cfg.keep_alive = str(o.get("keep_alive", cfg.keep_alive))
        cfg.request_timeout = int(o.get("request_timeout", cfg.request_timeout))
        cfg.local_serial = bool(o.get("local_serial", cfg.local_serial))
        cfg.ollama_auto_start = bool(o.get("auto_start", cfg.ollama_auto_start))
        cfg.ollama_wait = int(o.get("auto_start_wait", cfg.ollama_wait))

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
                keep_alive=str(spec.get("keep_alive", "") or ""),
            )

        r = raw.get("routing", {}) or {}
        cfg.routing_default = r.get("default", "general")
        cfg.code_signals = _expand_signals(r.get("code_signals", []) or [])
        cfg.language_signals = _expand_signals(r.get("language_signals", []) or [])
        cfg.routing_capability = bool(r.get("capability", cfg.routing_capability))
        cfg.routing_quality_threshold = int(r.get("quality_threshold", cfg.routing_quality_threshold))
        cfg.routing_allow_upgrade = bool(r.get("allow_upgrade", cfg.routing_allow_upgrade))
        cfg.routing_allow_paid_upgrade = bool(r.get("allow_paid_upgrade", cfg.routing_allow_paid_upgrade))

        w = raw.get("workspace", {}) or {}
        cfg.workspace_root = Path(_expand(w.get("root", str(ROOT))))
        cfg.allow_outside = bool(w.get("allow_outside", False))
        gr = w.get("git_root")
        cfg.git_root = Path(_expand(gr)) if gr else None
        sd = w.get("state_dir")
        if sd:
            p = Path(_expand(sd))
            cfg.state_dir = p if p.is_absolute() else ROOT / p

        p = raw.get("permissions", {}) or {}
        cfg.require_approval = list(p.get("require_approval", []) or [])
        cfg.command_blocklist = [c.lower() for c in (p.get("command_blocklist", []) or [])]

        a = raw.get("agent", {}) or {}
        cfg.max_steps = int(a.get("max_steps", 40))
        cfg.max_tool_output_chars = int(a.get("max_tool_output_chars", 12000))
        cfg.keep_recent_tool_results = int(a.get("keep_recent_tool_results", 8))
        cfg.rounds_soft = int(a.get("rounds_soft", 30))
        cfg.rounds_hard = int(a.get("rounds_hard", 60))
        cfg.stream = bool(a.get("stream", True))
        cfg.memory_autonote = bool(a.get("memory_autonote", True))
        # 收尾验证闸门：任务书里声明了验证命令时，收尾由系统亲自运行并播报真实结果
        cfg.verify_gate = bool(a.get("verify_gate", True))
        # 一手来源核查：只用过搜索没抓过一手来源、总结里又有数字时，附「未经核实」声明
        cfg.sources_gate = bool(a.get("sources_gate", True))
        relay = str(a.get("relay", "brief" if cfg.relay_brief else "full")).strip().lower()
        cfg.relay_brief = relay != "full"

        d = a.get("dispatcher", {}) or {}
        cfg.dispatcher_enabled = bool(d.get("enabled", cfg.dispatcher_enabled))
        cfg.dispatcher_model = str(d.get("model", cfg.dispatcher_model))
        cfg.dispatcher_max_tasks = int(d.get("max_tasks", cfg.dispatcher_max_tasks))
        cfg.dispatcher_parallel = bool(d.get("parallel", cfg.dispatcher_parallel))
        cfg.dispatcher_translate = bool(d.get("translate", cfg.dispatcher_translate))
        cfg.dispatcher_bridge_threshold = int(
            d.get("bridge_threshold", cfg.dispatcher_bridge_threshold))
        cfg.dispatcher_execute_simple = bool(
            d.get("execute_simple", cfg.dispatcher_execute_simple))
        cfg.dispatcher_synthesis = bool(d.get("synthesis", cfg.dispatcher_synthesis))

        t = raw.get("tools", {}) or {}
        cfg.tools_lazy = bool(t.get("lazy", cfg.tools_lazy))
        cfg.tools_core = [str(x) for x in (t.get("core", []) or [])]
        cfg.tools_limit = int(t.get("limit", cfg.tools_limit))
        cfg.tools_min_score = int(t.get("min_score", cfg.tools_min_score))

        c = raw.get("compact", {}) or {}
        cfg.compact_threshold = int(c.get("threshold", cfg.compact_threshold))
        cfg.compact_keep_chars = int(c.get("keep_chars", cfg.compact_keep_chars))
        cfg.compact_summary_chars = int(c.get("summary_chars", cfg.compact_summary_chars))
        cfg.compact_auto = bool(c.get("auto", cfg.compact_auto))

        sk = raw.get("skills", {}) or {}
        cfg.skill_auto_load = bool(sk.get("auto_load", cfg.skill_auto_load))
        cfg.skill_min_score = int(sk.get("min_score", cfg.skill_min_score))
        cfg.skill_auto_max_chars = int(sk.get("auto_max_chars", cfg.skill_auto_max_chars))
        cfg.skill_dirs = [
            Path(_expand(d)) for d in (sk.get("dirs", []) or [])
        ]

        s = raw.get("session", {}) or {}
        cfg.session_dir = Path(_expand(s.get("dir", str(ROOT / ".sessions"))))
        if not cfg.session_dir.is_absolute():
            cfg.session_dir = ROOT / cfg.session_dir

        return cfg

    def state(self) -> Path:
        """运行期状态目录（长期记忆、上传附件）的唯一来源。

        此前有四处各自拼 `workspace_root / ".agentforge"`，改一处漏三处；
        统一到这里。默认落在工作区里，因为记忆是「这个工作区的记忆」，
        换个工作区就该换一份。
        """
        return self.state_dir or (Path(self.workspace_root) / ".agentforge")

    def model(self, key: str) -> ModelSpec:
        if key not in self.models:
            raise KeyError(f"未定义的模型 key: {key}，可选: {list(self.models)}")
        return self.models[key]

    def register_model(self, spec: ModelSpec) -> ModelSpec:
        """运行时注册模型（自动发现的本地模型、用户添加的付费模型都走这里）。"""
        self.models[spec.key] = spec
        return spec

    def unregister_model(self, key: str) -> None:
        self.models.pop(key, None)

    @staticmethod
    def remove_local_model_entry(path: str | Path, model_name: str) -> bool:
        """从配置文件里删除一个本地模型声明（按 name 匹配对应的 key）。返回是否删到。

        仅用于「移除本地模型」：把 config 里显式声明的本地模型条目也一并摘掉。
        否则下次 catalog() 又会按配置把它加回可用目录（显示成「未安装」），
        用户就会觉得「明明删了怎么还在」。找不到该模型时原样返回、不写文件。
        """
        path = Path(path)
        if not path.exists():
            return False
        try:
            raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            return False
        models = raw.get("models")
        if not isinstance(models, dict):
            return False
        hit = None
        for k, v in models.items():
            if isinstance(v, dict) and v.get("name") == model_name:
                hit = k
                break
        if hit is None:
            return False
        models.pop(hit)
        path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return True
