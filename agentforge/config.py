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
    # 算力闸门（任务级）：同时「执行中」的任务上限。本地显存一次只稳得住一个
    # 大模型就填 1；超出的任务登记为「排队中」，有空位自动开跑。填 2 表示
    # 跑着 A 的同时还能立刻开 B（显存够才行）。
    compute_max_running: int = 1
    # Ollama 没起来时自动拉起（见 agentforge/ollamaguard.py）。
    # 本机长期不关机，Ollama 可能被手动退出或升级时重启，本地模型会静默失效。
    ollama_auto_start: bool = True
    # 拉起后最多等多少秒才判定失败。冷启动要加载模型索引，别给太短。
    ollama_wait: int = 45
    # 开局健康门禁：任务启动前对每个本地模型做「最小生成探活」的最长等待秒数。
    # 期间对仍返回 5xx 的模型按退避重试；超时才判定该模型不可用并优雅中止任务，
    # 而不是硬冲进主流程被连环 500 拖垮（复测里 86 分钟里有大段是 Ollama 开局不稳
    # 时 Leader 双环空转）。给 0 关闭门禁。
    ollama_warmup_timeout: int = 90

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
    # ---- 按失败后果定档（swe-router 思路）----
    # 档位（planner.stakes_of）→ 该档模型综合分的下限。选型原则是
    # 「满足下限的最便宜模型」而不是「永远挑最强」：低风险任务不值得动用大模型。
    routing_stakes_tiers: dict = field(default_factory=lambda: {
        "low": 0, "medium": 78, "high": 86})

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
    # 本机事实核查（同一模块的第二路）：任务要的是「工作区里现在有什么」，模型却一次
    # 读取类工具都没调，答复里却列着具体文件名时——先给一轮修正机会去真看一眼，
    # 仍然没看就由程序在答复末尾追加「未经核实」声明。治「凭空编一份目录清单」。
    local_facts_gate: bool = True
    # 收尾自述核查（同一模块的第三路，见 verify.unfinished_claim）：模型在总结里
    # **自己承认**「下一步我将…」「我无法修改…」而本任务全程零文件改动时——先给一轮
    # 修正机会（现在就做，别把计划当交付），复查仍如此就在答复末尾如实标注「这一步
    # 没有交付」。治「两个指令都不执行，却回复得像已完成」。
    unfinished_gate: bool = True
    # 通用诚实守卫（agentforge/honesty_guard.py）：基于执行轨迹的证据比对，而非枚举能力。
    # 任务未达成却把锅甩给用户 / 假装做了 → 给一轮修正机会逼其如实说明。判定与任务类型 /
    # 数量无关，可扩展，替代旧 preflight 的硬编码能力信号表。
    honesty_gate: bool = True
    # 关键事实回显核查（verify.required_fact_tokens）：任务书里明确要求「保留 X /
    # 确保 Y / 写明 Z」时，提取其中的硬 token（引号内容、时间、日期、数字），
    # 汇总后程序比对答复是否包含；缺了给一轮修正，仍缺则附系统侧声明。
    # 治 w3 类「文件写对了、回复把关键事实丢了」。
    facts_gate: bool = True

    # ---- 多 agent 调度（Dispatcher）----
    # 由一个本地决策模型把请求拆成【语义独立】的任务、指派执行 agent、维护共享进度，
    # 任务在 agent 之间接力流转。模型不可用或输出无法解析时自动回退到规则拆解。
    dispatcher_enabled: bool = True
    # 决策/调度模型。默认 Qwen3-14B：拆解与交接能力强、中文好，Q4 量化约 9GB，
    # 16GB 显存能跑（32B 的 Q4 约 20GB，16GB 卡放不下）。可在「可用模型管理」里
    # 换成任意本地或在线模型（留空=关闭调度，回退到规则拆解）。
    dispatcher_model: str = "qwen3.5:4b"
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

    # ---- Leader 双环调度（第 47 步：七步架构更新）----
    # 评估环轮次上限（规划→执行→判定为 1 轮）。Leader 上下文有固定槽位保护、
    # 头部字节稳定可吃服务端缓存，轮次成本远低于员工执行轮，可以给多。
    leader_max_rounds: int = 12
    # 步骤⑤语义验收未通过后的整体重试次数（失败原因回灌重新规划）。
    # 0 = 语义验收不过也直接放行（附系统侧声明）。
    leader_max_retries: int = 2
    # 单步失败去重上限：同一小步骤（按归一化目标去重）在评估环里最多被重排执行
    # leader_step_retry_cap 次，超过即标记为「已放弃」、不再让 Leader 把它无限重排进
    # 后续批次。复测里 Ollama 开局不稳时，一批 500 的分析步会被 R2–R5 反复重排，
    # 纯属烧时长；2 次足够暴露「确实做不动」，再重排就是抖动。
    leader_step_retry_cap: int = 2

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
        # 本地模型串行执行已升级为**硬规则**（第 42 步），不再可配置：同一时刻
        # 只跑一个本地模型，避免并发任务互相抢显存。config 里残留的
        # local_serial 键会被忽略（写 False 也无效），界面上的开关已移除。
        cfg.local_serial = True
        cfg.ollama_auto_start = bool(o.get("auto_start", cfg.ollama_auto_start))
        cfg.ollama_wait = int(o.get("auto_start_wait", cfg.ollama_wait))
        cfg.ollama_warmup_timeout = int(o.get("warmup_timeout", cfg.ollama_warmup_timeout))
        # 容器/云端部署：环境变量优先于配置文件——镜像里的 config.yaml 不用改，
        # 换个上游（云服务器上的 Ollama 容器 / 局域网机器）只动 compose 的 env。
        # auto_start 置假时 ollamaguard.ensure 只探测不拉起，容器里没有
        # Ollama 可执行文件也不会试图本机启动。
        if env_host := os.environ.get("AGENTFORGE_OLLAMA_HOST", "").strip():
            cfg.ollama_host = env_host.rstrip("/")
        if env_auto := os.environ.get("AGENTFORGE_OLLAMA_AUTO_START", "").strip():
            cfg.ollama_auto_start = env_auto.lower() not in ("0", "false", "no", "off")

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
        st = r.get("stakes_tiers") or {}
        if isinstance(st, dict) and st:
            cfg.routing_stakes_tiers = {str(k): int(v) for k, v in st.items()
                                        if str(k) in ("low", "medium", "high")} \
                or cfg.routing_stakes_tiers

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
        # 本机事实核查：要列工作区现状却没读过工作区时，先修正一轮、仍然没看就附声明
        cfg.local_facts_gate = bool(a.get("local_facts_gate", True))
        # 收尾自述核查：模型自认「下一步我将…/我无法修改…」且零文件改动时，先修正一轮
        cfg.unfinished_gate = bool(a.get("unfinished_gate", True))
        # 通用诚实守卫：基于执行轨迹证据比对，任务未达成却甩锅时逼其坦白
        cfg.honesty_gate = bool(a.get("honesty_gate", True))
        # 关键事实回显核查：任务书点名「保留 X / 确保 Y」时，答复缺了先修正一轮
        cfg.facts_gate = bool(a.get("facts_gate", True))
        relay = str(a.get("relay", "brief" if cfg.relay_brief else "full")).strip().lower()
        cfg.relay_brief = relay != "full"

        d = a.get("dispatcher", {}) or {}
        cfg.dispatcher_enabled = bool(d.get("enabled", cfg.dispatcher_enabled))
        cfg.dispatcher_model = str(d.get("model", cfg.dispatcher_model))
        cfg.dispatcher_parallel = bool(d.get("parallel", cfg.dispatcher_parallel))
        cfg.dispatcher_translate = bool(d.get("translate", cfg.dispatcher_translate))
        cfg.dispatcher_bridge_threshold = int(
            d.get("bridge_threshold", cfg.dispatcher_bridge_threshold))
        cfg.dispatcher_execute_simple = bool(
            d.get("execute_simple", cfg.dispatcher_execute_simple))

        # ---- Leader 双环调度（第 47 步：七步架构更新）----
        # 评估环：每轮「规划→执行→判定+规划+精简」算一轮，轮次可以给多——
        # 员工的执行才是 token 大头，Leader 自身的上下文有固定槽位保护不膨胀。
        ld = raw.get("leader", {}) or {}
        cfg.leader_max_rounds = max(1, int(ld.get("max_rounds", cfg.leader_max_rounds)))
        # 失败重试：语义验收未通过后带着失败原因重新规划的整体重试次数。
        # 2 与任务级闸门的「至多三次执行」约定同口径。
        cfg.leader_max_retries = max(0, int(ld.get("max_retries", cfg.leader_max_retries)))
        cfg.leader_step_retry_cap = max(1, int(ld.get("step_retry_cap", cfg.leader_step_retry_cap)))

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

        cp = raw.get("compute", {}) or {}
        cfg.compute_max_running = max(1, int(cp.get("max_running", cfg.compute_max_running)))

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
