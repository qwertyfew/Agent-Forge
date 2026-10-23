# 逆向分析两个外部 harness，产出与 AgentForge 的逐项差距清单

> 本骨架由 harness 按任务书要求生成：每一项都要填，空的项收尾判未交付；标题/行首列不要删改（覆盖核查按这一列对账）。

| 对比点             | 对方做法（附证据：仓库/文件/行号）                                                        | AgentForge 现状（附证据：文件/行号）                                      | 可否借鉴与取舍建议                                                                      |
| --------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| agent loop 结构   | `external/learn-claude-code/s17_goal_loop/code.py:61-92` (目标循环逻辑)         | `agentforge/orchestrator.py:1470-1490` (Dispatcher分发)         | **可借鉴**：AgentForge目前偏向于“决策-拆解-分发”模型，可引入`learn-claude-code`的目标循环逻辑来增强复杂任务的收敛能力。 |
| 工具调用方式（原生/文本兜底） | （待填）                                                                      | （待填）                                                          | （待填）                                                                           |
| 上下文压缩策略         | （待填）                                                                      | （待填）                                                          | （待填）                                                                           |
| TODO/任务清单机制     | `external/learn-claude-code/s05_todo_write/code.py:5-19` (TodoManager状态机) | `agentforge/skeleton.py:30-40` (支持TODO标记但无自动追踪)               | **建议借鉴**：引入`TodoManager`的状态机逻辑，在`orchestrator.py`中加入“无进展提醒”逻辑。                 |
| 子代理与上下文隔离       | `external/learn-claude-code/s06_subagent/code.py:5-10` (独立loop且消息隔离)      | `agentforge/orchestrator.py:1470-1490` (Dispatcher分发但共享逻辑较模糊) | **可借鉴**：明确`s06_subagent`的消息隔离策略，确保子代理不污染主对话流。                                  |
| 权限与沙箱           | （待填）                                                                      | （待填）                                                          | （待填）                                                                           |
| 评测体系            | （待填）                                                                      | （待填）                                                          | （待填）                                                                           |
| 会话恢复（中断续跑）      | （待填）                                                                      | （待填）                                                          | （待填）                                                                           |
| 记忆系统            | `external/learn-claude-code/s09_memory/code.py:242` (MemoryStore持久化)      | `agentforge/memory.py:1-253` (基于关键词的记忆检索)                     | **可借鉴**：引入`MemoryStore`的清理与更新逻辑，优化当前`memory.py`的检索准确度。                         |
| skill 系统        | （待填）                                                                      | （待填）                                                          | （待填）                                                                           |

## 按性价比排序的借鉴清单

（待填）
