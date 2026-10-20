# External Harness Gap Analysis (Rerun 22)

## 对比清单

| 对比点 | 对方做法（附证据） | AgentForge 现状（附证据） | 可否借鉴与取舍建议 |
| :--- | :--- | :--- | :--- |
| agent loop 结构 | `external/learn-claude-code/s17_goal_loop/code.py`：基于 Goal Loop 驱动，包含明确的完成条件判断。 | `agentforge/tools/ops.py`：采用任务拆解后的步骤流转，侧重于多模型协作和并行执行逻辑。 | **可借鉴**：引入更显式的“完成条件”检查机制，在每个 Loop 结束时自动判断是否达到目标。 |
| 工具调用方式 | `external/mini-swe-agent/src/minisweagent/`：极简 Agent 处理原生工具调用。 | `agentforge/tools/`：高度封装的工具集，支持参数校验和多种协议。 | **无需借鉴**：AgentForge 已具备更成熟的工具抽象层。 |
| 上下文压缩策略 | `external/learn-claude-code/s08_context_compact/code.py`：在模型调用前主动执行摘要与 budget 裁剪。 | `agentforge/tools/memory.py`：提供记忆存储，但缺乏实时的、基于 token 预算的 Context Compaction 逻辑。 | **高优先级借鉴**：在长对话或多步骤任务中，引入 `s08` 的预算裁剪策略可有效降低 Token 成本。 |
| TODO/任务清单机制 | `external/learn-claude-code/s10_task_system/code.py`：维护 session 范围内的 TODO 系统，支持自动状态更新。 | `agentforge/tools/notes.py`：提供简单的笔记存储，缺乏与 Agent 运行状态强绑定的任务清单。 | **可借鉴**：将 `s10` 的任务系统集成到核心调度逻辑中，实现 TODO 的自动化 Tick。 |
| 子代理与上下文隔离 | `external/learn-claude-code/s13_agent_teams/code.py`：多代理协作，明确隔离不同代理的 Context。 | AgentForge Core 调度逻辑：支持多模型分配，但在 Session 级别的 Context 隔离尚不显著。 | **可借鉴**：借鉴 `s13` 的团队分配逻辑，确保子代理仅接收必要的上下文片段。 |
| 权限与沙箱 | `external/learn-claude-code/s03_permission/code.py`：显式的权限控制层。 | `agentforge/tools/shell.py`：提供基础 shell 执行功能，但缺乏细粒度的权限审批流程。 | **建议取舍**：当前 AgentForge 侧重于生产力，复杂的权限系统可按需模块化增加。 |
| 评测体系 | `external/mini-swe-agent/README.md`：基于 SWE-Bench Verified 的定量评估。 | `evals/` 目录：包含多种分析报告和对比数据。 | **无需借鉴**：AgentForge 已有完善的 eval 框架。 |
| 会话恢复 | `external/learn-claude-code/s11_background_tasks/code.py`：支持后台任务与状态持久化。 | `agentforge/tools/memory.py`：支持基本记忆存储。 | **可借鉴**：引入 `s11` 的背景任务处理逻辑，支持长耗时任务的异步续跑。 |
| 记忆系统 | `external/learn-claude-code/s09_memory/code.py`：基于 Key-Value 结构的记忆检索。 | `agentforge/tools/memory.py`：核心记忆组件。 | **已对标**：AgentForge 的记忆系统已具备基本能力。 |
| skill 系统 | `external/learn-claude-code/s07_skill_loading/code.py`：动态 Skill 加载与分发。 | `agentforge/tools/skill.py`：支持 Skill 安装、搜索与加载。 | **已对标**：AgentForge 的 Skill 系统架构更成熟。 |

## 按性价比排序的借鉴清单

1. **Context Compaction (来自 s08)**
   - 预期收益：大幅降低长任务 Token 消耗，防止 Context 过载导致模型性能下降。
   - 预估改动面：`agentforge/tools/memory.py`, `agentforge/tools/ops.py`。
2. **Task System (来自 s10)**
   - 预期收益：提供更透明的任务进度可视化，支持 TODO 的自动化状态流转。
   - 预估改动面：新建核心任务管理类，关联到 AgentForge core 调度逻辑。
3. **Completion Condition (来自 s17)**
   - 预期收益：减少模型在已完成任务上的无效循环，提高 Agent 决策准确性。
   - 预估改动面：`agentforge/tools/ops.py` 核心循环逻辑。
4. **Background Tasks (来自 s11)**
   - 预期收益：支持高耗时（如大规模文件扫描）任务的后台执行，提升用户响应速度。
   - 预估改动面：`agentforge/tools/ops.py`, `agentforge/tools/shell.py`。
5. **Agent Teams (来自 s13)**
   - 预期收益：更复杂的任务可以分配给专门的子代理，减少主 Agent 的认知负载。
   - 预估改动面：AgentForge core 多模型调度逻辑。
