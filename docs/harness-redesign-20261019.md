# Harness 重设计：任务现场（参照 DeepSeek Harness 三段式）

日期：2026-10-19 ｜ 触发：rerun13 实证「同任务中，后面的员工不知道前面员工做了什么」

## 一、现状诊断（代码级实证）

1. **leader 双环路径员工上下文 = 空对话 + 自己的 goal**（`orchestrator.run_one`：
   `convo = []`，prompt 只有 `t.goal`）。跨步骤信息只靠 leader 把上批结果转写进
   下一步 goal——两道手、有损，且 leader（gpt-oss）在 rerun13 写出
   「假设已存在于 evals/learn-claude-code-analysis.json」这类臆测。
2. **批内并行竞态**：leader 只被提示词约束「仅相互独立的才同批」，rerun13 的
   l4（写 JSON）/l5（读 JSON）同批并行，l5 先跑完 → 找不到输入。
3. **错位升级**：下游员工找不到文件 → ask_user 弹窗问用户。但该文件是本任务
   上游步骤的预期产物，不是用户才知道的信息。
4. 员工自述摘要不可信：rerun12 声称「已写入并验证」实际 7 次写入全被拒；
   rerun13 l7 声称完成但文件不存在。

## 二、DeepSeek Harness 模型 → AgentForge 映射

| DeepSeek 概念 | AgentForge 对应物 | 状态 |
|---|---|---|
| Header（会话元数据） | TaskStore 任务记录 + TaskSession.sid/目录 | 本批 |
| Event Log（不可变追加日志） | `.sessions/<task_id>/events.jsonl`（seq/ts/type/载荷） | ✅ 本批落地 |
| Surface（上下文视图，可压缩替换） | `TaskSession.worker_brief()`：从事件重建「任务现场」块注入员工 prompt | ✅ 本批落地（简化版：每次重建，暂不做摘要替换） |
| Step / Turn | 小步骤（run_one 一次完整 agent 循环）/ 一次用户请求 | 既有概念对齐 |
| Inbox·Follow-up（追加任务→新 Turn） | 暂无；用户追加即新请求 | Phase 2 |
| Inbox·Steering（中途补充→当前 Turn 采纳） | 暂无；用户只能停止重来 | Phase 2 |
| Inbox·Inject（插件补信息→下 Step 读取） | 部分：守卫打回/评测答复/澄清答复都是直接回灌 | Phase 2 队列化 |

## 三、本批落地（Phase 1）

新模块 `agentforge/tasksession.py`：

- **Event Log**：`append(type, **fields)` 唯一写入入口，JSONL 追加、seq 单调、
  带 ISO 时间戳；同任务重跑另开 `<sid>.<HHMMSS>` 目录，绝不混流。
- **Surface**：
  - `worker_brief()` → 「任务现场」块：已完成步骤（真实状态+摘要+产出）、
    **产出文件磁盘实查**（存在/缺失分列）、硬规则「清单外的文件一律当作
    不存在；缺输入=上游未完成，基于现有材料继续并如实说明，不假设、不追问用户」。
    leader 路径每个员工开局拼进 prompt（`run_prompt = goal + brief`；验证闸门
    仍只看原 goal，防 brief 文本被误当成任务书声明）。
  - `auto_answer(question)`：员工追问文件下落时按事实自动作答——已产出→
    「直接 read_file」；登记过但缺失→「上游失败，继续并如实说明」；goal 提过
    但未产出→「第 N 步尚未执行到产出」。认不出的追问照常真问用户。
  - `step_start/step_end/leader_eval` 事件：步骤真实状态（failed 标志）与真实
    产出（file_log 台账）由程序记入，不采信员工自述。
- **批内依赖守卫** `batch_dependency_reason()`：程序级侦测——①两个 goal 共享
  同一路径 token（产出→消费嫌疑）；②「上一步/已生成/已存在」类措辞。命中即
  整批降级串行，并向前端播报原因。leader 漏声明 depends_on 也不再引发竞态。
- **interact 自动答复钩子**：`set_auto_answer/clear_auto_answer`（线程局部），
  `ask()` 最先裁决；员工运行前挂上、收尾摘掉。
- **write_file 漏参提示**：缺 `path` 的 TypeError 附期望签名（rerun13 l4 连败
  两次同因，干巴巴的 Python 报错没帮上忙）。
- `run_task`/`run_leader` 增加 `task_id` 透传，webui 传入真实任务 id。

事件类型（现阶段）：`step_start`(index/task_id/title/goal/model)、
`step_end`(index/title/summary/files/failed)、`leader_eval`(round/done/reason)。
Phase 2 扩展：`artifact`、`inject`、`steering`、`summary_replace`（Surface 压缩）。

## 四、Phase 2 路线（未实施）

1. **Inbox 三通道**：每任务消息队列；Steering 接前端「中途补充」（替代停止
   重来）；Inject 承接守卫/评测/澄清回灌；Follow-up 支持同任务追加。
2. **Surface 压缩**：长任务时把 N 条 step_end 摘要压缩成一条 `summary_replace`
   事件，替换视图而非日志（日志永不动）。
3. **跨 run 延续**：同任务重跑时可选载入上次事件日志，员工 brief 含历史产出。
4. 旧多步路径（PROGRESS_TEMPLATE 接力）同样接入 TaskSession 事实核验。

## 五、验证

`tools/test_tasksession.py` 22 项（Event Log 追加性/同 sid 隔离/Surface 重建/
worker_brief/自动答复四分支/interact 钩子挂摘/依赖侦测四例/路径抽取边界）；
leader/planner/router 单测回归全过。
