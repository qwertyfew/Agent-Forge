# rerun22 评测报告：模块耗时拆解 + 回答正确性核查

- 日期：2026-10-19
- 轮次：**rerun22**（prompt 顶部带「【本轮编号：rerun22】」，交付物独立命名 `evals/external-harness-gap-list-rerun22.md`）
- 运行：总耗时 **717.9s**，20 次工具调用，28 次 LLM 调用
- 对比：rerun21（单任务，186K 累计 token，0 闸门/落盘成功）

---

## 0. 结论速览

1. **慢在 LLM，不在流程，更不在工具。** 总耗时 717.9s 中，LLM 回复 **641.8s（89.4%）**，流程开销 76.0s（10.6%，主要是开局 assign/warmup 38s + 3 次 skill 加载），工具执行 **0.16s（0.02%）**。
2. **LLM 内部再拆**：纯生成（Ollama eval）388.4s（占总 54.1%），预填充/排队/网络 253.4s（占 LLM 39.5%）。随轮次推进，单轮 prompt 从 7.5K 涨到 33K，单轮耗时从 ~1s 涨到 65s —— **上下文逐轮重发（O(n²)）是耗时放大的主因**。
3. **回答正确性不合格**：交付物 **0 行号**（任务硬性要求「文件/行号」）；AgentForge 现状 10 条中 **6 条有实质错误**（2 处「缺乏」是假、1 个路径不存在、1 处指错文件、1 处会话恢复映射错、1 处任务清单判断错）。

---

## 1. 耗时拆解（harness 在任务中统计）

| 模块 | 耗时 | 占比 | 说明 |
|---|---:|---:|---|
| **总耗时** | 717.9s | 100.0% | 派发到收尾全流程 |
| **LLM 回复** | 641.8s | **89.4%** | 28 次调用；其中 Ollama 纯推理 388.4s |
| ├ 纯生成（eval） | 388.4s | 54.1% | Ollama `eval_duration`，真实出 token 的时间 |
| └ 预填充/排队/网络 | 253.4s | 35.3% | `llm_ms − eval_ms` 的差额，随上下文增大而涨 |
| **工具执行** | 0.16s | **0.02%** | 见下方明细 |
| **流程开销** | 76.0s | 10.6% | 路由 / 编排 / 序列化 / 收尾 / skill 加载 |

工具耗时明细（`loop._execute` 实测 wall-clock，`ops_batch` 脚本内工具也计入）：

| 工具 | 合计 | 说明 |
|---|---:|---|
| ops_batch | 0.10s | 共 13 次脚本执行，单次 0.9–13.2ms；脚本内 32/9/10 次工具调用也在此范围内 |
| find_tools | 0.04s | 共 7 次，后面 4 次被去重回放 |
| write_file | 0.00s | 1 次直接调用被 PTC 顶层守卫拒绝（未执行） |

> **口径提醒**：派发脚本里 `tokens=542154` 是**累计**（prompt+completion 求和），不是上下文窗口；单轮 prompt 最大 **32,826**。工具耗时极小不是漏计——`dur_ms` 是进程内实测，且与派发侧时间戳一致（工具调用都在同秒内完成）。

---

## 2. 逐次 LLM 耗时趋势

| 轮次 | LLM 墙钟 | 其中生成 | 预填充/其他 | 单轮 prompt |
|---:|---:|---:|---:|---:|
| 1 | 23.9s | 6.6s | 17.3s | 7,471 |
| 5–8 | 0.8–1.9s | 0.2–0.3s | 0.6–1.6s | 9,963–10,630 |
| 12–16 | 15.4–31.5s | 8.6–22.7s | 1.8–10.5s | 13,456–18,541 |
| 20–24 | 19.8–44.5s | 3.4–26.4s | 0.9–18.9s | 24,436–29,485 |
| 25（峰值） | **64.9s** | 45.3s | 19.6s | 30,552 |
| 28（末轮） | 29.2s | 9.9s | 19.3s | 30,260 |

**读法**：早期几轮 1–8 秒；进入中后段（上下文 20K+）后普遍 20–65s，且「预填充/其他」从 ~1s 涨到 ~20s。**流程和工具都不是瓶颈，瓶颈是每轮把整段历史重发给模型**（prompt 越大，prefill 越慢、生成越慢）。

---

## 3. 回答正确性核查

### 3.1 硬性格式

| 要求 | 结果 |
|---|---|
| 四列格式 | ✓ 满足 |
| 覆盖 10 个对比点 | ✓ 全部覆盖 |
| 「按性价比排序的借鉴清单」≤5 条 | ✓ 5 条 |
| **每条注明「文件/行号」** | ✗ **0 行号**，只有文件路径，无任何行号 |

### 3.2 逐条核验

