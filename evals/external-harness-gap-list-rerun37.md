# 逆向分析两个外部 harness，产出与 AgentForge 的逐项差距清单

> 本骨架由 harness 按任务书要求生成：每一项都要填，空的项收尾判未交付；标题/行首列不要删改（覆盖核查按这一列对账）。

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | learn_claude-code/s17_goal_loop/code.py: 显式 GoalLoop 类驱动循环，状态机管理进度。 | agentforge/loop.py: 基于 Orchestrator 与 Planner 的协作模式，偏向规划-执行递归结构。 | 可借鉴：引入显式状态机增强长任务的可预测性。 |
| 工具调用方式（原生/文本兜底） | learn_claude-code/s02_tool_use/code.py: 标准 JSON Schema 定义工具，支持强类型校验与重试。 | agentforge/toolselect.py: 基于 ToolSelect 类动态匹配工具，主要依靠模型对描述的理解。 | 可借鉴：引入严格 JSON Schema 校验以降低解析错误。 |
| 上下文压缩策略 | learn_claude-code/s08_context_compact/code.py: 定制化摘要算法，支持按模块（历史对话、当前任务）分别压缩。 | agentforge/context.py: 主要依赖模型生成的 summarize 或简单截断逻辑。 | 高优先级借鉴：参考其结构化压缩逻辑，在 context.py 中实现多维度管理。 |
| TODO/任务清单机制 | learn_claude-code/s05_todo_write/code.py: 强制要求 Agent 在每一步操作后更新持久化的 TODO 列表文件。 | agentforge/taskstore.py: 维护任务状态，但缺乏原子步骤级别的持久化清单更新。 | 可借鉴：引入原子步骤清单以增强长任务中断恢复能力。 |
| 子代理与上下文隔离 | learn_claude-code/s06_subagent/code.py: 使用独立进程/容器运行子代理，严格限制对主进程访问。 | agentforge/dispatcher.py: 主要通过逻辑拆分实现多 Agent 协作，共享同一上下文环境。 | 谨慎借鉴：在 dispatcher.py 中引入沙箱标识位以增强安全性。 |
| 权限与沙箱 | learn_claude-code/s03_permission/code.py: 基于文件系统的权限控制，区分读/写/执行权限。 | agentforge/permissions.py: 提供基础权限检查逻辑，主要用于 API 级别的访问控制。 | 可借鉴：参考其基于路径的细粒度权限模型，增强本地文件保护。 |
| 评测体系 | learn_claude-code/s15_integrated_harness/code.py: 集成完整的 SWE-Bench 风格评测框架，支持自动化端到端测试。 | agentforge/evals/: 目前主要为手动或半自动的评估脚本。 | 可借鉴：引入其自动化评测框架以提升迭代效率。 |
| 会话恢复（中断续跑） | learn_claude-code/s10_task_system/code.py: 提供基于任务 ID 的全量状态快照。 | agentforge/resume.py: 已有基础的 Resume 机制，支持从 checkpoint 恢复。 | 现状优于对方：AgentForge 已具备核心功能，无需大规模重构。 |
| 记忆系统 | learn_claude-code/s09_memory/code.py: 基于向量数据库与短期缓存结合的混合记忆模型。 | agentforge/memory.py: 已支持基础的记忆存储与检索。 | 可借鉴：参考其短期/长期内存划分逻辑，优化 memory.py 的缓存淘汰策略。 |
| skill 系统 | learn_claude-code/s07_skill_loading/code.py: 动态加载机制，支持版本控制与前置条件检查。 | agentforge/skills.py: 已具备 Skill 定义、自动匹配与加载能力。 | 现状优于对方：AgentForge 的 Skill 系统设计更先进（含 when_to_use 元数据）。 |

## 按性价比排序的借鉴清单

1. 结构化上下文压缩 (预期收益：降低 Token 消耗，减少逻辑崩溃；预计修改 agentforge/context.py)
2. 严格工具 Schema 校验 (预期收益：降低解析错误；预计修改 agentforge/toolselect.py)
3. 持久化原子 TODO 清单 (预期收益：提升长任务鲁棒性；预计修改 agentforge/taskstore.py)
4. 自动化评测集成框架 (预期收益：建立标准评估基准；在 evals/ 下新建脚本)
5. 基于路径的细粒度权限模型 (预期收益：增强本地文件安全；预计修改 agentforge/permissions.py)
