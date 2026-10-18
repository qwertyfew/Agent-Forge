# rerun6 验证报告（五点改动端到端验证）

> 重跑任务：REA 长链路「逆向分析两个外部 harness，产出与 AgentForge 的逐项差距清单」
> 对比基线：rerun5（`.run/rerun_rea_events.rerun5.jsonl`，42896 行，卡在第 12 轮停止）
> 本轮数据：`.run/rerun_rea_events.jsonl`（84433 事件），最终 `complete`（12 步，multi）
> 配置差异：本轮开启 `agent.dispatcher.leader_as_worker: true`（验证 point 3）

## 一、总览（rerun5 → rerun6）

| 指标 | rerun5 | rerun6 | 结论 |
|---|---|---|---|
| `subtask_error` | **8**（7×Ollama 500 + 1×空输出）| **0** | ✅ point 2 500 重试兜住 |
| 执行模型 | 全 `gpt-oss:20b`(26) | 全 `gemma4:12b`(21) | ✅ point 3 决策模型下场执行 |
| 工具调用总数 | 777 | **142** | ✅ point 4 累积进度、不再从零重派 |
| 其中 read_file / search_code / list_dir | 230 / 314 / 180 | 44 / 10 / 74 | ✅ 重复抽取大幅减少 |
| `reassigned`（重派） | 大量 True | 几乎全 False | ✅ 少重派 |
| `leader_round` | 12 | 21 | 更多增量轮次喂进度 |
| `verify` 事件（收尾闸门） | 0 | 5 | 闸门正常跑完 |
| 完成状态 | 卡 12 轮被停 | `complete`(12 步) | ✅ 正常收敛 |
| 真实 stale/ghost 误判 | 0 | 0 | ✅ step52 回归干净 |

## 二、逐点结论

### ✅ point 2 — 员工 500 重试
`llm.py.chat_stream` 把「流结束却没 `done` / 出现非 JSON chunk」判为可恢复，整轮重请求、先缓冲再放出。
- 效果：`subtask_error` 从 8 降到 **0**，7 条 Ollama 500 与 1 条「连续 3 轮空输出」全部消失。
- 说明：员工模型本身仍会偶发 500，但重试后同 prompt 重发即过，不再中断子任务。

### ✅ point 3 — 决策模型常驻当员工
`leader_as_worker: true` 后，`dispatcher._resolve` 不再把决策/调度模型（gemma）从重活候选剔除。
- 效果：21 个子任务**全部由 `gemma4:12b`（即 dispatcher_model）执行**，决策模型规划完一轮后顺手执行小步骤，不再闲置。
- ⚠️ 调参提醒：本轮 `gpt-oss:20b` 完全没被用到（全部给了 gemma）。若你希望 gpt-oss 仍是主力员工、gemma 只在弱模型交不出时补位，把 `dispatcher_model` 设为 `gpt-oss:20b` 即可反转；两种取向都已在开关下可控。

### ✅ point 4 — 跨批次「工作汇报」治从零重派
`LeaderContext.add_progress` 累积每批结果，后续轮次 plan/eval 注入「已完成工作汇报」并提示「不要重做已完成步骤」。
- 效果：工具调用 **777 → 142**（读文件 5.2×↓、搜索 31×↓、列目录 2.4×↓）；`reassigned` 几乎全 False；任务从「卡 12 轮」变为「正常 complete」。
- ⚠️ 两点已知小坑（不影响正确性，仅可观测性）：
  1. `progress` 事件（前端阶段成果信号）本轮 0 条——`orchestrator.py:773` 的 yield 存在，但触发依赖 `local_log` 里 write 结果回填 `path`，且 SSE/重跑驱动未把它写进 jsonl。核心机制（add_progress）已证明生效，事件透出是后续小修。
  2. `task_done` 的 `files=[]` 是 harness 每步文件追踪未回填，交付物**实际已落盘**（见下），不是没产出。

### ✅ point 5 — 失败工具入能力缺口台账
`gaps.py` 新增 `tool_failed` 种类，`loop.py` 工具返回 `[错误]` 时按 (工具名) 合并计数入 `.agentforge/gaps.json`。
- 效果：本轮台账新增 3 条 `tool_failed`：
  - `read_file` ×2（文件不存在：路径推断错）
  - `edit_file` ×1（待替换文本未找到）
  - `list_dir` ×1（路径不存在：`...agent-forge\.` 尾点）
- 这些正是「反复失败/路径推断错」的工具，现在成为能力缺口栏目的权威来源（此前栏目只记 tool_missing/capability_missing）。

### ✅ point 6 — 软件类工具
确认无需改：`node v22.22.2`、`git 2.55.0` 已在 PATH；harness 注册表已有 `run_command`（shell，可跑 node/git/python 任意命令）+ `build_git_tools`（git 专用）。

## 三、交付物与回归
- 交付物：`evals/external-harness-gap-list-20261016.md` 已被本轮改写（git status ` M`，18:11 修改），写入差距清单表（write_file×2 + edit_file×1）。
- step52 回归：真实 stale/ghost 误判 **0 条**（5 处提及均为员工模型在读 verify.py 源码注释，非闸门裁决）。

## 四、残留小项（非阻塞，待办）
1. `progress` 事件未透出到 SSE/jsonl —— 让 write 结果回填 `path` 到 `local_log`，并确认 SSE 层转发 `progress` 类型。
2. `task_done.files` 追踪未回填 —— 小步骤写盘的文件应回写进 task_done，闸门与前端才好引用。
3. point 3 调参：确认 gpt-oss / gemma 的主次分工（当前全给 gemma）。
4. point 1 术语对齐：按你要求留待后续，本轮未动文案/变量名。
