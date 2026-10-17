# AgentForge vs External Harnesses Gap List (2026-10-16)

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| :--- | :--- | :--- | :--- |
| agent loop 结构 | **mini-swe-agent**: 极简循环，直接从 Model 类发起 `run` 并执行任务 (src/minisweagent/__init__.py:78)。<br>**learn-claude-code**: 典型的“思考->行动->观察”递归逻辑，强调 session 持久化 (s01_agent_loop/code.py)。 | **AgentForge**: 采用复杂的 `run` 主循环，包含预算检查、自动压缩、记忆注入和确定性澄清逻辑 (agentforge/loop.py:458-664)。 | **建议借鉴**：learn-claude-code 的 session 持久化机制。目前 AgentForge虽然有 `resume.py`，但 session 的状态机隔离不如其细致。 |
| 工具调用方式 | **mini-swe-agent**: 强类型协议定义 Environment 和 Model，工具为环境交互的抽象 (src/minisweagent/__init__.py:61-70)。 | **AgentForge**: 基于 `ToolRegistry` 的动态 Schema 注入与 `Router` 路由。支持 `tool_select` 和 `lazy_tools` (agentforge/loop.py:418-445)。 | **建议保留**：当前 AgentForge 的“工具索引”机制（在提示词中列出名字但隐藏 Schema）更适合节省上下文的策略，优于对方的强类型定义。 |
| 上下文压缩策略 | **learn-claude-code**: 采用渐进式压缩，涵盖从简单截断到基于摘要的重新生成 (s08_context_compact/code.py)。 | **AgentForge**: 由 `Compressor` 类驱动的自动瘦身，支持摘要式压缩并记录压缩日志 (agentforge/loop.py:396-412; agentforge/context.py)。 | **建议借鉴**：learn-claude-code 中对“不同层级”信息的分类处理（如把事实、计划分别处理）。AgentForge 目前倾向于整体摘要。 |
| TODO/任务清单机制 | **learn-claude-code**: 专门的 `todo_write` session，将子任务动态写入文件作为状态持久化 (s05_todo_write/code.py)。 | **AgentForge**: 通过 `gaps.ensure_step` 在内存中标记步骤，且有 `taskstore.py` 处理任务流，但缺乏“写入文件作为当前进度”的强一致性机制。 | **高度推荐借鉴**：将 TODO 列表持久化到外部文件是 AgentForge 目前最大的短板。建议引入类似 learn-claude-code 的 `todo_write` 操作，作为多步骤任务的“锚点”。 |
| 子代理与上下文隔离 | **learn-claude-code**: 通过 `subagent` 会话创建独立上下文，避免子代理任务污染主任务历史 (s06_subagent/code.py)。 | **AgentForge**: 虽有 `orchestrator.py` 和并行逻辑，但在单次 run 中尚未实现物理上的“上下文隔离”沙箱。 | **建议借鉴**：在子任务启动时自动创建独立的 Session ID 和上下文快照。这对防止子代理“幻觉污染”主路径至关重要。 |
| 权限与沙箱 | **learn-claude-code**: 独立的 `permission` session，基于权限等级控制 API 和文件读写 (s03_permission/code.py)。 | **AgentForge**: 提供 `Guard` 类和 `full_access` 开关，支持路径边界检查和命令审批 (agentforge/permissions.py; agentforge/loop.py:490-495)。 | **建议保留**：AgentForge 的 Guard 机制更成熟，支持“完全访问”模式。只需加强 learn-claude-code 中的权限层级细分即可。 |
| 评测体系 | **mini-swe-agent**: 通过 `README.md` 指向 SWE-Bench Verified 跑分 (mini-swe-agent/README.md)。 | **AgentForge**: 拥有完善的 `evals.py` 和 `evalsdesk.py`，支持多维度评分和自动化评估框架。 | **建议保留**：AgentForge 的评测体系远比上述两个开源项目复杂且强大，无需借鉴。 |
| 会话恢复 | **learn-claude-code**: 基于文件状态的断点续传机制 (s01_agent_loop/code.py)。 | **AgentForge**: 有 `resume.py` 处理简单的会话重连，但核心循环 `run` 对中间断点的细粒度状态恢复支持较弱。 | **建议借鉴**：learn-claude-code 的 session 序列号机制，确保从特定 Step 直接 Resume。 |
| 记忆系统 | **learn-claude-code**: 提供独立的 `memory` session 处理长短期记忆 (s09_memory/code.py)。 | **AgentForge**: 拥有 `MemoryStore` 和基于 `remember` 工具的长期记忆注入系统 (agentforge/memory.py; agentforge/loop.py:305-361)。 | **建议保留**：AgentForge 的“用户偏好”持久化与模型提示词注入机制已非常成熟。 |
| skill 系统 | **learn-claude-code**: 包含 `skill_loading` 会话，用于按需动态加载函数或能力 (s07_skill_loading/code.py)。 | **AgentForge**: 拥有完整的 `SkillRegistry` 和 `autoskill.py`，支持 skill 的描述注入与自动激活。 | **建议保留**：AgentForge 的 Skill 系统是核心竞争力，且比对方的逻辑更系统化。 |

### 按性价比排序的借鉴清单

1. **TODO 列表文件持久化（高收益，中改动）**
   - **预期收益**：显著降低多步骤任务中的“目标漂移”，为复杂任务提供稳定的状态锚点。
   - **预估改动面**：`agentforge/loop.py`（增加 TODO 更新逻辑）、`agentforge/taskstore.py`（增加持久化接口）。

2. **子代理上下文物理隔离（高收益，高改动）**
   - **预期收益**：彻底解决子代理生成的冗余信息污染主线任务历史的问题。
   - **预估改动面**：`agentforge/orchestrator.py`、`agentforge/session.py`（引入独立的上下文快照机制）。

3. **Session 细粒度断点恢复（中收益，中改动）**
   - **预期收益**：提高长任务在网络波动或模型报错时的可用性。
   - **预估改动面**：`agentforge/resume.py`、`agentforge/loop.py`（记录当前 Step 状态）。

4. **多层级事实压缩策略（低收益，低改动）**
   - **预期收益**：进一步优化长对话下的 Token 利用率。
   - **预估改动面**：`agentforge/context.py`（细化压缩规则）。

5. **权限层级细分（低收益，低改动）**
   - **预期收益**：提供更精细的安全控制，而非简单的“全开”或“审批”。
   - **预估改动面**：`agentforge/permissions.py`。
