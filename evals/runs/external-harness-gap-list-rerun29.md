# 外部 Harness 差距清单 (rerun29)

## 差距对比表

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| :--- | :--- | :--- | :--- |
| agent loop 结构 | `external/learn-claude-code/s01_agent_loop/code.py:7-15` 使用 `while True` 循环，包含明确的 `thought` -> `action` -> `observation` 状态机。 | `agentforge/loop.py:1-100` 基于 `Dispatcher` 和 `Planner` 的多步拆解与分发逻辑，而非单一循环。 | **可借鉴**：AgentForge 目前更偏向“任务编排”，可引入对方的“状态机”可视化或显式状态记录来增强稳定性。 |
| 工具调用方式 | `external/learn-claude-code/s02_tool_use/code.py:10-30` 使用简单的 `dispatch` 字典映射工具名到函数。 | `agentforge/toolselect.py:9-15` 采用三级渐进式下发（Tier 1 索引，Tier 2 关键词匹配），优化 Token 开销。 | **保留现状**：AgentForge 的渐进式下发是针对长上下文优化的核心竞争力，不建议退回简单的全量映射。 |
| 上下文压缩策略 | `external/learn-claude-code/s08_context_compact/code.py:6-15` 在每次模型调用前强制执行压缩逻辑（L1 截断 + L2 摘要）。 | `agentforge/context.py:4-8` 支持两级压缩策略，且支持手动触发。 | **可借鉴**：对方的“强制每轮压缩”逻辑可以作为 AgentForge 的默认配置，防止长任务中上下文爆炸。 |
| TODO/任务清单机制 | `external/learn-claude-code/s10_task_system/code.py:6-12` 使用 `.tasks/` 目录下的 JSON 文件维护任务依赖图（Dependency Graph）。 | `agentforge/tasksession.py:3-5` 采用“不可变事件日志（Event Log）+ 上下文视图”的设计。 | **可借鉴**：对方的显式依赖图（BlockedBy）非常适合处理复杂的并行/串行任务逻辑，可整合进 AgentForge 的 `TaskSession`。 |
| 子代理与上下文隔离 | `external/learn-claude-code/s06_subagent/code.py:5-7` 运行独立的子 agent 循环，仅返回最终文本给父级。 | `agentforge/loop.py` 通过 `Dispatcher` 分发到不同模型，共享核心状态但逻辑隔离。 | **可借鉴**：对方的“完全隔离”思想可用于处理高风险任务（如删除文件），AgentForge 可引入“沙箱 Session”。 |
| 权限与沙箱 | `external/learn-claude-code/s03_permission/code.py:5-9` 包含三道门禁（黑名单、规则匹配、用户确认）。 | `agentforge/permissions.py` 已有基础权限管理。 | **可借鉴**：对方的权限模型更细粒度，可参考其针对不同工具类型的动态权限分配方案。 |
| 评测体系 | `external/mini-swe-agent/README.md:7` 强调 SWE-Bench Verified 实测结果。 | `evals/` 目录下包含多种 benchmark 和测试脚本。 | **可借鉴**：对方的自动化评估流程非常成熟，可引入其针对 coding 任务的特定评测指标。 |
| 记忆系统 | `external/learn-claude-code/s09_memory/code.py:6-10` 使用 `.memory/` 目录存储提取的记忆片段。 | `agentforge/memory.py:3-5` 仅存储用户偏好（Preferences）。 | **可借鉴**：AgentForge 目前只存偏好，可引入对方的“事实记忆”提取机制，实现更强的长期上下文能力。 |

## 按性价比排序的借鉴清单

1. **引入显式任务依赖图（Dependency Graph）**
   - 预期收益：显著提升复杂多步骤任务的自动化编排能力，支持复杂的并行/串行逻辑。
   - 预估改动面：修改 `agentforge/tasksession.py` 和 `agentforge/planner.py`，引入 `.tasks/` JSON 文件管理机制。
2. **强制每轮上下文压缩（Mandatory Context Compaction）**
   - 预期收益：大幅降低长对话中的 Token 消耗，提高小模型的推理稳定性。
   - 预估改动面：修改 `agentforge/loop.py`，在每次模型调用前插入 `context.py` 的自动压缩逻辑。
3. **细粒度动态权限分配（Granular Dynamic Permissions）**
   - 预期收益：提升 Agent 操作安全性，防止误操作。
   - 预估改动面：修改 `agentforge/permissions.py`，参考 `external/learn-claude-code/s03_permission/code.py` 的逻辑更新工具权限矩阵。
4. **事实记忆提取（Fact Extraction Memory）**
   - 预期收益：Agent 能记住历史任务中的关键事实而非仅是用户偏好。
   - 预估改动面：修改 `agentforge/memory.py`，增加“事实片段”存储逻辑，并参考 `external/learn-claude-code/s09_memory/code.py` 的提取流程。
5. **状态机可视化（State Machine Visualization）**
   - 预期收益：增强用户对 Agent 当前思考过程的可视化感知。
   - 预估改动面：修改 `agentforge/loop.py`，在 Loop 中记录当前状态（Thought/Action/Observation），并同步到前端 UI。
