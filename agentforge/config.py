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
    # 惩罚回看窗口（token 数）。Ollama 默认 64 只覆盖 ~50 词，抓不到段落级
    # 复读循环；生态建议代码/长文场景 128-512。0 = 不下发（用 Ollama 默认 64）。
    repeat_last_n: int = 0
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
        if self.repeat_last_n and self.repeat_last_n > 0:
            opts["repeat_last_n"] = self.repeat_last_n
        if self.num_predict and self.num_predict > 0:
            opts["num_predict"] = self.num_predict
        return opts


@dataclass
class Config:
    ollama_host: str = "http://localhost:11434"
    keep_alive: str = "15m"
    request_timeout: int = 900
    # 流式读取的空闲上限（秒）：多久没有收到任何新字节就认定端侧卡住（P8）。
    # 与 request_timeout 分工不同：后者是总上限/连接，前者治"不回包"。
    # 180s 是给 12B 本地模型的长提示首字延迟留的余量；在线模型可调到 60。
    stream_idle_timeout: int = 180
    # 本地模型串行执行：同一时刻只跑一个本地模型，避免两个并发任务同时压 Ollama
    # 抢 GPU/显存互相拖累。付费/在线模型不受此限制，可并行。
    local_serial: bool = True
    # 本地模型批内并发上限（取代 local_serial 的粗开关）：leader 同批多个员工
    # 步骤同时执行时，本地模型调用最多允许 N 个并发（1 = 完全串行，等价旧的
    # local_serial=true）。收到 Ollama 侧的 OLLAMA_NUM_PARALLEL 与显存余量约束：
    # 同一模型的并发请求由 Ollama 合批（权重共享，KV cache 各自增长）；不同模型
    # 并发则各自占显存。16GB 卡建议 2，且只对小模型（≤8B）受益明显。
    local_max_concurrent: int = 1
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
    # 未交付硬闸（verify.delivery_failed_block）：任务书点名了交付物、复核后仍不存在，
    # 且整条轨迹**零写类动作**——这不是「写错地方」，是压根没动手。以前只附一段声明
    # 就放行，模型连写 7 轮「下一步计划」照样判成功（rerun26）。开启后由程序给出终态，
    # 任务标记为未完成。只认「一次都没写过」这一种确定情形，不误伤「写了但没到位」。
    delivery_hard_gate: bool = True
    # 会话事件日志（journal.py）：把本次 run 的模型可见消息、系统提示变化、
    # 每次压缩的前后快照落盘到 <state>/journal/<sid>/。这是「模型看到什么」
    # 可被程序重放的唯一依据，也是评测归因的前提。关掉只会少记账，不影响任务。
    journal: bool = True
    # 写前策略（writepolicy.py）：写类工具执行**之前**先过一遍意图检查——
    # 交付物必须建立在真实读过的证据上，缺失时直接给「先读哪些文件」的清单，
    # 而不是等整篇写完再由引用核查打回重写。拒绝时内容存入 .drafts/ 供局部修正。
    prewrite_gate: bool = True
    # 通用诚实守卫（agentforge/honesty_guard.py）：基于执行轨迹的证据比对，而非枚举能力。
    # 任务未达成却把锅甩给用户 / 假装做了 → 给一轮修正机会逼其如实说明。判定与任务类型 /
    # 数量无关，可扩展，替代旧 preflight 的硬编码能力信号表。
    honesty_gate: bool = True
    # 关键事实回显核查（verify.required_fact_tokens）：任务书里明确要求「保留 X /
    # 确保 Y / 写明 Z」时，提取其中的硬 token（引号内容、时间、日期、数字），
    # 汇总后程序比对答复是否包含；缺了给一轮修正，仍缺则附系统侧声明。
    # 已停用（默认 False）：信号词匹配太脆弱——「必须」这类词会碰巧落在专有名词里
    # （如游戏名「忍者必须死3」）触发「必须回显 3」的假阳性。需要时把默认值改回 True。
    facts_gate: bool = False

    # ---- 工具按需下发（省固定开销，见 agentforge/toolselect.py）----
    tools_lazy: bool = True            # True = 只下发核心 + 关键词命中的工具
    tools_core: list[str] = field(default_factory=list)   # 常驻工具名，空则用内置清单
    tools_limit: int = 16              # 单轮最多下发几个 schema
    tools_min_score: int = 2           # 关键词命中的最低分
    ptc_resident: bool = True           # True = 所有工具调用强制走 ops_batch 脚本（PTC 常驻）

    # ---- 澄清弹窗判定（docs/clarify-policy.md §12，试行版 v0）----
    # on = 判到就弹；shadow = 只记 clarify_decision 事件不弹（上线前影子期）；
    # off = 全关（回到「什么都不问」）。阈值集中在 agentforge/clarify.py 的 PARAMS
    # ——全部是占位值，待影子期实测后统一改。
    clarify_mode: str = "on"

    # ---- 核查打满后的「程序补查」（agentforge/autocheck.py）----
    # 答复核查打回 2 轮仍不通过时，先由程序自己去抓一手源（能抓的自己抓），
    # 抓不到才在交付物上标注「未经核实」。这是单场任务最多抓几个源（含 1 次检索）。
    autocheck_max_fetch: int = 3

    # ---- 上下文压缩 ----
    compact_threshold: int = 60_000      # 超过这个字符数就触发压缩
    compact_keep_chars: int = 20_000     # 压缩后至少保留的近期内容
    compact_summary_chars: int = 1500    # 摘要的目标长度
    compact_auto: bool = True            # 是否自动压缩
    compact_keep_recent: int = 8         # T3：每轮只保留最近 N 条完整工具结果，更老的压成一行

    # ---- 编排模式（T2，docs/changes/token-economy-20261019.md）----
    # auto = 极简单循环是默认路径，只有用户明确要并行才拆多 agent；
    # always = 旧行为（多动作/长文即拆，评测对照用）；never = 永不拆。
    orchestration: str = "auto"

    # ---- 任务开局预热注入（agentforge/warmup.py）----
    # 任务开局扫一遍工作区，把「目录树 + README 摘要 + 任务关键词命中文件」
    # 注进每个员工的系统提示，省掉开局摸黑的 1~3 轮探索调用。注入位置在
    # 系统提示（免压缩区），_compact 永不碰它。体量很小：max_chars=3500 字
    # ≈ 1~2K token，占 gemma 256K 窗口 <1%、64K 模型约 3%。
    warmup_enabled: bool = True
    warmup_max_chars: int = 3500         # 注入块字符上限（超出截断）
    warmup_max_files: int = 12           # 关键词命中文件清单的条目数

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
        cfg.stream_idle_timeout = max(10, int(o.get("stream_idle_timeout",
                                                   cfg.stream_idle_timeout)))
        # 本地模型串行执行已升级为**硬规则**（第 42 步），local_serial 键被忽略。
        # 细粒度并发由 local_max_concurrent 接管（1 = 完全串行，等价旧硬规则；
        # >1 时 leader 同批员工步骤允许 N 路本地并发，显存自负）。
        cfg.local_serial = True
        cfg.local_max_concurrent = max(1, int(o.get("local_max_concurrent",
                                                    cfg.local_max_concurrent) or 1))
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
                repeat_last_n=int(spec.get("repeat_last_n", 0)),
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
        # （已停用：信号词匹配脆弱，见字段定义；默认 False，配置里可显式打开）
        cfg.facts_gate = bool(a.get("facts_gate", False))
        relay = str(a.get("relay", "brief" if cfg.relay_brief else "full")).strip().lower()
        cfg.relay_brief = relay != "full"

        t = raw.get("tools", {}) or {}
        cfg.tools_lazy = bool(t.get("lazy", cfg.tools_lazy))
        cfg.tools_core = [str(x) for x in (t.get("core", []) or [])]
        cfg.tools_limit = int(t.get("limit", cfg.tools_limit))
        cfg.tools_min_score = int(t.get("min_score", cfg.tools_min_score))
        cfg.ptc_resident = bool(t.get("ptc_resident", cfg.ptc_resident))
        cfg.clarify_mode = str(t.get("clarify_mode", cfg.clarify_mode) or "on").lower()
        cfg.autocheck_max_fetch = max(0, int(t.get("autocheck_max_fetch",
                                                  cfg.autocheck_max_fetch)))

        c = raw.get("compact", {}) or {}
        cfg.compact_threshold = int(c.get("threshold", cfg.compact_threshold))
        cfg.compact_keep_chars = int(c.get("keep_chars", cfg.compact_keep_chars))
        cfg.compact_summary_chars = int(c.get("summary_chars", cfg.compact_summary_chars))
        cfg.compact_auto = bool(c.get("auto", cfg.compact_auto))
        cfg.compact_keep_recent = max(2, int(c.get("keep_recent", cfg.compact_keep_recent)))

        cfg.orchestration = str(raw.get("orchestration", cfg.orchestration)).lower()

        wu = raw.get("warmup", {}) or {}
        cfg.warmup_enabled = bool(wu.get("enabled", cfg.warmup_enabled))
        cfg.warmup_max_chars = max(500, int(wu.get("max_chars", cfg.warmup_max_chars)))
        cfg.warmup_max_files = max(0, int(wu.get("max_files", cfg.warmup_max_files)))

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