| # | 对比点 | 对方做法（交付物称） | 真实情况（已核验） | 判定 |
|---:|---|---|---|---|
| 1 | agent loop | `s17_goal_loop/code.py`：Goal Loop + 完成条件 | s17 确实存在（goal loop 变体）。但**对比基线选错**：基本 agent loop 应是 `s01_agent_loop`，用 s17 比「loop 结构」不对等 | ⚠ 基线错 |
| 2 | 工具调用 | `mini-swe-agent/src/minisweagent/`：极简原生调用 | 真实：`default.py` `run()` 是 while 循环 → `query()` → `execute_actions()`；且仓库同时提供 **text-based** 模型（`litellm_textbased_model.py`）。交付物漏掉「文本兜底」，未回答题目要求 | ⚠ 不完整 |
| 3 | 上下文压缩 | AgentForge `agentforge/tools/memory.py`「缺乏实时 token 预算压缩」 | **假**：`agentforge/context.py` 有 `class Compressor`(190)、`compact()`(276)、`truncate_tool_results()`(116)，loop 里 `_compact()` 每轮调用；T3 已实装工具结果老化。引用的 `tools/memory.py` 是记忆**工具壳**，不是压缩实现 | ✗ 事实错误 |
| 4 | TODO/任务清单 | AgentForge `agentforge/tools/notes.py`「缺乏强绑定任务清单」 | **不准**：有 `agentforge/planner.py`、`taskstore.py`（结构化任务状态）、`tasksession.py`（事件日志）。`tools/notes.py` 只是笔记工具 | ✗ 判断错误 |
| 5 | 子代理/上下文隔离 | 「AgentForge **Core** 调度逻辑」 | **路径不存在**：`agentforge/core/` 没有（已核实 MISSING）；实际在 `session.py` / `tasksession.py` / `orchestrator.py` | ✗ 虚构路径 |
| 6 | 权限与沙箱 | `agentforge/tools/shell.py`「缺乏细粒度权限审批」 | **假**：`agentforge/permissions.py` 有 `class Guard`(36)、`PermissionDenied`(32)、`check_command`(89)、`claim_write`(147)。`tools/shell.py` 是命令执行工具壳 | ✗ 事实错误 |
| 7 | 评测体系 | mini-swe-agent README：SWE-Bench Verified | 真实：README.md:28「>74% on SWE-bench verified」（可引用，但交付物未给行号） | ⚠ 缺行号 |
| 8 | 会话恢复 | `s11_background_tasks/code.py`；AgentForge `tools/memory.py` | **映射错**：会话恢复/续跑对应 **s10_task_system**（file-persisted task graph），s11 是后台任务（不同概念）；AgentForge 侧在 `resume.py` / `tasksession.py`，不是 memory.py | ✗ 映射+路径错 |
| 9 | 记忆系统 | `agentforge/tools/memory.py` | 文件存在但**指错层**：核心是 `agentforge/memory.py`（MemoryStore），`tools/memory.py` 是工具封装 | ⚠ 指错层 |
| 10 | skill 系统 | `agentforge/tools/skill.py` | 文件存在但**指错层**：核心是 `agentforge/skills.py`（SkillRegistry） | ⚠ 指错层 |

### 3.3 正确性小结

- **对方（external）侧**：10 条路径基本真实（session 编号对得上），但 **0 行号**、且第 1/2 条基线或要点没答到（s17≠基本 loop；漏文本兜底）。
- **AgentForge 侧**：**6 条实质错误**——第 3、6 条把「已有能力」说成「缺乏」；第 5 条引用不存在的 `agentforge/core/`；第 8 条映射与路径双错；第 4 条判断不准；第 9、10 条虽文件存在但指到工具壳而非核心模块。
- **净结论**：这份清单「看起来像」一份合格报告，但**结论层不可信**。若按它去改 AgentForge，会重复造轮子（压缩、权限都已存在）。

---

## 4. rerun22 暴露的 harness 问题

1. **证据闸门只拦「路径形式」，不拦「语义」**：本轮 write 被拦过几次（外部路径缺 `external/` 前缀），但最终写盘成功时仍含 `agentforge/core/`（不存在）和两处「缺乏」假结论 —— 说明证据闸门没有对「被引用的 AgentForge 路径是否真实存在 / 是否 read_file 过」做硬校验。
2. **`from tools import X` 仍会静默失败**：模型 6 次写盘脚本里多次用 `from tools import write_file / list_dir...`，全部返回「0 次工具调用」（ImportError），改用 `call()` 才成功。虽然已注入裸 `call()`，但 `tools` 这个壳没有实现 `__getattr__`，模型仍会踩坑，**白烧 4–5 轮**。
3. **弱模型在「证据纪律」上不可靠**：反复 6 次写同一文件、20 次工具调用，交付物仍只有 4.4KB 且无行号。提示词约束不足，需要硬闸（行号缺失可提示、否定性结论要求程序核验）。

---

## 5. 建议（按性价比）

1. **证据闸门升级为「存在性 + 可复现」双校验**：交付物里引用的每个路径必须先 `read_file` 且真实存在；对「缺乏/没有/未实现」类结论，程序 grep 目标能力关键词，命中即驳回。
2. **`tools` 壳加 `__getattr__`**：`from tools import read_file, write_file, ...` 返回 `call(name, ...)` 包装，消除这一类静默失败。
3. **行号硬要求落地**：交付物格式要求「文件:行号」，程序校验引用带 `:数字` 或不带则提示（本轮 0 行号未被拦）。
4. **耗时侧**：已能定位「LLM 占 89%」。若要继续压，方向是 **减少轮次 + 控制上下文**（工具结果老化阈值 / 禁止把整文件 read 结果回传），不是优化工具或流程。

---

## 附：本次评测用到的 harness 计时能力（已实装）

- `agentforge/loop.py`：`stats` 事件带 `llm_ms`（LLM 墙钟）+ `eval_ms`（Ollama 纯推理）；`tool_result` 事件带 `dur_ms`（工具执行墙钟）。
- `webui.py`：`stats` 的 SSE 白名单补 `llm_ms`/`eval_ms`（`tool_result` 本就原样透传）。
- `debug/dispatch_task.py`：流式累加并在收尾打印「总耗时 / LLM / 工具 / 流程」四段 + 工具明细。
