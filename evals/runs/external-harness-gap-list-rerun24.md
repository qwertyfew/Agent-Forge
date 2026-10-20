# 外部 Harness 与 AgentForge 差距清单 (rerun24)

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| :--- | :--- | :--- | :--- |
| agent loop 结构 | `external/learn-claude-code/s17_goal_loop/README.md` 展示基于目标拆解的递归循环逻辑。 | `agentforge/loop.py` 实现基础的任务流控制。 | 可借鉴其 goal loop 的状态机转换，目前 AgentForge 更偏向线性任务流。 |
| 工具调用方式 | `external/mini-swe-agent/README.md` 定义了强校验的工具交互协议。 | `agentforge/toolselect.py` 支持多维度能力分发与模型选择。 | 建议借鉴其对工具输出的强校验机制，AgentForge 目前更依赖模型自发合规。 |
| 上下文压缩策略 | `external/learn-claude-code/s08_context_compact/README.md` 提供显式的 context 压缩算法。 | `agentforge/context.py:190` 实现 `Compressor` 类。 | 对齐其压缩逻辑以支持更复杂的长文本处理需求。 |
| TODO/任务清单机制 | `external/learn-claude-code/s05_todo_write/README.md` 展示持久化的 TODO 写入与更新。 | `agentforge/dispatcher.py:101` (`TaskSpec`) 与 `agentforge/tasksession.py:99` (`TaskSession`) 管理任务状态。 | 可借鉴其在 Task 内部维护显式子任务清单的 UI 交互模式。 |
| 子代理与上下文隔离 | `external/learn-claude-code/s06_subagent/README.md` 展示独立的子代理执行环境。 | `agentforge/dispatcher.py` 实现分层小步骤，但逻辑共享在同一会话中。 | 可借鉴其子代理的上下文完全隔离机制，防止干扰主流程。 |
| 权限与沙箱 | `external/learn-claude-code/s03_permission/README.md` 定义了基于 Session 的精细权限。 | `agentforge/permissions.py` 已包含基础权限控制。 | 建议对齐其基于 Session 的动态权限分配逻辑。 |
| 评测体系 | `external/mini-swe-agent/README.md` 展示了 SWE-Bench 相关评测指标。 | `evals/` 目录进行自定义评估。 | 可借鉴其自动化的 Benchmark 生成 pipeline。 |
| 会话恢复机制 | `external/learn-claude-code/s12_cron_scheduler/README.md` 包含中断续跑逻辑。 | `agentforge/resume.py` 支持基本状态持久化，但缺乏细粒度快照。 | 建议增加“断点续传”式的更细粒度快照功能。 |
| 记忆系统 | `external/learn-claude-code/s09_memory/README.md` 提供多维度、可检索的长期/短期记忆模型。 | `agentforge/memory.py` 实现基础核心功能，结构较单一。 | 建议引入其针对“知识 vs 事实”的分类存储逻辑。 |
| skill 系统 | `external/learn-claude-code/s07_skill_loading/README.md` 提供动态 Skill 加载与能力扩展。 | `agentforge/skills.py` 支持技能注册与分层调用。 | 两者高度对齐，AgentForge 的分层能力目前更强。 |

## 按性价比排序的借鉴清单

1. **上下文压缩策略 (Context Compression)**
   - 预期收益：显著降低长任务下的 Token 消耗，提高模型注意力。
   - 预估改动面：`agentforge/context.py`（优化压缩算法）、`agentforge/llm.py`（集成逻辑）。

2. **子代理上下文隔离 (Sub-agent Isolation)**
   - 预期收益：防止子任务产生的噪音污染主任务决策流。
   - 预估改动面：`agentforge/dispatcher.py`、`agentforge/session.py`。

3. **持久化 TODO 清单机制 (Persistent TODOs)**
   - 预期收益：提供更清晰的任务进度可视化，增强用户交互体验。
   - 预估改动面：`agentforge/taskstore.py`（增加子任务状态）、`webui.py`（前端展示）。

4. **精细化权限模型 (Granular Permissions)**
   - 预期收益：提高 agent 在执行敏感操作时的安全性。
   - 预估改动面：`agentforge/permissions.py`。

5. **断点续跑快照 (Checkpointing)**
   - 预期收益：支持大规模任务在网络中断或模型崩溃后的无损恢复。
   - 预估改动面：`agentforge/resume.py`、`agentforge/loop.py`。
