# 外部 Harness 与 AgentForge 差距清单 (2026-10-16)

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | `mini-swe-agent`: 基于协议定义的 `Agent.run` (external/mini-swe-agent/src/minisweagent/__init__.py:78) 和任务目标驱动。 | `agentforge/loop.py`: 复杂的 `orchestrator` (orchestrator.py) 与 `planner` (planner.py) 协作的非线性多轮循环。 | **可借鉴**: 考虑将 AgentForge 的复杂调度逻辑抽象为类似协议的形式，以简化核心 Loop 的状态管理。 |
| 工具调用方式 | `mini-swe-agent`: 通过 `Environment.execute` 统一封装 (external/mini-swe-agent/src/minisweagent/__init__.py:66)。 | `agentforge/toolselect.py`: 复杂的工具选择逻辑与基于 `llm.py` 的动态生成。 | **可借鉴**: 引入更明确的“执行环境”抽象，隔离工具的物理运行（沙箱）与逻辑调用。 |
| 上下文压缩策略 | `learn-claude-code`: 专用 session `s08_context_compact` 处理上下文滑动窗口与总结。 | `agentforge/context.py`: 主要依赖动态管理，目前缺乏强力的自动压缩机制。 | **高度建议借鉴**: 引入 `s08_context_compact` 的策略，这是处理长任务的关键。 |
| TODO/任务清单机制 | `learn-claude-code`: `s05_todo_write` 强调在每个 session 中持久化任务状态。 | `agentforge/taskstore.py`: 提供结构化的任务存储与状态流转。 | **现状领先**: AgentForge 的 `taskstore` 已具备较好的结构化支持，可继续深化。 |
| 子代理与隔离 | `learn-claude-code`: `s06_subagent` 展示了明显的子代理派生逻辑。 | `agentforge/orchestrator.py`: 支持多 Agent 协作但隔离机制尚在完善中。 | **可借鉴**: 参考 `s06` 的上下文传递规范，确保子代理不会污染主上下文。 |
| 权限与沙箱 | `learn-claude-code`: `s03_permission` 重点关注命令执行权限控制。 | `agentforge/permissions.py`: 已有基础的权限模型。 | **可借鉴**: 参考 `s03` 的精细化指令拦截逻辑。 |
| 评测体系 | `mini-swe-agent`: 使用 SWE-Bench Verified (external/mini-swe-agent/README.md)。 | `agentforge/evals.py`: 提供多种评估指标，但缺乏标准化的 Benchmark 对比。 | **可借鉴**: 将 SWE-Bench 的测评标准引入到 `evals` 模块中。 |
| 会话恢复 | `learn-claude-code`: 通过 `s10_task_system` 实现中断后的断点续跑。 | `agentforge/resume.py`: 提供基础的会话恢复功能。 | **可借鉴**: 参考 `s10` 的任务快照机制以实现更细粒度的中断恢复。 |
| 记忆系统 | `learn-claude-code`: `s09_memory` 分离了短期与长期记忆的处理。 | `agentforge/memory.py`: 已有基础的记忆存储逻辑。 | **可借鉴**: 引入 `s09` 的记忆更新策略（如基于重要性的权重）。 |
| skill 系统 | `learn-claude-code`: `s07_skill_loading` 提供动态技能发现与加载。 | `agentforge/skills.py`: 已有完善的技能注册与调度机制。 | **现状领先**: AgentForge 现有的 Skill 系统架构已非常成熟。 |

## 按性价比排序的借鉴清单

1. 上下文压缩 (Context Compaction)
   - 预期收益：大幅降低 Token 消耗，支持更长周期的复杂任务处理。
   - 预估改动面：在 `agentforge/context.py` 中新增自动摘要与清理逻辑。

2. 断点续跑机制优化 (Stateful Checkpointing)
   - 预期收益：提高复杂 Agent 任务的可靠性，防止因网络或超时的失败导致重跑。
   - 预估改动面：修改 `agentforge/taskstore.py` 与 `agentforge/resume.py`。

3. 执行环境抽象 (Execution Environment Abstraction)
   - 预期收益：解耦逻辑循环与物理执行，便于支持更多多样的沙箱（Docker, VM）。
   - 预估改动面：重构 `agentforge/toolselect.py` 及工具执行层。

4. 细粒度权限控制 (Granular Permission Control)
   - 预期收益：提高安全性，防止 Agent 执行破坏性指令。
   - 预估改动面：增强 `agentforge/permissions.py` 的拦截列表与策略。

5. 标准化 Benchmark 集成 (Standardized Benchmarking)
   - 预期收益：提供客观的性能评估数据，加速模型迭代。
   - 预估改动面：在 `agentforge/evals.py` 中增加对 SWE-Bench 的解析支持。
