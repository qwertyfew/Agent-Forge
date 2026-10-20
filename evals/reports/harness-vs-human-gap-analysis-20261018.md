# Harness 任务结果审计 + 人工同任务复做对比（2026-10-18）

> 审计对象：AgentForge harness（REA，rerun6，2026-10-18 18:11）产出的
> `evals/external-harness-gap-list-20261016.md`
> 任务原文：`.run/rea_task_prompt.txt`（逆向分析 mini-swe-agent 与 learn-claude-code，产出与 AgentForge 的逐项差距清单）
> 审计方法：逐条回读两份外部仓库源码 + AgentForge 后端源码核实 harness 的引用与结论；再由我独立复做同一任务，对比。

---

## 一、结论：harness 的结果「正确吗」

| 维度 | 判定 | 说明 |
|---|---|---|
| 交付物格式合规 | ✅ 正确 | 4 列差距表（对比点 / 对方做法 / AgentForge 现状 / 借鉴建议）+ 末尾「按性价比排序的借鉴清单」（5 条），覆盖任务要求的全部 10 个对比点。 |
| 机制描述准确性 | ⚠️ 大体准确 | 我对 7 个 learn-claude-code 会话（s01/s03/s06/s07/s08/s09/s10）+ mini-swe-agent 实景核对，harness 对各机制（while 循环、三道权限闸、子代理隔离、ContextCompactor、TaskStore、SkillLoader、MemoryStore）的**文字描述与源码一致**。 |
| 证据纪律（任务硬性要求） | ❌ 有硬伤 | 任务明确规定「每条结论必须注明证据来源（文件路径+行号）」「禁止凭印象写」。harness 的引用大量断裂，读者按引用找不到文件。 |
| 分析判断准确性 | ⚠️ 2 处失真 | ① 低估 AgentForge 的子代理能力；② 高估 mini-swe-agent 的 SWE-Bench 文档。 |

**一句话结论**：harness 把任务「做完了」且主体判断靠谱，但**证据引用系统性错误**（违反任务最硬的纪律要求），另有一处低估、一处高估。作为「可复现的分析」它不合格；作为「方向正确的差距清单」它基本可用。

---

## 二、harness 输出逐条审计

### 2.1 引用正确性（最严重的问题）

| harness 原文引用 | 真实位置 | 判定 |
|---|---|---|
| `learn_claude_code/s01_agent_loop/code.py:68-76, 96-125` | `learn-claude-code/s01_agent_loop/code.py` | ❌ 路径错（下划线→连字符），行号内容对 |
| `learn_claude_code/s08_context_compact/code.py` | `learn-claude-code/s08_context_compact/code.py` | ❌ 路径错 |
| `learn_claude_code/s10_task_system/` | `learn-claude-code/s10_task_system/` | ❌ 路径错 |
| `learn_claude_code/s06_subagent/code.py` | `learn-claude-code/s06_subagent/code.py` | ❌ 路径错 |
| `learn_claude_code/s03_permission/code.py` | `learn-claude-code/s03_permission/code.py` | ❌ 路径错 |
| `learn_claude_code/s09_memory/` | `learn-claude-code/s09_memory/` | ❌ 路径错 |
| `learn_claude_code/s07_skill_loading/` | `learn-claude-code/s07_skill_loading/` | ❌ 路径错 |
| `agentforge/tools.py` | `agentforge/tools/`（包目录，无 tools.py） | ❌ 文件不存在 |
| `agentforge/loop.py:96-124`（引为「Orchestrator 模式」） | `loop.py:96-124` 实为 `SYSTEM_PROMPT_TEMPLATE` 提示词文本 | ❌ 行号指向错（非编排代码） |
| `mini-swe-agent/src/minisweagent/agents/default.py:88-124` | 同（run() 的 while 循环在 96） | ✅ 正确 |
| `mini-swe-agent/src/minisweagent/agents/default.py:159-182` | 同（serialize 159-180 / save 182-190） | ✅ 正确 |
| `agentforge/context.py` | 同（Compressor 类在 190） | ✅ 正确 |
| `agentforge/permissions.py` | 同（Guard 类在 36） | ✅ 正确 |
| `agentforge/taskstore.py` | 同（Task 99 / TaskStore 142） | ✅ 正确 |
| `agentforge/loop.py:365-368` | 同（memory.prompt_block() 注入） | ✅ 正确 |

**影响**：任务书把「证据优先一手来源、引用文件路径+行号」列为硬性纪律。harness 的 9 处坏引用里有 7 处是同一类错（`learn_claude_code` 应为 `learn-claude-code`）——典型「凭印象拼路径」而非回读核实，正好踩中任务书禁止的行为。

### 2.2 分析判断准确性

