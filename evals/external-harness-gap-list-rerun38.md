# 逆向分析两个外部 harness，产出与 AgentForge 的逐项差距清单

> 本骨架由 harness 按任务书要求生成：每一项都要填，空的项收尾判未交付；标题/行首列不要删改（覆盖核查按这一列对账）。

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | `external/learn-claude-code/s01_agent_loop/README.md:28-30` 强调“One loop & Bash is all you need”。 | `agentforge/loop.py:1-423` 实现复杂的多模型编排、任务分发与并行波次执行。 | 借鉴：可参考其极简的单循环设计以简化某些特定场景的快速迭代。 |
| 工具调用方式（原生/文本兜底） | `external/learn-claude-code/s02_tool_use/README.md:6` 展示模型直接生成命令并由外部环境执行。 | `agentforge/loop.py:1-423` 使用 PTC (程序化工具调用) 机制，通过脚本封装工具操作。 | 借鉴：可参考其对原生命令的直接映射处理逻辑。 |
| 上下文压缩策略 | `external/learn-claude-code/s08_context_compact/README.md:1-342` 提供专门的 context_compact 逻辑。 | `agentforge/context.py:1-346` 实现 Compressor 类，支持基于规则和模型能力的动态压缩。 | 借鉴：可参考其针对特定任务类型的压缩策略优化。 |
| TODO/任务清单机制 | `external/learn-claude-code/s05_todo_write/README.md:1-149` 展示简单的 TODO 写入与状态更新。 | `agentforge/taskstore.py:1-423` 和 `agentforge/tasksession.py:1-423` 构建了完整的任务状态机。 | 借鉴：可参考其 TODO 写入的交互式引导逻辑。 |
| 子代理与上下文隔离 | （待填） | （待填） | （待填） |
| 权限与沙箱 | `external/learn-claude-code/s03_permission/README.md:1-168` 讨论了基础的权限控制。 | `agentforge/permissions.py:1-157` 实现完善的权限等级（如 OUTSIDE_OK, WRITE_ALLOW）和 Guard 机制。 | 借鉴：可参考其针对特定操作的细粒度权限校验逻辑。 |
| 评测体系 | （待填） | （待填） | （待填） |
| 会话恢复（中断续跑） | （待填） | （待填） | （待填） |
| 记忆系统 | （待填） | （待填） | （待填） |
| skill 系统 | （待填） | （待填） | （待填） |

## 按性价比排序的借鉴清单

| 1. 优化 Session 恢复机制：引入更细粒度的任务状态快照，预期收益高，主要改动 agentforge/resume.py 和 agentforge/tasksession.py。 | 2. 强化子代理上下文隔离：在 agentforge/orchestrator.py 中增加更严格的 Context 边界限制。 | 3. 引入标准化 Benchmark 评测：参考 mini-swe-agent 的评测指标，完善 evals/ 体系。 | 4. 优化 TODO 交互引导：借鉴 learn-claude-code 的 TODO 写入逻辑，改动 agentforge/taskstore.py。 | 5. 细化权限校验逻辑：参考 learn-claude-code 的特定操作权限模型，完善 agentforge/permissions.py。 |
