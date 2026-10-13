# AgentForge 跨对话交接提示词（Batch D）

> 用法：新窗口开一个对话，把下面「===== 提示词开始 =====」到「===== 提示词结束 =====」之间的内容整体粘贴进去。
> 日期：2026-10-12
>
> **归档说明（2026-10-13）**：本文是 Batch D 当时的交接提示词，留作历史记录，**不要再照着执行**。
> 文中反复提到的评测脚本 `_eval_novel.py` 已退休——它把 `D:\AI_develop\project\novel` 写死在源码里，
> 换台机器就跑不起来，留着只会让人误以为「上次过了 = 现在也对」。它守的契约（SSE 事件全表、
> 编排决策怎么复盘、以及自沉淀的三条不变量）已抽进 README「能力的契约：可观测、可验证、不自我污染」。

---

===== 提示词开始 =====

## 0. 你的角色与项目位置

你在维护 **AgentForge**，一个基于本地 Ollama + 在线模型（OpenAI 兼容 / Anthropic）的 **Agent Harness**（带工具调用、技能、多 agent 调度、Web UI）。

- 工作目录：`D:\AI_develop\project\agent-forge`（**所有命令都在这个目录下执行**）
- 环境：Windows + Git Bash；venv 解释器 `D:\AI_develop\project\agent-forge\.venv\Scripts\python.exe`
- 主端口：8787，带局域网配置 `config.lanrun.yaml`（`python run.py --config config.lanrun.yaml`）
- 硬件：NVIDIA RTX 5060 Ti，**16GB 显存**。已安装本地模型：`gpt-oss:20b`(13.8GB)、`gemma4:12b`(8GB)、`qwen3.5:4b`(3.3GB)。`qwen3:14b` **未安装**。
- 代码是分批次迭代的，当前已做完 **Batch A / B / C**，本次要做 **Batch D**。

开工前请先跑一遍：

```bash
cd "D:\AI_develop\project\agent-forge"
git status --short                 # 预期：11 个 M + dispatcher.py / _eval_novel.py / extracted_notes.txt 未跟踪
git diff --stat                    # 预期：1195 insertions(+), 111 deletions(-)
```

**当前改动尚未提交**，全部是 Batch A–D 的成果，不要 `git checkout` 掉。

---

## 1. 已完成的架构现状（必须理解，否则会改错）

### 1.1 两条编排路径

| 路径 | 入口 | 拆解方式 | 适用 |
|---|---|---|---|
| 规则编排 | `orchestrator.run_task` | `planner` 规则拆（split/kind/assign） | 单模型任务 |
| 多 Agent 调度 | `orchestrator.run_multi_agent` | **决策模型**产出 JSON 任务 | 多模型协作 |

**重要设计约束（用户明确要求过）**：不要描述成「任务在 LLM 之间流转」，而是 **「单 agent 架构升级成多 agent 架构，任务在 agent 之间流转」**。每个任务由 `spawn_agent()` 起一个全新的 `Agent` 实例执行。

### 1.2 决策模型／调度器（核心）

- 实现在 `agentforge/dispatcher.py`（**新文件**）。
- 决策模型自己也是一个普通 `ModelSpec`，配置项在 `config.yaml` / `config.lanrun.yaml` 的 `agent.dispatcher` 段。
  - `config.yaml` → `model: qwen3.5:4b`（本地模板）
  - `config.lanrun.yaml` → `model: "smart-mini"`（本机跑用这个）
- 决策模型只产出「任务清单 JSON」，然后由 `_resolve()` 把每个任务指派给合适的执行模型。
- 关键护栏：
  - `MODEL_CHOICE_GAP = 8`：若点名模型在该任务维度上比场上最强者弱 ≥8 分，改成用最强者，并置 `TaskSpec.reassigned=True`，在 `reason` 里写清原因。
  - `NOT_SIMPLE_KINDS = {"coding","math","writing","analysis"}`：这四个 kind 永不派给决策模型。
  - `_TOOL_HINTS`：goal 里出现「文件/目录/代码/运行/脚本/.py/read_file/...」等词，视为要动手。
  - `SIMPLE_CHAT_MAX_CHARS = 120`。
  - `is_simple_chat(t)`：`kind=="general"` **且** goal ≤120 字 **且** 不含工具提示词 → 只有这种才允许决策模型自己接。
  - `cfg.dispatcher_execute_simple`（`agent.dispatcher.execute_simple`）：设 `false` 则决策模型**纯调度**，一句闲聊也不接。
