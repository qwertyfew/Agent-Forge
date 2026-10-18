# 外部 Harness 差距清单 (2026-10-16)

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| --- | --- | --- | --- |
| agent loop 结构 | `mini-swe-agent`: 线性循环，基于状态机切换 (src/minisweagent/agents/interactive.py)。<br>`learn-claude-code`: 递归嵌套的 goal loop (s17_goal_loop/code.py)。 | AgentForge: 基于编排器 orchestrator.py 的调度模型，支持多路径分发 (orchestrator.py:450+)。 | **可借鉴**：mini-swe-agent 的状态机对简单任务更稳定；AgentForge 目前的动态编排更适合复杂任务。 |
| 工具调用方式 | `mini-swe-agent`: 原生 Python 函数映射 + 文本兜底 (src/minisweagent/agents/default.py)。<br>`learn-claude-code`: 基于 MCP 协议插件化 (s14_mcp_plugin/...)。 | AgentForge: 完善的 `toolselect.py` 与 `skills.py` 系统，支持多模型联合决策。 | **保留现状**：AgentForge 的 skill 系统比简单的函数映射更高级。 |
| 上下文压缩策略 | `learn-claude-code`: 显式的滑动窗口与 token 计数压缩 (s08_context_compact/...)。 | AgentForge: 依赖模型原生上下文及基本的 `readcache.py` 处理。 | **可借鉴**：引入 learn-claude-code 的滑动窗口动态裁剪逻辑。 |
| TODO/任务清单机制 | `learn-claude-code`: 将 todo 写入本地文件作为持久化状态 (s05_todo_write/...)。 | AgentForge: 基于内存的任务流控制，主要通过 `taskstore.py` 维护进度。 | **可借鉴**：引入“文件锚点”机制，让 Agent 在中断后能通过读写 local todo 文件快速恢复进度。 |
| 子代理与上下文隔离 | `learn-claude-code`: 强制子代理进程隔离 (s06_subagent/...)。 | AgentForge: 目前倾向于在同一 session 内的任务拆分 (orchestrator.py)。 | **可借鉴**：引入进程级或严格上下文隔离的子代理机制，防止信息污染。 |
| 权限与沙箱 | `learn-claude-code`: 基于文件系统的权限 ACL 模型 (s03_permission/...)。 | AgentForge: 包含 `permissions.py` 但侧重于逻辑权限核查。 | **可借鉴**：引入更严格的系统级沙箱（如 Docker 或专用隔离环境）。 |
| 评测体系 | `mini-swe-agent`: 基于 SWE-Bench 的特定 benchmark 测试。 | AgentForge: `evals.py` 提供了通用评估框架。 | **保持现状**：AgentForge 的框架更通用，无需为了特定 benchmark 重构核心逻辑。 |
| 会话恢复 | `learn-claude-code`: 通过持久化 session 文件实现断点续传 (s15_integrated_harness/...)。 | AgentForge: 有 `resume.py` 基础，但缺乏细粒度的任务状态持久化。 | **可借鉴**：仿照 learn-claude-code 的集成 harness 实现断点恢复。 |
| 记忆系统 | `learn-claude-code`: 显式的长短期记忆分层 (s09_memory/...)。 | AgentForge: `memory.py` 已有基础实现，支持多模型交互中的 Context 注入。 | **保持现状**：AgentForge 的 memory 系统已具备较好的架构基础。 |
| skill 系统 | `learn-claude-code`: 动态加载与 Skill 定义 (s07_skill_loading/...)。 | AgentForge: `skills.py` 已建立成熟的技能注册机制。 | **保持现状**：AgentForge 的 skill 设计已领先于简单的代码块拼接。 |

## 按性价比排序的借鉴清单

1. **文件锚点 TODO 机制**
   - 预期收益：极大提升长任务的健壮性，解决网络波动或进程崩溃导致的进度丢失。
   - 预估改动面：修改 `taskstore.py` 与 `orchestrator.py`，增加本地文件读写逻辑。
2. **显式上下文滑动窗口**
   - 预期收益：降低 token 成本，提高模型处理超长历史时的注意力集中度。
   - 预估改动面：修改 `context.py` 和 `readcache.py`。
3. **进程级子代理隔离**
   - 预期收益：防止复杂任务中不同子任务的中间变量/上下文互相污染。
   - 预估改动面：修改 `orchestrator.py`，引入子进程管理逻辑。
4. **MCP 插件化标准对齐**
   - 预期收益：极大扩展工具链的可扩展性，兼容更多第三方生态。
   - 预估改动面：在 `toolselect.py` 中增加 MCP 协议解析层。
5. **断点续传持久化模型**
   - 预期收益：支持超长周期的自动化任务运行。
   - 预估改动面：完善 `resume.py`，对接 `taskstore.py` 的状态持久化。
