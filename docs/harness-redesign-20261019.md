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
| Inbox·Steering（中途补充→当前 Turn 采纳） | `agentforge/steering.py` 收件箱：任务运行中回车插话，员工下一轮推理 / leader 下批规划立即采纳 | ✅ Phase 2 首项落地 |
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

## 四、Phase 2 路线

1. ~~**Inbox·Steering**~~ ✅（本批）：`agentforge/steering.py` 收件箱
   （与 runcontrol 同款协作式模型）。`submit` 仅收运行中任务（回落普通消息）；
   每条消息带 seen 集合，**并行批次每个员工各见一次互不抢占**；两个消费点：
   员工循环每轮推理前（`Agent.run(steer_check=…)`，注入为用户消息「立即采纳
   并调整方向」）+ leader 每批规划前（写进 reasons/note 硬约束下一批）。
   webui `/api/steer` + 前端「执行中回车=插话」+ `steered` 事件胶囊播报；
   任务收尾清箱（与 runcontrol.release 同点）。
2. **Inbox 三通道收尾**：Follow-up（同任务追加开新 Turn）；Inject 队列化
   （守卫/评测/澄清回灌走同一队列，可插队可合并）。
3. **Surface 压缩**：长任务时把 N 条 step_end 摘要压缩成一条 `summary_replace`
   事件，替换视图而非日志（日志永不动）。
4. **跨 run 延续**：同任务重跑时可选载入上次事件日志，员工 brief 含历史产出。
5. 旧多步路径（PROGRESS_TEMPLATE 接力）同样接入 TaskSession 事实核验。

## 五、验证

`tools/test_tasksession.py` 22 项（Event Log 追加性/同 sid 隔离/Surface 重建/
worker_brief/自动答复四分支/interact 钩子挂摘/依赖侦测四例/路径抽取边界）；
leader/planner/router 单测回归全过。

## 六、Phase 3：契约闸门——验证从「回合后返工」前移到「回合内自愈」（2026-10-19）

rerun14 实测定性：11 步任务里「任务书点名的交付物不存在」的核查拦截反复出现
（raw.log 中核查类消息 22 处），全部挂在**收尾**——员工分析做完、上下文里全是
素材，却宣布完成；收尾闸门才发现文件没落盘，只能整轮返工。这正是「严格审核、
做完以后核实返工」的形态，与 DSH「每一步四环节闭环（组装上下文→调模型→处理
响应→执行工具→observation 立即回灌）」的根本差距。

落地（commit 344828a）——契约两端：

1. **开工亮标准**：`tasksession.contract_paths / contract_block`——任务书点名
   要求**写出**、开工时不存在的文件 = 验收契约，显式写进员工 prompt。句子级
   判定避免误伤：写入动词须在路径之前（「写入 tmp/x.txt」算；「参考
   agentforge/loop.py」「读取上一步生成的 evals/x.json」不算，对冲词整句排除）。
2. **收工对账**：`Agent.run` 里模型不再调工具、想给最终答复时，程序先对契约，
   缺了**拦截**——不 yield assistant（那张「已完成」的嘴用户看不到），缺口当
   observation 塞回上下文继续跑（Claude Code Stop hook 同款）。此刻分析内容
   都在上下文里，补写只是落盘，不用重新分析。最多拦 2 次，拦满放行交收尾
   闸门兜底（闸门从「主要修正机制」降级为「最后防线」）。
3. 契约只从本步骤任务书（t.goal）提取，不掺任务现场里的缺失上游产物——
   那是别人的欠账，不该拦本步骤收工。

测试：`tools/test_contract_gate.py`（stub hub 端到端）全过；tasksession 22 /
steering 18 / leader 33 / planner / router 回归全过。

### Phase 4+（把「回合内闭环」推到底）

1. **核查全面前移**：收尾闸门的其余静态核查（引用 read 硬闸、事实回显）逐个
   变成收工拦截项——模型每次想收工，程序做全部静态核查，缺什么补什么，全过
   才放行；收尾闸门降级为纯报告。
2. **Follow-up**：同任务追加需求开新 Turn，复用任务现场。
3. **Inject 队列化**：守卫打回/评测答复/澄清回灌走同一队列。
4. **Surface 压缩**：step_end 摘要压缩成 summary_replace 事件（日志不动）。