- `_extract_json()` 已重写为**括号配对扫描**，能容忍 ```json 围栏、前后 prose、字符串里的花括号、截断。

### 1.3 并行执行（Batch C item 4）

`orchestrator.run_multi_agent` 支持两种模式：

- **串行接力**（默认回退）：`run_serial()` + `_ready_batch(tasks, done)`，按 `depends_on` 顺序放行。
- **并行波次**：需同时满足 `cfg.dispatcher_parallel == true` **且** 权限为 full。`run_parallel(wave)` 用线程 + `queue.Queue` 跑一波，`spawn_agent()` 为每个任务建独立 Agent。
  - **本地串行闸门**：`threading.Semaphore(1)`（`local_gate`）。`_is_local_model()` 为真时拿闸门 → 同一时刻只跑一个**本地**模型；在线模型不受限。
  - **同文件写保护**：`permissions._WriteClaims` + `claim_write(path)`（模块级 `WRITE_CLAIMS`）。`loop._execute` 对 `FILE_TOOLS` 用 `os.path.normcase(os.path.abspath(path))` 作 key 包住 `tool.run`。同一文件串行、不同文件互不阻塞。
  - 每波结束发 `conflict` 事件（重叠写入时）。
  - `sink[t.id] = {"files", "failed", "convo"}`，并行模式下 `final_convo` 由各任务 convo 重建。

### 1.4 Provider 层

`ClientHub.client_for(spec)` 按 `spec.provider` 分发：

- `OllamaClient`（`llm.py`）：本地 `/api/chat`，原生 `tool_calls`（`arguments` 是 **dict**）+ `thinking` 字段。**只有它** 会去抢模块级 `threading.Lock`（`_LOCAL_SERIAL` 全局开关，`set_local_serial()`）。
- `OpenAIClient`（`providers.py`）：OpenAI 兼容。DeepSeek / SiliconFlow / Kimi / GLM / 通义都走这条。
- `AnthropicClient`：Claude 官方 `/v1/messages`。

`OpenAIClient._convert_messages()`（静态方法，已加）：把内部历史转成严格 OpenAI 形状 —— `arguments` dict→JSON 字符串、补 `id`/`type`、给 `role=tool` 补 `tool_call_id`（与前面 assistant 的 tool_calls 按序配对）。

### 1.5 前端（`web/index.html`）

- 模型页有调度器下拉 `#dispatcherModel` + 状态 `#dispatcherStatus`（`renderDispatcher()`，onchange 调 `set_dispatcher`）。
- 并行任务泳道：`lanes` / `addTaskLanes` / `laneOf` / `dropLanes` / `addLaneThinking`，`.lanes`/`.lane` CSS。
- 所有流式事件按 `task_id` 路由，避免两路 delta 串在一起。
- SSE 事件类型（`STREAM_EVENTS`）：`combo route dispatch plan assign bridge conflict tool_start task_done stats complete stopped error history`。
- `assign` 处理：当 `data.reassigned && !data.multi` 时显示「调度指派：… — {reason}」。
- `webui.py`：`_dispatcher_snapshot()` / `_write_dispatcher_model()` / `_models_snapshot` 注入 `snap["dispatcher"]`；动作 `set_dispatcher` / `set_local_serial` / `remove_model`。

---

## 2. 本次要做的两件事（Batch D）

### 任务 1：决策模型只做简单对话 + 调度

**用户原话**：

> 决策模型可以处理一些简单对话任务，比如不涉及工具调用、skill 调用、代码编写、数学计算等复杂任务的。决策模型仅处理简单对话以及负责分配任务、传递任务。尽量不要把需要性能/算力的任务分派给决策模型，我怕真实有能力的模型闲着，而决策模型因为体量小没有做好任务。

**当前状态：功能已实现，但需要在修完第 3 节三个 bug 后重新验证。**

已落地的部分（见 1.2）：

