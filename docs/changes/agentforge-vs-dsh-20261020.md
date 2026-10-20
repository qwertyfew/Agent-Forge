# AgentForge 与 DeepSeek Harness（dsh）架构对照

（2026-10-20。对照对象：dsh v0.1 开发者预览，Cordis「一切皆插件」；AgentForge 当前 main。）

## 一句话结论

**两者都长成了「事件日志 + 上下文视图 + 单循环」的形状，差别在于扩展方式和信任模型**：
dsh 把每个环节做成可替换的插件 seam，扩展靠事件瀑布，信任建立在「日志可追溯」上；
AgentForge 把每个环节做成可核查的程序分支，扩展靠改主循环，信任建立在「程序对账、不听模型自述」上。

AgentForge 的 `tasksession.py` 就是照 dsh 的 Session 三段式写的，`steering.py` 明写「Inbox 三通道之一」——
**它是 dsh 的裁剪版：少一半可替换性，多一层交付裁定。**

---

## 一、Agent Loop：Turn / Step

| | dsh | AgentForge |
|---|---|---|
| 层级 | 两层：**Turn**（一次用户任务）⊃ **Step**（一次模型决策 + 该决策要求的工具） | **一层**：`loop.py` 的 `for step in range(1, max_steps+1)`；一次 `Agent.run` 隐含等于一个 Turn，但没有 turn 对象、没有 turn 边界事件 |
| Turn 关闭条件 | 不欠工作：本轮无工具调用 **且** Inbox 无待读的 Steering/Inject | `if not result.tool_calls` → 契约闸门通过 → 产出 assistant 即返回 |
| Step 继续条件 | 执行过工具，**或** Inbox 有 Steering/Inject | 有工具调用，**或** 系统回灌后 `continue`（空轮次提醒、契约闸门、轮次预算） |
| 驱动方式 | React 范式：输入进 Inbox，事件驱动唤醒 | `Agent.run` 同步迭代器，调用方 `yield from` 消费事件 |
| 可替换性 | Agent 循环本身是插件（`ctx.agentLoop`），整条循环可换 | 循环写死在 `Agent.run` 里，换策略要改代码 |

**实质差异**：AgentForge 没有 turn 边界，所以：

- 同一个会话里「再追加一件事」会开一次新的 `Agent.run`：对话历史（`session.py` 的 JSONL）是延续的，但 turn 的「欠工作」状态、契约清单、闸门计数全部归零 —— 相当于新任务借用了旧上下文，而不是同一 Turn 的续接；
- 一个 Turn 内的「欠工作」判定没有统一出口：工具调用走正常路径，闸门回灌走 `continue`，两者在代码里是两处 `if`。

## 二、Inbox 三通道

| 通道 | dsh 语义 | AgentForge |
|---|---|---|
| **Follow-up** | 新 Turn 处理，空闲时唤醒 | **缺失**。`/api/steer` 只收运行中任务，非运行中回落成普通新消息（`steering.submit` 返回 False） |
| **Steering** | 当前 Turn 的下一个 Step 立即采纳 | **有**，`steering.py`：两个消费点（员工每轮推理前 `steer_check`、leader 每批规划前），带 `seen` 集合让并行批次各见一次 |
| **Inject** | 插件补信息，不唤醒空闲 Agent，下个 Step 前注入 | **只有隐式形态**：守卫/契约闸门/verify 的缺口直接 `_push({"role":"user", ...})`，不走收件箱、不区分来源、不排队 |

所以用户那份描述里的三通道，AgentForge 实际是「1 个真通道 + 2 处硬编码回灌」。
`docs/changes/harness-redesign-20261019.md` 的 Phase 2 第 2 条（Inbox 三通道收尾）已把 Follow-up 与 Inject 队列列为待办，至今未做。

## 三、Step 四环节

| 环节 | dsh | AgentForge |
|---|---|---|
| 组装上下文 | `turn/start` → `agent/pre-step`（waterfall，可改写/拒绝输入）→ 组装系统提示 + 工具 schema + 环境 + 历史 + Inbox | `_active_tools(ctx)` → `messages_with_system(ctx)` → `_compact()` → 追加 `LANG_REMINDER` → 轮次预算提示 |
| 调模型 | `agent/request` → `prepareCall()` 解析路由 → 提交系统提示与用户消息 → `llm/stream` | `self.hub.chat_stream(spec, messages, tools)`，多 provider 分发；失败即 `yield error` 退出本轮 |
| 处理响应 | 流式组装成完整 message 落日志；失败按插件策略重试；`assistant/attempt` 保留失败尝试 | 流式 delta/thinking 直出；`parse_text_tool_calls` 抢救正文式调用；空轮次最多重试 2 次 |
| 执行工具 | `tool/call` → `tools/pre-execute`（waterfall：hooks/权限/沙箱）→ 单调 guards → `tools/execute` → `tools/post-execute`（可改写/拦截/加上下文）→ `tool/result` 冻结 | 逐个执行：`Guard` 路径判定 → 只读缓存 → 工具体 → `echo_lines` 权威回显 → tool 消息回填；写类额外跑引用核查与语义闸门 |

