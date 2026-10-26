# 04 · Leader 双环架构（第 47 步：七步架构更新）

> ⚠️ **已退役（第 53 步，2026-10-26 砍多 LLM）**：本文描述的 Leader 双环 / 员工内环 /
> 维度指派已整体删除（`agentforge/leader.py`、`dispatcher.py`、`orchestrator.run_leader`）。
> 现行架构是「单模型 + 规则拆解」，见 README 特性与 `agentforge/orchestrator.py` 的 `run_task`。
> 本文仅作历史存档。

> 2026-10-17 用户口述新逻辑并逐条确认落地。本文记录现行实现的真实结构。

## 总体结构

```
run_flow（orchestrator.py）
  └─ run_leader（调度路径，取代旧 run_multi_agent）
       外环（Leader，决策模型）                    内环（员工，各执行模型）
       ┌────────────────────────────┐              ┌──────────────────┐
       │ 步骤② 规划本批小步骤(1~N) ──┼──并行派发──▶ │ 完整 agent 循环   │
       │   仅相互独立的才可同批      │              │ 多轮工具调用      │
       │ 步骤④ 评估：done?规划?精简 ◀─┼──收齐结果──  │（上下文只有小步  │
       │   一次调用三件事同时做      │              │ 骤提示词，无总任务│
       └────────────────────────────┘              └──────────────────┘
            │ done=true → 拟回答
            ▼
       步骤⑤ 程序裁定（_verify_gate：验证命令亲跑/交付物核查，沿用）
            + Leader 语义判定（只看 原文+拟回答）
            ├─ 通过 → 正式回答
            └─ 不过 → 失败原因入槽，回到步骤②（重试 ≤ leader.max_retries）
```

## Leader 上下文：固定槽位（架构级保护）

```
[system] [任务原文槽·不可变] [失败原因槽·最近K条] [历史摘要槽·滚动] [本批全量·只活一轮]
```

- **任务原文、失败原因**放独立槽位，任何精简/替换只发生在摘要槽——不是提示词
  里的"请勿压缩"，是架构上压缩器根本碰不到这两块；
- **本批员工回复全量进 Leader，不截断**（步骤④「更新完再压缩，最新一轮全量进」）；
- 摘要槽由 Leader 评估输出里的 `note` 字段**滚动覆盖**（Leader 没给时程序按批次
  要点兜底追加）——Leader 上下文头部字节级稳定，DeepSeek 等服务端上下文缓存
  可完整命中（第 46 步诊断出的"缓存被架构性击穿"由此根治）；
- 失败原因由 Leader 判"未完成"时给出，**累积保留最近 K=3 条**（`REASONS_KEEP`），
  旧教训不被新失败冲掉，也不无限膨胀。

## 六类判定表（2.2：保留六类）

`coding / inspect / writing / math / analysis / general`——Leader 提示词（`LEADER_PLAN_SYSTEM`）
硬性要求"每个小步骤能概括为六类中**一类且仅一类**"。`leader.py` 的 `parse_batch`
对非法 kind 用 `planner.kind_of` 按文本兜底推断。

## 两条提示词边界（2.1，写死在 Leader 提示词里）

- **必须自包含操作信息**：执行者只看这一条就要能干活——路径、文件、期望产出
  全部写明；原文的交付物要求与验证命令原样保留进对应步骤；
- **不要包含背景叙事**：不复述任务总目标与整体方案，员工不知道项目全貌。

员工拿到的用户消息 = Leader 的 prompt 原文（不再有旧 AGENT_TASK_TEMPLATE 的
【总任务】全文块）。**"小步骤"≠"一次调用"**：一步能力仍可能多轮工具调用（内环保留）。

## 并行（保留核心设计）

Leader 每轮输出"本批 1~N 个小步骤，**仅相互独立的才可同批**"；架构并行发给
员工（各起一线程，本地模型受 local_serial 闸门约束）、收齐再进下一轮评估。
与 README「核心设计：任务的划分实打实决定执行节奏」完全一致。

## 步骤⑤ 判定权分界（用户确认）

| 判定 | 归属 |
|---|---|
| 验证命令是否通过（程序亲跑） | 程序 |
| 交付物存在/本轮更新（mtime 快照比对） | 程序 |
| 拟回答是否覆盖原文全部要求（语义完成度） | Leader 模型 |

语义验收调用失败不拦回答，如实附一句系统侧声明；重试到上限仍未通过则放行并
附系统侧声明（与旧闸门"到限放行附报告"同语义），绝不死循环。

## 配置（webui 设置页「模型管理」底部可直接改）

| 配置 | 键 | 默认 | 说明 |
|---|---|---|---|
| 评估环轮次上限 | `leader.max_rounds` | 12 | Leader 上下文有槽位保护、头部稳定可吃缓存，轮次成本远低于员工执行，可以给多 |
| 语义验收重试上限 | `leader.max_retries` | 2 | 与任务级闸门「至多三次执行」同口径；0 = 验收不过也放行（附声明） |

前端改动即时 POST `/api/models {action:"set_leader"}` 落盘 + 热更新。

## 退役清单（旧一次性拆分机器）

`dispatcher.plan/_llm_plan/_rules_plan/_enforce_goal_fidelity/missing_deliverables/
SharedState 黑板/synthesize/DISPATCH_SYSTEM/SYNTH_SYSTEM`、orchestrator 的
`run_multi_agent/_topo_order/_ready_batch/_produced_files`、配置项
`goal_fidelity_gate/dispatcher_max_tasks/dispatcher_synthesis`、测试
`tools/test_goal_fidelity.py`。

**语义去向**：旧 goal 保真（交付物名词比对）由两层接棒——①Leader 提示词要求
交付物原样进对应步骤 prompt；②步骤⑤语义验收对照原文逐项核对。旧 SharedState
接力简报由 Leader 滚动摘要接棒。旧 `Dispatcher._resolve` 指派（防自派/防重活给
决策模型/点名偏弱改派）**原样保留**，Leader 批次解析直接复用。

## 代码位置

| 部件 | 位置 |
|---|---|
| 提示词 ×3、LeaderContext、parse_batch/parse_eval | `agentforge/leader.py` |
| run_leader 主循环（事件流兼容旧前端） | `agentforge/orchestrator.py` |
| 指派 `_resolve`、翻译桥接 | `agentforge/dispatcher.py` |
| 配置字段/解析 | `agentforge/config.py`（`leader:` 块） |
| 设置端点 + 配置文件回写 | `webui.py`（`set_leader` / `_write_leader`） |
| 前端设置项 + leader_round 胶囊 | `web/index.html` |
| 单元测试（33 项，离线） | `tools/test_leader.py` |
