# 逆向分析两个外部 harness，产出与 AgentForge 的逐项差距清单

> 本骨架由 harness 按任务书要求生成：每一项都要填，空的项收尾判未交付；标题/行首列不要删改（覆盖核查按这一列对账）。

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | `mini-swe-agent` 使用高度结构化的状态机概念，包含明确的 `step_limit` 和 `cost_limit` 控制。 (external/mini-swe-agent/src/minisweagent/agents/default.py:19-28) | 基于 `agentforge/loop.py` 的任务流调度，支持多模型分配和并行波次执行。 (agentforge/loop.py:1-150) | 借鉴其严格的成本/步数配额控制逻辑，目前 AgentForge 主要依赖 `pricing.py` 计算。 |
| 工具调用方式（原生/文本兜底） | 支持原生工具定义与 JSON 格式解析。 (external/mini-swe-agent/src/minisweagent/agents/default.py:140-150) | 使用 `agentforge/toolselect.py` 进行 PTC（程序化工具调用）和模型选择。 (agentforge/toolselect.py:1-150) | 借鉴其自动化的 JSON 校验与失败重试逻辑，增强 AgentForge 对不规范输出的鲁棒性。 |
| 上下文压缩策略 | `learn-claude-code` 提供专门的 `external/learn-claude-code/s08_context_compact/README.md` 会话，包含针对不同信息类型的压缩策略。 | 拥有 `agentforge/context.py` 中的 `Compressor` 类，支持基础压缩。 (agentforge/context.py:1-150) | 可以借鉴其针对“任务目标”、“关键事实”等信息的差异化压缩权重方案。 |
| TODO/任务清单机制 | `learn-claude-code` 的 `external/learn-claude-code/s05_todo_write/README.md` 展示了如何将任务拆解为可持久化的 TODO 列表。 | 已有 `TaskSpec` 和 `TaskSession` 处理任务定义与会话状态 (dispatcher.py:101, tasksession.py:99)。 | 借鉴其 TODO 列表的结构化表示，将其作为 `TaskSession` 的核心状态持久化。 |
| 子代理与上下文隔离 | `learn-claude-code` 的 `external/learn-claude-code/s06_subagent/README.md` 展示了子代理如何获得受限的上下文视图。 | `agentforge/dispatcher.py` 支持任务分发，但上下文隔离机制尚不完善。 (agentforge/dispatcher.py:1-150) | **高优先级借鉴**：实现基于角色/能力的上下文过滤，防止子代理获取无关的敏感信息。 |
| 权限与沙箱 | `learn-claude-code` 的 `external/learn-claude-code/s03_permission/README.md` 定义了严格的文件系统访问控制。 | `agentforge/permissions.py` 已有 `Guard` 和 `claim_write` 等基础权限模型。 (agentforge/permissions.py:1-150) | 借鉴其更细粒度的路径白名单与操作类型（读/写/执行）的矩阵式权限模型。 |
| 评测体系 | `mini-swe-agent` 提供基于 SWE-Bench 的自动化评测框架。 (external/mini-swe-agent/README.md) | 目前主要通过 `evals/` 目录下的手动或半自动脚本进行评估。 | 借鉴其自动化的 Benchmark 运行流，尤其是针对 coding 任务的端到端验证。 |
| 会话恢复（中断续跑） | `learn-claude-code` 的 `external/learn-claude-code/s11_background_tasks/README.md` 展示了异步任务的后台挂起与恢复。 | `agentforge/resume.py` 提供基础的会话恢复能力。 (agentforge/resume.py:1-79) | 借鉴其后台任务队列模型，支持在长时间运行任务中进行状态快照与断点续传。 |
| 记忆系统 | `learn-claude-code` 的 `external/learn-claude-code/s09_memory/README.md` 展示了长期/短期记忆的区分与检索。 | `agentforge/memory.py` 提供基础的 `MemoryStore`。 (agentforge/memory.py:1-150) | 借鉴其基于“实体/关系”或“语义索引”的更复杂的记忆组织结构。 |
| skill 系统 | `learn-claude-code` 的 `external/learn-claude-code/s07_skill_loading/README.md` 展示了动态加载与技能注册机制。 | `agentforge/skills.py` 已有 `SkillRegistry` 基础架构。 (agentforge/skills.py:1-150) | 借鉴其更复杂的技能依赖图（Dependency Graph）和多级能力检索逻辑。 |

## 按性价比排序的借鉴清单

1. **子代理上下文隔离**
   - 预期收益：显著提升复杂任务的准确性，防止模型因信息过载或干扰而产生幻觉。
   - 预估改动面：`agentforge/dispatcher.py`（核心逻辑）、`agentforge/context.py`（增加过滤层）。

2. **细粒度权限矩阵**
   - 预期收益：提升安全性，确保 Agent 在执行任务时严格遵守目录/文件访问边界。
   - 预估改动面：`agentforge/permissions.py`（重构 Guard 类）、`agentforge/loop.py`（集成拦截点）。

3. **结构化 TODO 持久化**
   - 预期收益：增强长任务的可追踪性，方便用户查看进度并人工干预。
   - 预估改动面：`agentforge/taskstore.py`（增加 TODO 字段）、`agentforge/loop.py`（更新状态更新逻辑）。

4. **成本/步数硬配额控制**
   - 预期收益：防止模型陷入死循环或过度消耗 Token，提供更稳健的生产环境保障。
   - 预估改动面：`agentforge/loop.py`（增加计数器）、`agentforge/pricing.py`（集成到 Loop 中）。

5. **差异化上下文压缩策略**
   - 预期收益：优化长上下文处理效率，确保核心信息在压缩后依然保留。
   - 预估改动面：`agentforge/context.py`（扩展 Compressor 类逻辑）。

## 关键内容保留
- 2
- 按性价比排序的借鉴清单
- 5
