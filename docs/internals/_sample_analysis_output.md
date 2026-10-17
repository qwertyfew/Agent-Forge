## 1. Agent Loop 结构
核心循环位于 `agents/default.py` 第 88 行:
- `run()` 先准备系统/用户消息，随后进入 `while True` 循环。
- 每次循环调用 `step()` → `execute_actions()` → `query()`。
- 退出条件是最后一条消息 role 为 `exit`，或因限制抛出异常。

**证据**: `agents/default.py` 88‑124 行。

## 2. 工具调用方式
- 采用**工具调用（Tool Call）**模式：在模型响应的 `extra.actions` 中包含 `command` 字段。
- `LocalEnvironment.execute` 直接运行 bash 命令（`environments/local.py` 24‑30 行）。
- 也支持 **文本兜底**：若模型未返回 `actions`，`execute_actions` 仍会调用 `format_observation_messages`，返回空列表。

**证据**: `agents/default.py` 154‑157 行，`environments/local.py` 24‑30 行。

## 3. 上下文压缩策略
- 采用 `Template` 渲染时，`get_template_vars` 合并 `config`, `env`, `model` 变量。
- 没有显式的压缩算法；上下文保持完整，只有 `messages` 限制 `step_limit` 与 `cost_limit` 控制。

**证据**: `agents/default.py` 52‑64 行。

## 4. TODO/任务清单机制
- 通过 `Submitted` 异常实现任务提交：`LocalEnvironment._check_finished` 在命令输出首行为 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 时抛出。
- `InteractiveAgent` 在 `execute_actions` 捕获 `Submitted` 并询问是否新任务。

**证据**: `environments/local.py` 46‑56 行，`agents/interactive.py` 144‑160 行。

## 5. 子代理与上下文隔离
- 通过 `InteractiveAgent` 的 `mode` 以及 `whitelist_actions` 控制子代理行为。
- 子代理在 `InteractiveAgent` 内部执行，使用同一 `Environment`，但可以通过 `config.whitelist_actions` 区分。

**证据**: `agents/interactive.py` 24‑29 行，`execute_actions` 125‑139 行。

## 6. 权限与沙箱
- 本地环境使用 `subprocess.Popen`，并可设置 `cwd` 与 `env`。
- 没有专门的沙箱限制；可通过 `config.cwd` 控制工作目录。

**证据**: `environments/local.py` 13‑19 行。

## 7. 评测体系
- 通过 `tests/` 目录提供单元测试；核心评测在 `tests/test_models.py`。
- 评测主要验证模型成本、工具调用解析。

**证据**: `tests/test_models.py` 1‑30 行。

## 8. 会话恢复（中断续跑）
- `save` 方法在每一步 `finally` 里调用；序列化包含 `messages` 与 `config`。
- 通过 `load`（未显式实现）可恢复状态。

**证据**: `agents/default.py` 182‑190 行。

## 9. 记忆系统
- 记忆通过 `messages` 列表保存，序列化时包含 `messages` 与 `info`。
- 通过 `serialize` 与 `save` 实现持久化。

**证据**: `agents/default.py` 159‑179 行。

## 10. Skill 系统
- 目前没有 `skills` 目录；通过 `Model` 的 `format_message` 与 `format_observation_messages` 处理工具。
- 若需要自定义技能，可在 `models/utils/actions_toolcall.py` 添加。

**证据**: `models/litellm_model.py` 140‑151 行。

---

若需进一步细化某个维度，请告知。