**顺序一样，机制相反**：dsh 在四个环节之间插了 6 个 waterfall 事件，任何策略都是插件挂上去的；AgentForge 的策略是主循环里的 `if`（契约闸门、空轮次、预算、缓存、PTC 常驻、停止检查）。
前者利于「换产品形态」，后者利于「把某条底线钉死」。

## 四、Session：Header / Event Log / Surface

| 层 | dsh | AgentForge |
|---|---|---|
| **Header** | 独立对象：会话 ID、创建时间、工作目录等元数据 | **没有独立对象**。`sid` + `.sessions/<sid>/` 目录名 + taskstore 里的任务记录，散着放 |
| **Event Log** | 不可变 JSONL，每个事件 `type + seq + 时间戳`，只追加；`deriveMessages()` 从中投影模型历史；fork/恢复/重放/搜索同一条流 | `tasksession.py`：`.sessions/<sid>/events.jsonl` 追加写，注释即「唯一权威来源，员工会撒谎、日志不会」；事件类型 `step_start / step_end / ...` |
| **Surface** | 内存视图，**可被替换**：压缩时把摘要作为新事件追加，再用它替换 Surface 里的 5 条旧事件；日志里 5 条原样保留 | 从事件列表重建的视图（`worker_brief` / `auto_answer` 取材），但**没有 replacement 语义** |
| **压缩落点** | 压缩本身是事件（可重放「模型当时看到什么」） | `context.Compressor` 直接改内存 `messages` 列表，**日志里没有压缩事件** |

这条是本轮最值得修的一处：dsh 的「模型可见即已记录」在 AgentForge 上是断的。
现状是「模型看到什么」只在内存里成立，一旦压缩发生，日志就无法精确还原当轮 prompt —— 评测复盘（`evals/rerunN`）看到的是压缩后的结果，原始证据链缺一环。

## 五、工具层

| | dsh | AgentForge |
|---|---|---|
| 注册 | `ctx.tools` 作用域化注册表，schema 进提示词组装 | `registry` 40 个工具 + `toolselect` 按任务词打分动态裁剪；PTC 常驻时只下发 3 个（`ops_batch`/`ask_user`/`find_tools`） |
| 放行 | `tools/pre-execute` + 单调 guard（deny 或弃权）+ 一次性审批 `ctx.approval`（无人应答即拒绝） | `Guard`：路径沙箱、写白名单、产出目录重定向；`auto_approve` 按任务传，不动全局开关 |
| 写入前 | `fs/write-intent` / `fs/edit-intent` —— **写之前**问策略 | 只有路径层判定（能不能写这个路径），**内容层**核查在写之后 |
| 结果 | `projectContent` / `finalizeContent` / `tools/result` 冻结权威结果 + `additionalContexts` FIFO | `echo_lines` + 「【系统核对·写盘结果】」块压末尾，明示它不是脚本里的 print |

**方向不同**：dsh 事前拦（省时间），AgentForge 事后对账（抓得住「写对了路径、写错了内容」）。
代价是返工：rerun23 里模型被引用核查连拒 4 次，每次整篇重写 —— 这正是「生产时就做好」比「写后返工」贵的地方。

## 六、交付与诚实层：AgentForge 独有的一层

dsh 对「交付」只做**记录与展示**：`deliverables` 组用 `present` 工具声明交付文件、用 git 快照记录改动并渲染 diff；`goal` 组管持久目标与自动续行；`guard` 组只管循环卫生（重复调用提醒、单次工具超时）。
**没有一处裁定「任务书点名的文件到底存不存在」。**

AgentForge 把这一层做成了硬约束：

| 闸门 | 时机 | 依据 |
|---|---|---|
| 契约闸门 | 模型宣布收工、`continue` 之前 | 磁盘 mtime 晚于本轮开始，最多拦 2 次 |
| 收尾 verify | 任务结束 | 产物 mtime 快照、验证命令实跑、引用核查（read 台账 + 行号）、关键事实核查 |
| 语义闸门 | `write_file` 落盘前 | 「否定词 + 功能名」→ 到代码里找 `class/def` 定义行，命中即拒写并回传真实 `path:line` |
| 未交付硬闸 | 收尾 | 声明交付物 + 复核全 missing + 全程零写类动作 → `delivery_failed`，任务状态 `error` |

