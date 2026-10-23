# 逆向分析两个外部 harness，产出与 AgentForge 的逐项差距清单

> 本骨架由 harness 按任务书要求生成：每一项都要填，空的项收尾判未交付；标题/行首列不要删改（覆盖核查按这一列对账）。

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | external/learn-claude-code/s01_agent_loop/code.py:7-13 (标准 while True 循环) | agentforge/loop.py (基于状态机的任务驱动循环) | 可借鉴：对方的极简逻辑适合快速原型，但 AgentForge 的状态机更适合复杂长任务。 |
| 工具调用方式（原生/文本兜底） | external/learn-claude-code/s02_tool_use/code.py (定义基础工具集) | agentforge/toolselect.py (复杂工具选择逻辑与能力分层调度) | 可借鉴：对方的“文本兜底”机制可强化 AgentForge 的鲁棒性。 |
| 上下文压缩策略 | external/learn-claude-code/s08_context_compact/code.py (显式压缩逻辑) | agentforge/context.py (基础上下文管理) | 高度建议借鉴：对方的压缩算法可集成到 AgentForge 的 context 管理中。 |
| TODO/任务清单机制 | external/learn-claude-code/s05_todo_write/code.py (每步更新 TODO 列表) | agentforge/taskstore.py:3-14 (.tasks.json 台账) | 可借鉴：对方将 TODO 作为动态任务清单实时反馈给模型的做法。 |
| 子代理与上下文隔离 | external/learn-claude-code/s06_subagent/code.py (独立会话上下文) | agentforge/dispatcher.py (多 Agent 调度) | 高度建议借鉴：对方的隔离机制能有效防止子代理污染主任务上下文。 |
| 权限与沙箱 | external/learn-claude-code/s03_permission/code.py (明确权限模型) | agentforge/permissions.py:11-15 (contextvars 路径放行) | 可借鉴：对方的细粒度权限定义可进一步丰富 AgentForge 的 permissions 模块。 |
| 评测体系 | external/mini-swe-agent/src/minisweagent/exceptions.py (基础评测逻辑) | agentforge/evals.py (基础评估框架) | 可借鉴：对方在 coding 场景下的特定评测指标可引入 AgentForge。 |
| 会话恢复（中断续跑） | 未显著体现复杂持久化恢复机制 | agentforge/resume.py:1-30 (支持从中断点恢复状态) | AgentForge 领先：AgentForge 已具备完善的 resume 机制。 |
| 记忆系统 | external/learn-claude-code/s09_memory/code.py (显式记忆管理) | agentforge/memory.py (持久化与检索) | 可借鉴：对方关于“记忆遗忘”或“权重分配”的策略可引入 AgentForge。 |
| skill 系统 | external/learn-claude-code/s07_skill_loading/code.py (动态加载技能) | agentforge/skills.py (能力定义与热加载) | AgentForge 领先：AgentForge 的 Skill 系统已高度成熟。 |

## 按性价比排序的借鉴清单

| 优先级 | 建议项 | 预期收益 | 预估改动面 |
| --- | --- | --- | --- |
| 1 | 上下文压缩算法 (s08_context_compact) | 降低 Token 消耗，提高长任务注意力 | agentforge/context.py |
| 2 | 子代理上下文隔离 (s06_subagent) | 防止子代理污染主任务上下文 | agentforge/dispatcher.py, context.py |
| 3 | 动态 TODO 列表反馈 (s05_todo_write) | 增强模型对复杂任务进度感知 | agentforge/taskstore.py, loop.py |
| 4 | 细粒度权限定义 (s03_permission) | 提高 AgentForge 在敏感环境下的安全性 | agentforge/permissions.py |
| 5 | 工具调用文本兜底机制 (s02_tool_use) | 提高模型在输出格式不规范时的鲁棒性 | agentforge/toolselect.py |
