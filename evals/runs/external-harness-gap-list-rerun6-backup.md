# AgentForge vs External Harnesses Gap List (2026-10-18)

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
|---|---|---|---|
| agent loop 结构 | 极简 `while True` 循环，核心逻辑在 `step()` 驱动的 query 和 execute（`mini-swe-agent/src/minisweagent/agents/default.py:88-124`） | 复杂的 Orchestrator 模式，解耦了任务状态、模型选择、权限拦截和上下文压缩（`agentforge/loop.py:96-124`） | 保持现有架构，它更适合生产环境的复杂逻辑流，mini-swe-agent 的极简设计仅适用于单一场景。 |
| 工具调用方式 | 标准化输入 schema，支持模型输出解析为 tool_calls 并循环执行（`learn_claude_code/s01_agent_loop/code.py:68-76`, `96-125`） | 基于 Registry 的工具模型，支持多模型适配和类型检查（`agentforge/tools.py` 等），具备更强的安全性过滤 | 现有的 Registry 模型更健壮。可借鉴对方的“纯粹循环”思想来简化某些特定的自动化脚本流。 |
| 上下文压缩策略 | 多阶段策略：snip_compact, micro_compact，且支持对大结果的文件持久化（`learn_claude_code/s08_context_compact/code.py`） | 独立的 `Compressor` 类，支持按权重和多种策略动态选择压缩方式（`agentforge/context.py`） | 借鉴对方的 snip_compact 逻辑优化长文本处理，特别是针对“大结果”的文件化持久存储。 |
| TODO/任务清单机制 | 专门的任务系统管理持久化的待办事项流，支持从任务分解到状态追踪的闭环（`learn_claude_code/s10_task_system/`） | 目前仅有基础的 `taskstore.py` 提供数据持久化，缺乏自动化的任务流状态机驱动（`agentforge/taskstore.py`） | **高优先级借鉴**。引入 Claude Code 式的任务状态管理，显著提升长路径任务的鲁棒性。 |
| 子代理与隔离 | 通过独立实例运行子代理，并能在不同上下文下隔离任务（`learn_claude_code/s06_subagent/code.py`） | 目前主要基于单 Agent 架构，缺乏标准化的子代理分发机制 | **中优先级借鉴**。针对超长任务或并行需求，引入子代理模型可以降低 Context 污染。 |
| 权限与沙箱 | 在执行每条 bash 前进行交互式确认和关键词过滤（`learn_claude_code/s03_permission/code.py`） | 通过 `Guard` 类实现路径边界判定、读写权限分级以及拦截机制（`agentforge/permissions.py`） | 保持现有的权限模型，它比简单的关键词过滤更符合生产级的安全需求。 |
| 评测体系 | 在 docs 中详细记录针对 SWE-Bench 的验证结果和取舍逻辑（`mini-swe-agent/docs/`） | 包含在 `evals/` 目录中，但缺乏像对方那样结构化的设计取舍文档 | 借鉴对方的文档组织方式，记录每项技术决策的“为什么”。 |
| 会话恢复 | 提供中断后的状态序列化与加载机制（通过 JSON 序列化整个 Agent 状态）（`mini-swe-agent/src/minisweagent/agents/default.py:159-182`） | 已具备基本状态序列化能力（`agentforge/loop.py` 中包含相关逻辑），支持任务流的持久化 | 继续完善细粒度的状态快照，确保在模型中断时能实现完美的“断点续传”。 |
| 记忆系统 | 使用 `MemoryStore` 管理用户偏好和长期习惯（`learn_claude_code/s09_memory/`） | 已实现基于 `memory.md` 的持久化记忆注入（`agentforge/loop.py:365-368`） | 完善当前的记忆提取逻辑，确保更精准地从历史任务中提取用户偏好。 |
| Skill 系统 | 支持动态的 Skill 加载机制，允许 Agent 在运行时识别并按需加载特定能力（`learn_claude_code/s07_skill_loading/`） | 已有 `SkillRegistry` 结构，但缺乏在运行过程中动态发现并热加载新 Skill 的机制 | **中优先级借鉴**。实现动态 Skill 注册与加载，减少系统提示词的冗余信息。 |

## 按性价比排序的借鉴清单

1.  **Todo 状态机闭环**: 从 `s10_task_system` 引入任务分解与状态追踪流。
    *   预期收益：大幅提升复杂长路径任务的成功率，减少 Agent 循环导致的上下文偏移。
    *   预估改动面：新增 `task_manager.py`，在 `loop.py` 中集成任务分解逻辑。
2.  **多阶段压缩微调**: 参考 `s08_context_compact` 的 snip_compact 策略。
    *   预期收益：优化长文本工具结果的处理，减少 Token 浪费并提升模型注意力。
    *   预估改动面：修改 `agentforge/context.py`。
3.  **子代理分发模式**: 基于 `s06_subagent` 的隔离思想设计子代理分发器。
    *   预期收益：支持超大规模任务的并行处理与上下文隔离。
    *   预估改动面：新增 `subagent_orchestrator.py`，扩展 `router.py`。
4.  **Skill 动态加载**: 实现类似 `s07_skill_loading` 的按需热加载机制。
    *   预期收益：精简系统提示词，提高模型对特定技能的遵从度。
    *   预估改动面：修改 `skills.py`。
5.  **交互式确认增强**: 借鉴 `s03_permission` 的交互反馈流。
    *   预期收益：提升用户在关键操作（如文件删除、生产环境变更）时的体验与安全性。
    *   预估改动面：修改 `permissions.py` 和前端 UI 展示。