这层的存在理由在 dsh 里找不到对应物 —— 因为 dsh 默认模型能力足够、且用户可读 diff；AgentForge 面向本地 12B 模型，谎报是常态（rerun23 七次宣称已写入、文件不存在）。

## 七、多智能体与 PTC

| | dsh | AgentForge |
|---|---|---|
| 子代理 | `subagent` 是 provider seam，同一接口后可接子 agent、甚至另一个产品；Agent Teams 实验（持久 roster + 任务状态 + inbox） | `leader` + `planner` 拆步、线程池按依赖波次并发、泳道冲突检测、steering 广播、`relay` 接力（只带计划/摘要/路径省 token） |
| 默认路径 | 单 Agent + Step 循环 | `_orchestration_wanted` 判定：默认单 Agent，真并行才走 leader |
| PTC | **运行模式**（Standard / PTC / Minimal / Creator），TypeScript `run_code` + PTC SDK，PTC 模式下移除了通用 workflow 工具 | **工具面开关**（`ptc_resident`），Python 脚本 `call('工具名', ...)`，权限/台账全继承 |

同一种思想（多步工具编排压成一次程序），不同载体：dsh 用 TS 运行时，AgentForge 用 Python 沙箱 + 既有 Guard。

## 八、工程形态

| | dsh | AgentForge |
|---|---|---|
| 技术栈 | TypeScript / Node 22+，pnpm monorepo（`packages` / `apps` / `native` / `python`） | Python 3.13，单进程 + threading，约 2.1 万行（含 `webui.py`） |
| 组装 | profile + 组合包 + `cordis.patch.yml` 逐层叠加，HMR 热重载，`--dump-config` 可查整棵树 | `config.yaml` / `config.lanrun.yaml` 两文件，重启生效（部分字段每次请求重读） |
| 扩展 | 159 个内置插件 + 5627 社区插件，MCP / SKILL.md 兼容 | `agentforge/plugins/*.py` 暴露 `build_tools`；40 个内置工具；技能走 `skills/` + SKILL.md |
| 形态 | Web（3080）/ headless / TS+Python SDK / ACP / Electron 桌面 | 自研 WebUI（SSE）+ CLI + 评测台（`evals/` + `debug/dispatch_task.py`） |
| 模型 | 云为主，provider 插件 | 本地 Ollama 为主（`gemma4:12b` 执行 / `gpt-oss:20b` 调度）+ 在线 provider，`ollamaguard` 自动拉起与 keep_alive |

---

## 九、五处会真实影响行为的差距

1. **没有 turn 边界** → 无法续 turn，追加任务等于重开，上下文靠摘要而非延续。
2. **「模型可见即已记录」断裂** → 压缩只改内存，事后无法重放当轮 prompt，评测归因失真。
3. **扩展点靠 if 分支** → 改一条策略要动主循环（本轮就动了 4 处），易引入回归。
4. **只有写后内容核查** → 被拒 = 整篇重写，而 dsh 是写前问策略。
5. **工具面靠启发式打分** → 选错就直接缺能力，`find_tools` 兜底在 rerun22 白烧 4 轮。

## 十、AgentForge 比 dsh 多做的（dsh 没有对应物）

1. 交付裁定闭环：契约闸门 / 未交付硬闸 / mtime 对账 —— dsh 只记录不裁定。
2. 语义闸门：把「模型声称缺乏某功能」和代码里的真实定义对撞。
3. 引用核查：read 台账 + 行号门槛，防虚构路径落盘。
4. 评测台与耗时拆解：`llm_ms / eval_ms / load_ms / prompt_eval_ms` + 工具 `dur_ms`，能定位到「90% 时间在 LLM」。
5. 本地模型工程：进程守卫、keep_alive、num_ctx 调优、模型池与能力路由。

## 十一、建议吸收的三件事（按性价比排序）

1. **压缩也写事件**（低成本、直接补评测盲区）：`compact` 事件记录 before 事件区间 + 摘要引用，Surface 支持替换语义。补上这条，「模型当时看到什么」才可重放。
2. **写前内容检查**（中成本、直接省返工）：把引用核查与语义核查从 `write_file` 落盘后，前移到 `ops_batch` 脚本执行前对源码做一次静态扫描 —— 被拒时模型只改一段脚本，不用整篇重写。
3. **显式 turn + Follow-up 队列**（中成本、补语义缺口）：`run` 返回后保留现场，同会话追加走新 Turn 而非新任务；Inject 从「直接 push」改为排队消费。

**不建议照搬**：Cordis 微内核与插件市场（单机规模不匹配，工具扩展有 MCP 与 `plugins/` 已够）；TS 重写 PTC（Python 沙箱已达成同等效果，且保住了权限与台账继承）。
