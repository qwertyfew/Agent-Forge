# AgentForge vs. External Harnesses: Gap Analysis

## 差距清单

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| :--- | :--- | :--- | :--- |
| agent loop 结构 | **线性追加模式**：极简 `while True` 循环，每一步仅将模型响应和工具结果追加到消息列表。 (learn-claude-code/s01_agent_loop/code.py:96-127) | **复杂状态机驱动**：由 `orchestrator.py` 驱动，包含 `planner.py` 的多步规划和 `dispatcher.py` 的任务分发。 (agentforge/orchestrator.py, agentforge/planner.py) | **借鉴**：AgentForge 目前支持复杂的长程规划。mini-swe-agent 的线性历史更适合做简单的“单次任务”，但 AgentForge 的架构更适合复杂工程任务。 |
| 工具调用方式 | **原生工具调用接口**：直接利用 Anthropic/OpenAI 的 `tools` 参数传参，模型输出结构化 `tool_use` 块。 (learn-claude-code/s01_agent_loop/code.py:68-76, 98-112) | **混合模式（正则+原生）**：利用 `parse_text_tool_calls` 正则提取，同时也支持原生工具能力。 (agentforge/loop.py:17, agentforge/llm.py) | **借鉴**：mini-swe-agent 的“纯原生工具调用”在某些模型上稳定性更高，可考虑在特定 provider 上强制开启原生模式以减少正则解析错误。 |
| 历史记录管理 | **完全线性追加**：不做任何处理，所有 `tool_result` 直接塞入消息列表。 (mini-swe-agent/src/minisweagent/agents/default.py:71, learn-claude-code/s01_agent_loop/code.py:126) | **带上下文压缩的动态历史**：使用 `Compressor` 在循环中主动压缩旧消息，保持上下文窗口可用。 (agentforge/loop.py:15, agentforge/context.py) | **取舍**：AgentForge 的压缩机制是长任务的必须，mini-swe-agent 这种线性追加仅适用于短程推理或极长上下文模型。 |
| TODO/任务清单机制 | **无内置机制**：通过提示词要求模型在 bash 中操作，不维护内存中的 todo 列表。 (learn-claude-code/s01_agent_loop/code.py:63) | **结构化 TaskStore**：拥有独立的 `taskstore.py` 管理任务进度、依赖关系和待办项。 (agentforge/taskstore.py) | **维持现状**：AgentForge 的任务系统支持更复杂的工程逻辑（如多步骤依赖），mini-swe-agent 的做法过于简陋。 |
| 权限与沙箱 | **简单黑名单检查**：在 `run_bash` 中通过硬编码列表拦截 `rm -rf /` 等命令。 (learn-claude-code/s01_agent_loop/code.py:81) | **精细权限体系**：拥有 `permissions.py`，支持不同角色（Guard）和路径级别的读写控制。 (agentforge/permissions.py, agentforge/loop.py:20-27) | **维持现状**：AgentForge 的权限模型更符合生产安全要求，简单的黑名单无法防范复杂攻击。 |
| 记忆系统 | **无持久化记忆**：单次 session 执行完即结束，不涉及跨会话的长期记忆。 (learn-claude-code/s01_agent_loop/code.py:134) | **持久化 MemoryStore**：包含 `memory.py`，支持从本地文件读取和更新长期记忆（User Preference 等）。 (agentforge/memory.py, agentforge/loop.py:18, 338) | **维持现状**：AgentForge 的记忆系统是核心竞争力，mini-swe-agent 完全不具备此能力。 |
| Skill 系统 | **无独立 Skill 机制**：所有能力通过 bash 或提示词变相实现。 (mini-swe-agent/README.md:42, 71) | **高度模块化 SkillRegistry**：拥有专门的 `skills.py` 和 `autoskill.py` 支持技能加载与动态扩展。 (agentforge/skills.py, agentforge/autoskill.py) | **维持现状**：Skill 系统是 AgentForge 的核心架构设计，不宜为了模仿 mini-swe-agent 而简化。 |

## 按性价比排序的借鉴清单

1. **强制原生工具调用 (Native Tool Calling)**
   - 预期收益：减少由于正则匹配失败导致的“幻觉”动作，提高工具执行的确定性。
   - 预估改动面：修改 `agentforge/llm.py` 中的解析逻辑，在支持原生 tool_calling 的模型上优先使用其 native 返回值。
2. **线性历史的可视化追踪 (Trajectory Debugging)**
   - 预期收益：模仿 mini-swe-agent 的“线性历史”理念，在 UI 上提供一个完全纯粹的、未压缩的原始消息流视图（用于调试）。
   - 预估改动面：在 `web/` 前端增加一个“Debug Mode”开关，直接展示 `self.messages` 的原始序列。
