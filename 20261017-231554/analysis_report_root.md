## agent loop 结构
- Agent 类位于 `agentforge/loop.py`，构造时接收配置、LLM、工具注册表、权限、路由等。构造代码行 270-295。该类实现 `run()` 作为主循环，行 448 开始。

## 工具调用方式
- 工具注册表 `ToolRegistry` 在 `agentforge/tools/__init__.py`（行 1-20）。工具通过 `make_tool` 或直接子类化 `Tool`。Agent 在 `_active_tools`（行 418-445）动态生成可用工具 schema，并在系统提示中列出。工具执行通过 `tool_calls` 传入 LLM，Agent 在 `run()` 里根据 `tool_calls` 调用对应工具的 `run()`。

## 上下文压缩策略
- `Compressor` 在 `agentforge/context.py`（行 190-200）。`Agent._compact`（行 396-404）在每轮前调用 `compressor.compact()`，根据 `compact_auto` 触发。压缩分为 L1（截断）和 L2（摘要），实现细节见 `Compressor` 的 `compact()` 方法。

## TODO/任务清单机制
- 任务笔记文件位于 `cfg.state()/task-notes/`，读取路径在 `Agent._task_notes_path`（行 376-380）。`_task_notes_block`（行 381-392）将内容注入系统提示，用 `TASK_NOTES_HEADER`。

## 子代理与上下文隔离
- 子代理在 `agentforge/loop.py` 行 449 通过 `relay` 参数传递，内部通过 `run()` 递归调用，保持独立的历史与工具上下文。

## 权限与沙箱
- 权限由 `permissions.py`（行 1-30）定义，`Guard` 类在 `agentforge/loop.py` 通过 `self.guard` 控制。`claim_write` 等函数限制文件写入，检查 `OUTSIDE_RULE`。

## 评测体系
- 评测脚本位于 `evals/`，调用 `agentforge/run.py`。`run.py` 读取 `models.json` 并用 `Agent.run()` 进行评测。相关代码行 50-70。

## 会话恢复
- `session.py`（行 1-30）管理会话 ID 与历史，`Agent.run()` 通过 `history` 传递。

## 记忆系统
- 长期记忆存放在 `cfg.state()/memory.md`，由 `MemoryStore`（行 1-30）管理。`Agent.memory.prompt_block()` 在 `_system` 里注入。

## skill 方案
- `skills.py`（行 1-30）实现 `SkillRegistry`，`Agent.skills` 用于提供技能列表。技能通过 `SkillRegistry.catalog()` 在系统提示中展示。