- `agentforge/dispatcher.py`：`is_simple_chat()` / `NOT_SIMPLE_KINDS` / `_TOOL_HINTS` / `SIMPLE_CHAT_MAX_CHARS`；`_resolve()` 里 `cands = without_disp`（非简单任务把决策模型从候选里剔除）、`allow_disp = dispatcher_execute_simple and is_simple_chat(t)`。
- `agentforge/config.py`：`dispatcher_execute_simple: bool = True`，从 `agent.dispatcher.execute_simple` 读。
- `config.yaml` / `config.lanrun.yaml`：`execute_simple: true` + 注释。
- `DISPATCH_SYSTEM` 提示词已加：「**你自己（调度器）体量小，只负责拆解、指派、交接和几句简单对话**；凡是要调工具、读写字、写代码、做数学的任务，一律留空交给系统挑最强的执行模型，绝不能自己上。」

**你要做的**：

1. 复查 `dispatcher.py` 的 `is_simple_chat` 与 `_resolve` 是否真的把「工具/代码/数学/写作/分析」全部挡在决策模型之外（含 `kind=="general"` 但 goal 里提到文件的漏网情况）。
2. 补/跑单元验证，至少覆盖：
   - `kind="coding"|"writing"|"math"|"analysis"` → 必派最强执行模型，`reassigned=True`；
   - `kind="general"` 但含 `/\.py/`、`read_file`、`运行脚本` → 不派决策模型；
   - 纯闲聊（如「你好，帮我解释一下什么是 Agent」）→ 派决策模型；
   - 场上**只剩决策模型**时 → 能优雅回退，不能崩；
   - `execute_simple=false` → 任何任务都不派决策模型。
3. 确认在真实调度里决策模型的**调用费用/token 占比极低**（它只该被调用 1 次做拆解，外加极少量闲聊）。

### 任务 2：用真实「小说整理」例子评测

**用户原话**：

> 以小说整理的真实例子评测。将模型配置选为自动配置所有模型，包括付费模型，再次执行之前的小说整理任务，看看效果。

现成的评测脚本：**`D:\AI_develop\project\agent-forge\_eval_novel.py`**（已写好，不要重写，直接修好环境后跑）。

它做的事：
1. `Config.load("config.lanrun.yaml")` → `serve(cfg, True, 8795)` 起 Web 服务；
2. POST `/api/models {action:"auto_config", scope:"best"}` → 路由切到「自动配置所有模型（含付费）」；
3. 打印路由模式 / 已启用模型 / 决策模型；
4. 用**逐字原文**的小说 prompt 打 `/api/chat`（`permission:"full"`, `auto_plan:true`），流式解析并打印全部 SSE 事件；
5. 最后打印 `outline.md` 前 40 行。

**小说任务背景**（第一次跑失败过，这是重跑的原因）：

- 素材：`D:\AI_develop\project\novel\notes_extracted\写作.txt`（281 条，约 3.3 万字）——**只有它**是小说素材，同目录其它文件（备忘/日记/游戏/tips/数学/论文/考公笔记/赚钱/思想/笔记）与小说无关，必须忽略。
- 输出：覆盖写 `D:\AI_develop\project\novel\outline.md`。
- 旧备份：`D:\AI_develop\project\novel\outline.before-eval.md`（gemma4:12b 时代的产物，可作对比基线）。
- **旧版本的错误**：把配角「小师弟」误判成主角（小师弟全篇仅 4 行，是三国故事线里的边缘搞笑角色；笔记是主角视角，「小师弟」这个称呼本身就说明他不是主角）。正确结果应识别出多条线（三国穿越线、雨林演化寓言线、现代都市线…），并判断哪条是主线。
- 上次**部分正确**的观察：付费模型确实把主角识别对了（刘备方向），但整轮跑崩在 provider 报错上。

**你要做的**：先看第 3 节，把三个 bug 修掉，再跑 `_eval_novel.py`，然后给出「本轮效果 vs. gemma4:12b 旧结果」的对比结论（拆解对不对、指派对不对、决策模型有没有被派重活、耗时、花费、`outline.md` 质量）。

**已知环境坑（务必先确认）**：