| 行 | harness 结论 | 实际 | 判定 |
|---|---|---|---|
| 子代理与隔离 | 「AgentForge 目前主要基于单 Agent 架构，缺乏标准化的子代理分发机制」 | AgentForge 有 `dispatcher.py:199 class Dispatcher` + `orchestrator.py` + `leader.py:119 class LeaderContext` 的 **leader→worker 派发**；本次 rerun6 任务正是 leader 派 21 个子任务给 worker 执行。另有 `router.py:21 class Router` 按能力/后果路由到不同模型。 | ❌ 低估——已有 leader-worker 架构，非单 Agent |
| 评测体系 | 「mini-swe-agent 在 docs 中详细记录针对 SWE-Bench 的验证结果和取舍逻辑」 | 仓库内仅 `docs/faq.md:26` 一句 "Excellent performance on SWE-Bench"，无详细结果/取舍文档。任务书背景里的「74%+」在仓库内找不到数字出处。 | ❌ 高估——文档仅一句带过 |
| 会话恢复 | 「提供中断后的状态序列化与加载机制（…JSON 序列化整个 Agent 状态）」 | `default.py:159-190` 确有 `serialize()`+`save()`，且每步 `finally` 调 `save` 写 JSON 轨迹（行 120-121）。但 `default.py` 内**未见到显式 load/resume 入口**，"加载"说法未在本次读到的文件内证实。 | ⚠️ 部分夸大（序列化在，显式加载入口未证实） |

---

## 三、我独立复做同一任务的差距清单（正确引用版）

下面这份是我回读两份外部仓库 + AgentForge 后端源码后，按任务书格式独立产出的版本。所有引用均已核实存在。

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
|---|---|---|---|
| agent loop 结构 | mini-swe-agent：`run()` 内 `while True: self.step()`（`external/mini-swe-agent/src/minisweagent/agents/default.py:88-124`），`query()` 强制 step/cost/wall_time 上限（132-147）。learn-claude-code：`agent_loop()` `while True` 调 `LLM(tools)` 直到无 tool_use（`external/learn-claude-code/s01_agent_loop/code.py:96-127`）。 | `agentforge/loop.py:277 class Agent`（`run()` 在 455，可迭代、yield 事件）；上层是 leader-worker 双环：`dispatcher.py:199 class Dispatcher`、`leader.py:119 class LeaderContext`、`orchestrator.py`。 | 保持。AgentForge 的编排更生产化；mini 的极简只适用单场景。 |
| 工具调用方式 | `s01_agent_loop/code.py:67-76` 标准化 input_schema；`96-127` 循环解析 tool_use 执行。`s10_task_system/code.py:395-432` 多工具分发表。 | `agentforge/tools/` 包：`base.py:11 class Tool`、`__init__.py:46 class ToolRegistry`；`toolselect.py:99 class ToolSelector` 懒加载工具索引（`loop.py:360-364`）。 | 保持 Registry+懒加载；可借鉴对方的「纯循环」思想简化某些脚本流。 |
| 上下文压缩策略 | `s08_context_compact/code.py:254-527` `ContextCompactor`：tool_result_budget→snip_compact→micro_compact→fit_tool_results→compact_history；大结果落盘 `.task_outputs/tool-results/`（`save_output` 340-345，`persisted_preview` 347-361）。 | `agentforge/context.py:190 class Compressor` 按权重/多策略动态压缩（`loop.py` 内接入）。 | **借鉴**：给 Compressor 加「大结果文件化持久化」，减少 token 浪费、提升长结果注意力。 |
| TODO/任务清单机制 | `s10_task_system/code.py:74-199` `Task` dataclass + `TaskStore`，状态机 pending→in_progress→completed，`blockedBy` 依赖图，`claim_task`/`complete_task`（240-279）。 | `agentforge/taskstore.py:99 class Task` + `:142 class TaskStore` 提供持久化，但未见它作为任务流状态机驱动 agent loop。 | **高优先借鉴**：把 taskstore 接入 orchestrator/loop，给长链路任务明确的 pending/in_progress/completed + 依赖。 |
| 子代理与上下文隔离 | `s06_subagent/code.py:276-313` `run_subagent()` 用**全新 messages** 跑独立 loop，只回传 final text；子代理无 task 工具（不可二次委派）。 | 已有 leader→worker 派发（`dispatcher.py`/`orchestrator.py`/`leader.py`）；隔离靠 `loop.py:479-487` 的 `relay` 接力——子任务只带「计划+摘要+文件路径」，不带前序工具输出。另有 `router.py:21 class Router` 按能力/后果路由到不同模型。 | 可借鉴 s06 的「subagent 只回传 final text」语义，把现有 worker 显式结构化为 subagent API。 |
| 权限与沙箱 | `s03_permission/code.py:155-212` 三道闸：deny list→rules→`ask_user` 交互确认。 | `agentforge/permissions.py:36 class Guard` 路径边界判定 + 读写分级 + 拦截；`loop.py:500` auto_approve 完全访问模式。 | 保持；可借鉴对方「关键词+交互确认」双闸补充高风险操作体验。 |
| 评测体系 | 仓库内仅 `docs/faq.md:26` 一句 "Excellent performance on SWE-Bench"，无详细验证/取舍文档。（任务书背景称 74%+，仓库内无数字出处。） | `evals/` 目录有复测报告（如 `evals/rerun6-analysis-20261018.md`），偏「回归验证」而非 SWE-Bench 类基准。 | 借鉴：为每个技术决策补「为什么」的设计取舍文档，结构对齐对方 docs。 |
| 会话恢复（中断续跑） | `default.py:159-190` `serialize()`+`save()`，每步 `finally` 调 `save` 写 JSON 轨迹（120-121）。显式 load/resume 入口未在 default.py 内证实。 | `agentforge/session.py:13 class Session` 会话持久化；orchestrator 跨批累积 `LeaderContext.add_progress`（`leader.py`）。 | 完善细粒度快照，确保模型中断可断点续传（rerun5 曾因从零重派而久）。 |
| 记忆系统 | `s09_memory/code.py:155-190` 写/重建 `MEMORY.md` 索引；`392-454` `extract_memories` 抽取；`456-543` `consolidate_memories` 周期合并去重。`.memory/` 目录。 | `agentforge/memory.py:56 class MemoryStore`；`loop.py:312 self.memory = MemoryStore(.../memory.md)`；`365-368` `prompt_block()` 注入。 | 借鉴 s09 的 extract→consolidate 周期合并，防记忆膨胀。 |
| Skill 系统 | `s07_skill_loading/code.py:59-128` `SkillLoader` 扫描 `skills/*/SKILL.md`，启动建 catalog 注入系统提示，按需 `load_skill()` 读全文。 | `agentforge/skills.py:37 class Skill` + `:238 class SkillRegistry`；`loop.py:540-545` 自动激活（auto_hint）+ Tier2 注入。 | 保持；可借鉴「catalog 先列名、按需读全文」进一步精简系统提示。 |

