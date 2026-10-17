# AgentForge 维度分析

## 1. agent loop 结构
- 入口 `Agent.run`（`agentforge/loop.py` 448‑…）实现完整任务轮次循环，内部调用 `self.hub.chat_stream` 进行模型推理，并在每轮前后收集工具调用、压缩上下文、统计信息。

## 2. 工具调用方式
- 通过 `self.registry`（`agentforge/registry.py`）维护工具列表，`Agent.run` 在每轮前调用 `_active_tools` 过滤可用工具，随后将 `tools` 传给 `hub.chat_stream`。工具调用结果在 `result.tool_calls` 中返回，若无 `tool_calls` 但 `content` 存在则尝试解析 JSON 形式的工具调用。

## 3. 上下文压缩策略
- `Compressor`（`agentforge/context.py` 190‑…）实现两级压缩：L1 `truncate_tool_results`（截断早期工具返回）和 L2 `summarize`（用模型生成摘要）。压缩阈值由 `compact_threshold`、`compact_keep_chars` 等配置决定。

## 4. TODO/任务清单机制
- `Agent.run` 通过 `self._task_notes_block()` 读取 `task_notes`（`agentforge/tools/notes.py`）文件，形成系统消息 `TASK_NOTES_HEADER`，并在每轮输出前插入。

## 5. 子代理与上下文隔离
- 通过 `self._active_tools(ctx)` 仅保留当前轮次可用工具，且使用 `relay` 参数可提供裁剪后的历史，保证子任务只看到必要的上下文。

## 6. 权限与沙箱
- `permissions.py` 定义 `Guard`、`PermissionDenied`，`Agent.run` 在 `auto_approve` 参数决定是否使用 `Guard` 的审批逻辑。默认权限下工具调用会触发 `Guard` 进行路径与命令审批。

## 7. 评测体系
- `evals.py`（`agentforge/evals.py`）实现评测用例加载与四层判定，`Agent.run` 在 `evals` 模块中被调用以执行评测任务。

## 8. 会话恢复
- `cli.py`（`agentforge/cli.py` 31‑…）提供 `/new`、`/save`、`/load` 命令，使用 `memory.MemoryStore` 存储会话历史，`Agent.run` 可通过 `relay` 重新载入历史实现会话恢复。

## 9. 记忆系统
- `artifact.py` 追踪任务产出文件；`autonote.py` 在任务结束后自动将重要记忆写入 `memory.MemoryStore`；`memory.py`（`agentforge/memory.py`）提供读写接口。

## 10. skill 系统
- `autoskill.py`（`agentforge/autoskill.py`）负责自动沉淀 skill；`Agent.run` 在 `skill_auto_load` 开启时使用 `skills.auto_hint` 与 `skills.match` 自动注入 skill 摘要。`skills` 通过 `SkillRegistry` 发现并注册。