- `models.json` 里配的付费模型：
  - `paid:deepseek-deepseek-v4-pro` / `paid:deepseek-deepseek-flash` → **HTTP 402 余额耗尽**，跑之前先确认用户是否充值，否则**换用 SiliconFlow**。
  - `paid:siliconflow-deepseek-ai-deepseek-v4-flash` ✅
  - `paid:siliconflow-zai-org-glm-5.3` ✅
  - `paid:siliconflow-moonshotai-kimi-k2.7-code` ✅
  - `paid:siliconflow-qwen-qwen3.8-27b` ✅（**但它不接受结尾的 system 消息**，正是第 3 节 bug 的触发点）
- 评测脚本建议**后台跑**（上次约 3m15s 到 3 分钟级，付费模型更慢）。用 `run_in_background`。
- 跑完记得确认 `outline.md` 真被覆盖了，并与 `outline.before-eval.md` 对比。

---

## 3. 必须先修的三个 Provider 兼容 Bug（阻塞任务 2）

这三个 bug 在**严格的在线 provider**（DeepSeek / SiliconFlow / Qwen3.8-27B）上会直接把整轮跑崩。都是「消息形状」问题。

### Bug 1（最优先）— 历史尾部多了一条 system 消息

- 现象：`HTTP 400: "messages" in request are illegal: System message must be at the beginning.`
- 根因：`agentforge/loop.py` 第 64 行定义 `LANG_REMINDER = {"role": "system", ...}`，第 **315 行** 在每次循环里 `messages.append(dict(LANG_REMINDER))`，把一条 **system 消息追加到了 messages 末尾**。Ollama 容忍，严格 provider 直接 400。
- 修复方向（任选，优先选前两个）：
  1. 把语言要求**合并进 system prompt**（`messages_with_system(ctx)` 里拼），不再单独 append；
  2. 或改成 `role="user"` 的一条提醒；
  3. 兜底：在 `OpenAIClient._convert_messages()` 里把**非首位**的 system 消息**上提到开头**（或降级成 user），保证发出去的一定合法。
- 已定位到行号：`agentforge/loop.py:64`（定义）、`agentforge/loop.py:315`（append）。**尚未修**。

### Bug 2 — `reasoning_content` 必须原样回传（DeepSeek 思考模式）

- 现象：`HTTP 400: reasoning_content ... must be passed back`。
- 已做的修复：`agentforge/loop.py` 里 `replay_think`：
  ```python
  replay_think = (
      {"reasoning_content": result.thinking}
      if result.thinking and getattr(spec, "provider", "ollama") != "ollama"
      else {}
  )
  ```
  并混进 assistant 消息：`{"role": "assistant", "content": ..., **replay_think}`。
- **仍未解决**：上下文**压缩/摘要会重建 messages**，把 `reasoning_content` 丢了 → 长任务跑到中途又 400（上次在 146.3s 处复发）。
- 修复方向：在 `OpenAIClient._convert_messages()` 里，若某条 assistant 消息缺失 `reasoning_content`，**从原始历史里回填**（可对长度设上限，避免 token 膨胀）；或让 `context.py` 的压缩逻辑保留该字段。

### Bug 3 — 压缩把 `tool` 消息的 `tool_call_id` 丢了

- 位置：`agentforge/context.py` 的 `truncate_tool_results(messages, keep_recent)`（约 115–130 行）。
- 现象：对「较早」的 tool 消息，它重建成 `{"role": "tool", "content": "[已压缩] ..."}`，**丢掉了 `tool_call_id`**。OpenAI 严格要求 `role=tool` 必须能配对到前面 assistant 的 `tool_calls`，否则 400。
- 修复方向：重建时保留原 `tool_call_id`（以及 provider 需要的其它字段），例如一并 `**{k: v for k, v in m.items() if k != "content"}`；或只在 **Ollama 路径**下做这种精简，在线路径不动。
- **尚未修**。

> 建议顺序：Bug 1（改 `loop.py` + `_convert_messages` 兜底）→ Bug 3（改 `context.py`）→ Bug 2 的压缩回填。修完先跑一个「多轮工具调用 + 触发压缩」的小 smoke test，再跑小说评测。

---

## 4. 代码地图（改哪里）