### 按性价比排序的借鉴清单

1. **任务流状态机闭环**（取自 `s10_task_system`）：把 `agentforge/taskstore.py` 接入 orchestrator/loop，给长链路任务明确 pending/in_progress/completed 与依赖图。预期收益：大幅提升长任务成功率、根治「从零重派」。改动面：`orchestrator.py`、`loop.py`、可能新增 `task_manager` 接线。
2. **大结果文件化持久化**（取自 `s08_context_compact`）：给 `agentforge/context.py:190 Compressor` 加「超长工具结果落盘 + 占位引用」。预期收益：降 token 消耗、提升大输出注意力。改动面：`context.py`。
3. **subagent 语义结构化**（取自 `s06_subagent`）：把现有 leader→worker 显式化为「subagent 只回传 final text」API，强化上下文隔离。预期收益：超长/并行任务隔离更干净。改动面：新增 subagent 调度层，扩展 `dispatcher.py`。
4. **记忆周期合并**（取自 `s09_memory`）：借鉴 extract→consolidate 去重合并。预期收益：防 memory.md 膨胀、提升召回质量。改动面：`memory.py`。
5. **交互式权限确认增强**（取自 `s03_permission`）：在 `Guard` 基础上加关键词+交互确认双闸。预期收益：高风险操作更安全。改动面：`permissions.py` + 前端 UI。

---

## 四、harness 版 vs 人工版 对比小结

| 对比点 | harness 版引用是否正确 | 人工版引用 | 结论差异 |
|---|---|---|---|
| 全部 learn-claude-code 引用 | ❌ 7 处路径错（下划线） | ✅ 连字符路径已核实 | harness 违反证据纪律 |
| agentforge/tools | ❌ 写成 tools.py | ✅ tools/ 包 | harness 写错文件 |
| agentforge/loop:96-124 | ❌ 指向提示词文本 | ✅ 指向真实编排文件 | harness 行号误导 |
| 子代理判断 | ❌ 称「单 Agent、缺分发」 | ✅ 已确认有 leader-worker+router | harness 低估 |
| SWE-Bench 文档 | ❌ 称「详细记录」 | ✅ 仅 faq.md 一句 | harness 高估 |
| 机制描述 | ✅ 文字描述靠谱 | ✅ 一致 | 两边主体判断相同 |
| 借鉴清单 | ✅ 5 条方向合理 | ✅ 5 条方向合理（重合度 ~80%） | 建议高度一致 |

**根本差异不在「判断」，在「证据可信度」**：人工版每一项都能被读者照路径+行号复现；harness 版有 9 处坏引用，其中 7 处是同一拼写错误，说明它并未真正回读源码核实路径，而是凭印象拼装——这恰恰是任务书明令禁止的。

**给 harness 的修复建议（按性价比）**：
1. 引用生成后做一次 `Path.exists()` 校验，路径不存在就回读重找（治 7 处连字符错）。
2. 引用目录时确认是文件还是包目录（治 `tools.py` vs `tools/`）。
3. 行号引用绑定到「符号」而非「裸行号」——例如引用 `class Agent` / `class Compressor` 而非 `loop.py:96`，避免指向提示词文本。
4. 凡写「对方如何如何」「文档称」必须回读源码/文档核实，禁止来自任务书背景的二手数字直接当仓库证据（治 SWE-Bench 高估）。
