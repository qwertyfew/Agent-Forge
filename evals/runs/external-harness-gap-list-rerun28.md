## 对比点清单

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | learn-claude-code (s01_agent_loop): 采用高度抽象的循环，强调状态机的转变。证据：external/learn-claude-code/s01_agent_loop/README.md | AgentForge: 基于 orchestrator.py 和 loop.py 的调度模型，更偏向任务分发而非纯状态机流转。证据：agentforge/loop.py, agentforge/orchestrator.py | 借鉴：可考虑引入 learn-claude-code 的状态转换逻辑以增强复杂任务的鲁棒性。 |
| 工具调用方式 | mini-swe-agent: 极简设计，通过 polymorphism 提供多种环境和模型接口。证据：external/mini-swe-agent/AGENTS.md | AgentForge: 基于 toolselect.py 的动态选择机制，支持多种模型适配。证据：agentforge/toolselect.py | 借鉴：mini-swe-agent 的多态设计可简化工具封装的复杂度。 |
| 上下文压缩策略 | learn-claude-code (s08_context_compact): 专门的 session 处理上下文瘦身。证据：external/learn-claude-code/s08_context_compact/README.md | AgentForge: context.py 实现两级压缩（截断与摘要）。证据：agentforge/context.py | 借鉴：learn-claude-code 的策略更细致，可引入其特定的摘要策略。 |
| TODO/任务清单机制 | learn-claude-code (s05_todo_write): 显式的 todo 写入与管理。证据：external/learn-claude-code/s05_todo_write/README.md | AgentForge: taskstore.py 实现对话任务台账，记录产出文件。证据：agentforge/taskstore.py | 借鉴：learn-claude-code 的 todo 机制更偏向于“待办事项清单”，可与当前的“任务台账”互补。 |
| 子代理与上下文隔离 | learn-claude-code (s06_subagent): 强调子代理的独立性。证据：external/learn-claude-code/s06_subagent/README.md | AgentForge: 目前主要通过 orchestrator 分发，隔离度尚有提升空间。证据：agentforge/orchestrator.py | 借鉴：引入 learn-claude-code 的上下文隔离机制，防止子代理信息污染主任务。 |
| 权限与沙箱 | learn-claude-code (s03_permission): 细粒度的权限控制。证据：external/learn-claude-code/s03_permission/README.md | AgentForge: permissions.py 提供工作区边界保护和命令黑名单。证据：agentforge/permissions.py | 借鉴：learn-claude-code 的权限模型更接近工程实践，可参考其细粒度策略。 |
| 评测体系 | mini-swe-agent: 强调 SWE-Bench Verified 分数。证据：external/mini-swe-agent/AGENTS.md | AgentForge: evals.py 提供基础评估框架。证据：agentforge/evals.py | 借鉴：引入 mini-swe-agent 的评测指标（如特定 coding 任务的准确率）。 |
| 会话恢复 | AgentForge: resume.py 支持会话持久化。证据：agentforge/resume.py | learn-claude-code: 尚未在 session 中明确体现完整的持久化恢复机制。 | 借鉴：AgentForge 的持久化机制目前已领先于基础教程。 |
| 记忆系统 | learn-claude-code (s09_memory): 专门的记忆 session。证据：external/learn-claude-code/s09_memory/README.md | AgentForge: memory.py 提供基础存储能力。证据：agentforge/memory.py | 借鉴：引入 learn-claude-code 的记忆检索策略，增强长程任务的一致性。 |
| skill 系统 | learn-claude-code (s07_skill_loading): 动态加载技能。证据：external/learn-claude-code/s07_skill_loading/README.md | AgentForge: skills.py 提供技能注册与分发。证据：agentforge/skills.py | 借鉴：learn-claude-code 的技能动态加载机制更符合“按需扩展”的理念。 |

## 按性价比排序的借鉴清单

1. **引入状态机流转逻辑**：借鉴 learn-claude-code (s01)，增强 orchestrator 在复杂任务中的状态切换鲁棒性。预期收益：高，改动面：agentforge/orchestrator.py。
2. **细粒度权限模型**：参考 s03_permission，完善 permissions.py 的权限颗粒度。预期收益：中，改动面：agentforge/permissions.py。
3. **动态技能加载优化**：借鉴 s07_skill_loading，优化 skills.py 的按需加载逻辑。预期收益：高，改动面：agentforge/skills.py。
4. **上下文隔离机制**：参考 s06_subagent，在子任务执行时提供更强的上下文隔离。预期收益：中，改动面：agentforge/orchestrator.py, agentforge/context.py。
5. **强化 TODO 写入机制**：借鉴 s05_todo_write，增强 taskstore.py 对待办事项的显式管理。预期收益：低，改动面：agentforge/taskstore.py。