| 文件 | 作用 | 本次关注 |
|---|---|---|
| `agentforge/dispatcher.py` | **新**。决策模型 prompt、任务 JSON 解析、能力护栏、指派 | 任务 1 主战场 |
| `agentforge/orchestrator.py` | `run_task`（规则）/ `run_multi_agent`（并行波次 + 串行接力） | 并行、local_gate、conflict、assign 事件 |
| `agentforge/loop.py` | Agent 主循环、工具执行、消息组装 | **Bug 1（第 64/315 行）**、Bug 2 的 `replay_think` |
| `agentforge/context.py` | 上下文压缩（L1 截断 / L2 摘要） | **Bug 2（摘要丢 reasoning）、Bug 3（`truncate_tool_results`）** |
| `agentforge/providers.py` | `OpenAIClient._convert_messages` / `AnthropicClient` / `ClientHub` | Bug 1 兜底、Bug 2 回填 |
| `agentforge/llm.py` | `OllamaClient`、本地串行 `threading.Lock`、`_LOCAL_SERIAL` | 本地串行开关 |
| `agentforge/permissions.py` | `_WriteClaims` / `claim_write` | 同文件写保护 |
| `agentforge/config.py` | `Config` / `ModelSpec`（含 `keep_alive`）、`dispatcher_*` | 配置读取 |
| `webui.py` | HTTP API + SSE | `set_dispatcher` / `set_local_serial` / `_dispatcher_snapshot` |
| `web/index.html` | 前端（泳道、调度器下拉、task_id 路由） | 并行可视化 |
| `config.yaml` / `config.lanrun.yaml` | 配置（`agent.dispatcher` 段） | 决策模型选型 |
| `_eval_novel.py` | **评测脚本（勿重写）** | 任务 2 |
| `docs/HANDOFF.md` | 本文档 | —— |

`.tasks.json`（462KB）、`.sessions/`、`.run/`、`__pycache__/` 是运行产物，**不要提交、不要乱删**。

---

## 5. 关键配置速查

```yaml
# config.lanrun.yaml
agent:
  dispatcher:
    enabled: true
    model: "smart-mini"      # 决策模型；config.yaml 模板里是 qwen3.5:4b
    max_tasks: 6
    parallel: true           # 需 full 权限；本地模型仍受 local_gate 约束
    translate: true
    bridge_threshold: 55     # 中文维度低于 55 的执行模型才做桥接（桥接=多两次调度调用）
    execute_simple: true     # false → 决策模型纯调度，不接任何对话
    synthesis: true
```

- `bridge_threshold` 从 70 调到 55 的原因：`gemma4:12b` 中文 62 分，70 会让它**每次都白绕一圈**（多 4 次 LLM 调用，e2e 47.4s → 28.0s）。
- 每个模型可加 `keep_alive: "0"`，用完立刻还显存（16GB 卡上很有用）。

**模型评分**（用于护栏判断）：

| 模型 | coding | reasoning | writing | math | chinese | speed | cost | context | 总分 |
|---|---|---|---|---|---|---|---|---|---|
| `gemma4:12b` | 78 | 82 | 78 | 84 | 62 | 62 | 94 | 86 | **78** |
| `qwen3.5:4b` | 66 | 72 | 66 | 74 | 78 | 82 | 95 | 80 | **74** |

---

## 6. 开发与验证约定

- **跑测试**：`D:\AI_develop\project\agent-forge\.venv\Scripts\python.exe <script>`；临时脚本放项目根，跑完删掉或明确标注。
- **起服务**：`.venv/Scripts/python.exe run.py --config config.lanrun.yaml`（端口 8787）；评测用 8795，避免撞端口。
- **消息形状 smoke test 建议**：写一个最小脚本，对 `siliconflow-qwen-qwen3.8-27b` 发两轮带工具的对话（制造 tool 消息），并人为触发 `Compressor` 压缩，确认不再 400。
- **改完必须验证**：不要只说「已修好」。给出实际命令 + 实际输出。修完 Bug 1/2/3 要**重新跑一遍 `_eval_novel.py`** 拿真实结论。
- **不要动**已经验证过的东西：`_extract_json` 的括号配对、`MODEL_CHOICE_GAP` 护栏、`claim_write`、本地串行锁 —— 这些都有测试覆盖，除非有证据说明它们错了。
- **提交**：本次沿用「一轮改动一个 commit」的节奏；提交前确认没把 `.tasks.json`、`.sessions/`、`__pycache__/` 带进去。

