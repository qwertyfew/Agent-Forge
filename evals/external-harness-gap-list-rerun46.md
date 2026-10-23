# 逆向分析两个外部 harness，产出与 AgentForge 的逐项差距清单

> 本骨架由 harness 按任务书要求生成：每一项都要填，空的项收尾判未交付；标题/行首列不要删改（覆盖核查按这一列对账）。

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | 单一循环模式：LLM -> 工具执行 -> 结果追加 (external/learn-claude-code/s01_agent_loop/code.py:7-13) | 编排调度模式：Planner 拆解任务为小步骤，Orchestrator 分配给最强模型 (agentforge/planner.py:8-13, agentforge/orchestrator.py) | **可借鉴**：learn-claude-code 的单循环更易于调试和快速原型，但在复杂任务上不如 AgentForge 的 Planner 架构稳健。 |
| 工具调用方式（原生/文本兜底） | （待填） | （待填） | （待填） |
| 上下文压缩策略 | （待填） | （待填） | （待填） |
| TODO/任务清单机制 | （待填） | （待填） | （待填） |
| 子代理与上下文隔离 | s06_subagent 提供独立的子代理会话环境 (external/learn-claude-code/s06_subagent/code.py:1-383) | Planner 拆解任务后由 Orchestrator 分配给不同模型执行 (agentforge/planner.py:8-13) | **可借鉴**：AgentForge 目前是逻辑上的“分工”，可借鉴 learn-claude-code 的“会话隔离”概念，为每个子任务提供独立的上下文窗口。 |
| 权限与沙箱 | （待填） | （待填） | （待填） |
| 评测体系 | 包含独立 evaluator 判断是否停止 (external/learn-claude-code/s17_goal_loop/code.py:51) | verify.py 提供系统侧强制验证闸门 (agentforge/verify.py:1-12) | **优于对方**：AgentForge 的系统侧强制验证（真实运行命令并获取结果）比 learn-claude-code 的模型解读判断更可靠。 |
| 会话恢复（中断续跑） | （待填） | （待填） | （待填） |
| 记忆系统 | （待填） | （待填） | （待填） |
| skill 系统 | （待填） | （待填） | （待填） |

## 按性价比排序的借鉴清单

1. **引入 TodoManager 自动提醒机制**
   - 预期收益：显著减少 Agent 在复杂任务中“迷失方向”或“陷入死循环”的概率。
   - 预估改动面：修改 agentforge/tasksession.py，增加计数器逻辑。
2. **实施渐进式工具下发（Tiered Disclosure）**
   - 预期收益：大幅降低长上下文任务中的 Token 消耗。
   - 预估改动面：修改 agentforge/toolselect.py 和 skills.py 的加载逻辑。
3. **强化权限系统的“规则匹配”层**
   - 预期收益：更精细地拦截危险操作（如删除工作区外文件）。
   - 预估改动面：修改 agentforge/permissions.py，增加正则或关键词规则库。
4. **引入显式的上下文压缩算法**
   - 预期收益：支持更长的任务链条，避免因上下文过长导致的模型性能下降。
   - 预估改动面：在 agentforge/memory.py 中增加 summary_history 函数。
5. **子任务会话隔离**
   - 预期收益：防止子任务的中间过程污染主任务的上下文。
   - 预估改动面：修改 agentforge/orchestrator.py，为每个拆解出的 sub-task 创建独立的 session 对象。