---

## 7. 交付物

1. Batch D 任务 1 的**验证结论**（决策模型有没有接到重活；`execute_simple=false` 的行为；回退路径）。
2. Batch D 任务 2 的**评测报告**：
   - 拆出了几个任务、分别什么 kind、`depends_on` 合不合理；
   - 每个任务派给了谁、有没有 `reassigned`、理由是什么；
   - **决策模型有没有被派重活**；
   - 耗时 / token / 花费；
   - `outline.md` 是否真的按素材重写（主角判断对不对、故事线有没有梳理出来）；
   - 与 `outline.before-eval.md`（gemma4:12b 旧版）的差异对比。
3. 三个 provider bug 的修复说明（改了哪几行、怎么验证的）。
4. 如果还有没修完的，明确列出来，不要含糊带过。

现在开始：先跑 `git status --short` + `git diff --stat` 确认工作区，再读 `agentforge/dispatcher.py` 和 `agentforge/loop.py` 的第 300–330 行，然后动手。

===== 提示词结束 =====

---

## 附：Batch D 实际执行结果（2026-10-12 回填）

> 本节是执行后追加的结果记录，不属于要粘贴的提示词。

### 第 3 节的 bug：修了 3 个，另发现 2 个

| # | 位置 | 状态 |
|---|---|---|
| 1 尾部 system 消息 | `loop.py`（保留末尾 system）+ `providers._convert_messages` 上提合并成开头唯一一条 | ✅ 已修，负向对照复现 400 后修复转 200 |
| 2 reasoning_content 丢失 | `context.py` 新增 `rehydrate_reasoning()`，压缩后按工具签名回填 | ✅ 已修 |
| 3 tool_call_id 丢失 | `context.truncate_tool_results` 改为 `dict(m)` 只改 content | ✅ 已修 |
| 4 **SSE 中文乱码（新）** | `providers.py` 两个客户端 `iter_lines(decode_unicode=True)` → `decode_unicode=False` + `raw.decode("utf-8","replace")` | ✅ 已修（**付费跑失败的真因**） |
| 5 **空轮次假成功（新）** | `loop.py` 无 tool_calls 且正文为空时不再当完成，重试 ≤2 次后报 error | ✅ 已修 |
| 6 **调度缩小任务范围（新）** | `dispatcher.DISPATCH_SYSTEM` 增加「绝不缩小任务范围」 | ✅ 已修 |

Bug 4 细节：OpenAI 兼容流的 `Content-Type: text/event-stream` 不带 charset，
requests 会按 ISO-8859-1 解码 → 中文正文与**工具参数**整段乱码
（`写作.txt`→`åä½.txt`、`蔡文姬`→`è¡æå§¬`），文件读不到、搜索全 miss，agent 空转。
本地 Ollama 原生 `/api/chat` 是 `application/x-ndjson`（encoding=None 走 bytes），**不受影响**。

### 任务 1
`dispatcher.py` 护栏逻辑经复查正确，未改判定逻辑。新增离线单测 25 项全过；
真实调度中决策模型只被调用 1 次做拆解，重活一律 `reassigned` 给执行模型。

### 任务 2
- 付费跑通一次（GLM-5.3 / 14m44s / 约 ¥2.4）→ **未写出 outline.md**，根因即 Bug 4。
- 随后 **DeepSeek 与 SiliconFlow 账号双双 402 Insufficient Balance**，付费重跑受阻。
- 改用本地-only（免费）连跑三次数：修掉 Bug 5、Bug 6 后**成功重写 outline.md**
  （72.5s / 4 次调用 / 全免费）：识别出三条故事线、主线判为三国穿越线、
  主角判为刘备、**「小师弟」明确标为边缘配角**（原错误消除）。

### 仍未完成
付费模型下的评测复跑（需先充值）。步骤：`models.json` 里欠费模型删掉或充值 →
`.venv/Scripts/python.exe _eval_novel.py`。
