# AgentForge Batch D 执行报告

日期：2026-10-12 ｜ 依据：`docs/HANDOFF.md`
工作目录：`D:\AI_develop\project\agent-forge`

---

## 0. 工作区确认（开工前）

```
$ git status --short
 M agentforge/cli.py / config.py / llm.py / loop.py / modelstore.py
   orchestrator.py / permissions.py / providers.py / config.yaml
   web/index.html / webui.py          (11 个 M)
?? _eval_novel.py  ?? agentforge/dispatcher.py
?? docs/HANDOFF.md ?? extracted_notes.txt

$ git diff --stat
 11 files changed, 1195 insertions(+), 111 deletions(-)
```

与 HANDOFF 预期完全一致，未做任何 `checkout`。

---

## 1. Provider 兼容 Bug：文档列了 3 个，实际修了 5 个

### Bug 1 — 历史尾部多了一条 system 消息（400）

- 位置：`agentforge/loop.py`（`LANG_REMINDER`）+ `agentforge/providers.py`（`OpenAIClient._convert_messages`）
- 方案：**保留**「语言提醒以 system 贴在消息列表末尾」（本地 Ollama 的注意力优势不丢），
  改为在 `_convert_messages` 里把所有 system 消息收拢、合并成**开头唯一一条**再发出。
- 为什么不改成 `user` 角色：实测那样模型会把提醒当成一轮新对话去「回复」它
  （付费跑里出现过 `收到，最终回复用简体中文。…`），反而打断干活节奏，已回退。

验证一（负向对照，证明 400 真实存在）：

```
直接发送 [{system:...},{user:...},{system:提醒...}]
→ HTTP 400  {"code":20015,"message":"\"messages\" in request are illegal:
              System message must be at the beginning..."}
经 _convert_messages 上提后 → HTTP 200
```

验证二（离线形状断言）：`system 只出现一次 / 位于开头 / 两条已合并` 全部 PASS。

### Bug 2 — `reasoning_content` 压缩后丢失（400）

- 位置：`agentforge/context.py`
- 新增 `rehydrate_reasoning(original, compacted)`：按「工具调用指纹（名字+参数）」
  把丢掉的 `reasoning_content` 回填；`Compressor.compact()` 所有返回路径统一过一次。
  找不到对应指纹就保持原样（不张冠李戴）。

### Bug 3 — `truncate_tool_results` 丢 `tool_call_id`（400）

- 位置：`agentforge/context.py` `truncate_tool_results`
- 原来重建 `{"role":"tool","content":"[已压缩]…"}`，`tool_call_id` 被丢掉。
- 现在 `dict(m)` 只改 `content`，`tool_call_id` / `name` 等字段全部保留。

### Bug 4 — 【新发现·付费跑失败真因】SSE 中文乱码

- 位置：`agentforge/providers.py`（`OpenAIClient.chat_stream` + `AnthropicClient.chat_stream`）
- 原因：OpenAI 兼容流的 `Content-Type: text/event-stream` **不带 charset**，
  `requests` 于是把 `resp.encoding` 认成 `ISO-8859-1`；原来的
  `resp.iter_lines(decode_unicode=True)` 就按 latin-1 解码 → 中文全乱：
  正文、以及**工具调用参数**（文件路径、搜索词）都坏掉。
- 修法：
  ```python
  for raw in resp.iter_lines(decode_unicode=False):
      ...
      line = raw.decode("utf-8", "replace").strip()
  ```
- 危害实证（付费跑日志）：`写作.txt` → `åä½.txt`（read_file 读不到）、
  `蔡文姬` → `è¡æå§¬`（search_code 全部「未找到匹配」）→ agent 反复空转。
- 对照验证（同一份字节流，本地 Ollama）：

  | 端点 | Content-Type | requests.encoding | 结果 |
  |---|---|---|---|
  | `/v1/chat/completions` | `text/event-stream` | `ISO-8859-1` | latin-1 → `ä½ å¥½ï¼ä¸çã` ❌ |
  | `/api/chat`（原生） | `application/x-ndjson` | `None`（走 bytes） | UTF-8 → `你好，世界。` ✅ |

  修复后 `OpenAIClient` 得到 `你好，世界。` ✅

### Bug 5 — 【新发现】空轮次被当成「任务完成」

- 位置：`agentforge/loop.py`
- 现象：思考模型偶尔返回「**无 tool_calls 且正文为空**」的一轮；原代码直接收尾，
  界面显示 `task_done files=[]`，实际零产出（两次跑都这样，与模型无关）。
- 修法：连续空轮次 ≤2 次时注入一条「你上一轮没有输出，素材读完就直接
  `write_file`」的 user 提示并 `continue`；超过 2 次报 `error` 退出。不再假成功。

### Bug 6 — 【新发现】决策模型私自缩小任务范围

- 位置：`agentforge/dispatcher.py` `DISPATCH_SYSTEM`
- 现象：qwen3.5:4b 把「通读素材并**覆盖写入 outline.md**」改写成
  「仅提取剧情分析与主线判定，**不执行文件写入操作**」→ 执行者照 goal 干活，
  根本不写 outline.md。
- 修法：提示词增加「**绝不缩小任务范围**：用户要求的产物（文件名/路径/格式）
  必须原样出现在 goal 里」。

---

## 2. 任务 1：决策模型只做简单对话 + 调度

- 复查 `is_simple_chat` / `NOT_SIMPLE_KINDS` / `_TOOL_HINTS` / `_resolve`：逻辑正确，未改判定。
- 离线单测（临时脚本 `_smoke_batch_d.py`，**跑完已删**）25 项全 PASS：
  - `kind ∈ {coding, writing, math, analysis}` → 必派最强执行模型且 `reassigned=True`
  - `kind="general"` 但含 `.py` / `read_file` / `运行脚本` / `文档` / `函数` → 不派决策模型
  - 纯闲聊（≤120 字、无工具词）→ 派决策模型
  - 场上只剩决策模型 → 优雅回退，不崩
  - `execute_simple=false` → 任何任务都不派决策模型
  - 点名的弱模型（差 ≥8 分）→ 换成最强者
- 真实调度佐证：决策模型只被调用 **1 次**做拆解；重活一律 `reassigned`，
  理由 `决策模型不接这类要动手/要算力的任务，改派 <最强执行模型>`。

## 3. 任务 2：小说整理评测

### 3.1 付费跑（GLM-5.3，14m44s，约 ¥2.4）→ 失败，但定位到真因

- 拆解：1 个任务、`deps=[]`、`source=llm`、`parallel=False`（单任务）
- 指派：`任务#1 → GLM-5.3`，`reassigned=True`（决策模型不接）
- 工具序列：54 次调用，**全是 read/list/find/run/search，0 次 write_file**
- 结果：`task_done files=[]` → **outline.md 未被写入**
- 根因：Bug 4（乱码工具参数 → 读不到文件、搜索全 miss → 空转）

### 3.2 账单

- `paid:deepseek-deepseek-v4-pro` → **HTTP 402 Insufficient Balance**
- `paid:siliconflow-*`（GLM-5.3 / Qwen3.8-27B / Kimi-K2.7-Code / DeepSeek-V4-Flash）
  → 开跑前 200，**跑完也 402** 了

### 3.3 本地-only 连跑三次（免费）→ 成功

| 次数 | 状态 | 关键点 |
|---|---|---|
| #1 | 失败 | 空轮次假成功（Bug 5 未修），读 3 次后空回复 |
| #2 | 部分 | Bug 5 修后写出文件，但 dispatcher 把任务改成「不写文件」→ 写成了 `info_list.md`（Bug 6） |
| #3 | **成功** | Bug 6 修后：72.5s / 4 次调用 / 免费，`write_file` → **outline.md 被重写** |

`outline.md` md5 `7599a91c…` → `c5b27581…`（1794 字符）。
质量对比（vs `outline.before-eval.md` 基线）：

| 维度 | 旧基线 | 本次 |
|---|---|---|
| 故事线 | 未区分 | **明确三条**：三国穿越/轮回线（判为主线）、雨林演化寓言线、现代都市线 |
| 主角 | 含糊 | **刘备**（现代人穿越者，马克思主义追随者） |
| 「小师弟」 | 被误判为主角 | **「边缘搞笑配角，不参与核心剧情」** ✅ |
| 标题 | 《零级空壳的记忆》（源自配角能力，误植） | 已换掉 |

---

## 4. 未完成 / 需你决定

1. **付费模型下的评测复跑**：DeepSeek 与 SiliconFlow 双双欠费，需充值后重跑
   （`models.json` 里删掉欠费条目、或充值；然后 `.venv/Scripts/python.exe _eval_novel.py`）。
2. **`models.json` 已还原**为原始内容（含欠费的 DeepSeek 条目）。

## 5. 改动清单

```
agentforge/loop.py       +69/-?   Bug 1（末尾 system 保留 + 注释）、Bug 5（空轮次护栏）
agentforge/providers.py  +96/-?   Bug 1 兜底（system 上提合并）、Bug 4（UTF-8 解码）
agentforge/context.py    +89/-?   Bug 2（rehydrate_reasoning）、Bug 3（保 tool_call_id）
agentforge/dispatcher.py         Bug 6（DISPATCH_SYSTEM 禁止缩小范围）
```

临时脚本与日志（`_smoke_*.py`、`_eval_*_out.txt`、`_models_backup.json`）已全部删除。

---

## 6. 追加（同日）：`http://127.0.0.1:8787/` 打不开的根因与修复

三条原因，按因果链排列。

### 6.1 直接原因：服务没在跑

`8787` 是 Web UI 端口，靠 `start-web.bat` 或 `python run.py --web --port 8787` **手动拉起**。
本轮的评测脚本 `_eval_novel.py` 用的是 **8795**，而且结尾会 `httpd.shutdown()`。
诊断时 `netstat` 显示 8787 上没有任何 LISTENING → 浏览器只能得到连接被拒绝。

> 注：本报告写于当时。后来启动脚本收成了一个 `start.bat`（Windows）/ `scripts/start.sh`（Unix），
> 并加了常驻服务 + 开机自启，`start-web.bat` 这个名字已不存在——但下面关于 **`.bat` 必须是
> 纯 ASCII + CRLF** 的结论至今有效，是 `start.bat` 的硬约束。

### 6.2 为什么你启动不起来：`start-web.bat` 本身是坏的

文件是**纯 LF 行尾**（CR 数量 0，LF 数量 29），而 `.gitattributes` 明确要求
`*.bat text eol=crlf`（`git ls-files --eol` 显示 `i/lf w/lf attr/text eol=crlf`，
即工作区被本地改写过、丢了 CR）。

cmd.exe 解析批处理时**按 CRLF 计算字节偏移**。纯 LF 会让它每行错位、从行中间开始执行，
于是把半句话当命令跑：

```
'"' 不是内部或外部命令
'[AgentForge]' 不是内部或外部命令
install: unknown option -- upgrade
```

对照组（唯一变量 = 行尾，用同一段脚本跑）：

| 变体 | rc | 结果 |
|---|---|---|
| **LF + UTF-8（原文件）** | 1 | 报 `'exist'` / `'nv'` / `'o'` / `'1'` 不是内部命令，**脚本崩掉** |
| CRLF + UTF-8 | 0 | 能跑通，但仍报 `'RLF'` / `'成一堆'` 不是内部命令（中文导致的错位） |

### 6.3 中文编码：UTF-8 在 936 代码页下照样错位

本机控制台代码页 = **936（GBK）**（`chcp` 实测输出「活动代码页: 936」）。

UTF-8 中文的尾字节落在 `0x80–0xBF`，正好是 GBK 的合法前导字节区间；
GBK 解码器会把它**连同后面的 CR（0x0D）一起吃掉** → 行边界丢失 → 又是逐行错位。
把 `chcp 65001` 提到 `@echo off` 之后也**救不了**，实测仍报 `'RLF'`、`'成一堆'`。

→ 结论见 6.4：**这个 `.bat` 里不能出现任何非 ASCII 字符**。

### 6.4 修复内容与验证（含一次返工）

**第一版（已废弃）**：把文件改成 **CRLF + GBK**。cmd 侧确实好了，
但**编辑器里全是乱码和问号**——编辑器默认按 UTF-8 读，GBK 字节直接变成 `�`。
这是拿"命令行能跑"换了"人能看"，不划算，已返工。

**最终版：CRLF + 纯 ASCII（全英文注释与提示）**。理由：

| 存法 | cmd（936 控制台） | 编辑器（UTF-8） |
|---|---|---|
| LF + UTF-8 | ❌ 逐行错位 | ✅ |
| CRLF + UTF-8 | ❌ 中文吃掉行尾 CR，仍错位 | ✅ |
| CRLF + GBK | ✅ | ❌ 满屏乱码 |
| **CRLF + 纯 ASCII** | ✅ | ✅ |

ASCII 文件里没有多字节序列，从根上绕开了"解码器吃 CR"这个问题，两边都安全。
代价是提示信息只能用英文——对一个只显示 0.2 秒的启动器来说可以接受。

同时补了原文件的两个毛病：

1. 删掉无谓的 `chcp`（纯 ASCII 不需要）。
2. `del .run\web.log` + 启动命令加 **`-u`**：原注释写着「日志写 .run\web.log」，
   但 `start` 那一行**根本没有重定向**；补上重定向后 python 又是块缓冲，
   进程被杀时日志永远是空的，加 `-u` 才会立刻落盘。

验证证据（三项全过）：

```
启动器输出 : [AgentForge] Started with config config.lanrun.yaml - open http://127.0.0.1:8787 ...
             （零解析错误）
文件字节   : 纯 ASCII（非 ASCII 字节 = 0）·  CRLF=42 ·  裸 LF=0
端口       : TCP 127.0.0.1:8787  LISTENING
HTTP       : HTTP/1.0 200 OK  |  <title>AgentForge</title>  |  173,413 字节
日志       : .run\web.log 立即有内容（-u 生效），首行 "AgentForge Web UI: http://127.0.0.1:8787"
```

### 6.5 排查时的一个环境陷阱

本机 `http_proxy` / `https_proxy` 指向 `http://127.0.0.1:49422`，
带 `--noproxy '*'` 之外的请求打 `127.0.0.1:8787` 会被代理接走并返回 **HTTP 502**，
看起来像"服务坏了"。本地端口探测一律加 `curl --noproxy '*'`，或直接用 socket。

### 6.6 原来的一个坑（已在第 7 节处理）

修复前持久化的启用组合是 `qwen3.5:4b(local, 74)` + `DeepSeek V4-Pro(paid, 88)`，
付费模型分更高、会被优先选中，而 DeepSeek 账号已 402。
更隐蔽的是 `router.py:119`：

```python
all_avail = [e for e in store._catalog if getattr(e, "available", False)]
```

`all_avail` **包含没有勾选的模型**，所以只要 `allow_paid_upgrade=True`，
自动选型就可能挑到一个根本没勾选的付费模型。已一并关掉，见第 7 节。

---

## 7. 配置「本地最佳模型」（已应用）

走的是 Web UI 自己的官方路径，等价于在界面上点「自动配置本地模型」：

```
GET  /api/models                                      # 先触发 Ollama 发现，填充 catalog
POST /api/models {"action": "auto_config", "scope": "local"}
       -> store.apply_best_local()                    # 启用组合收窄为本地最优
       -> _write_routing(capability=True, allow_upgrade=True, allow_paid_upgrade=False)
```

### 7.1 候选与选择结果

| 模型 | kind | 总分 | 结果 |
|---|---|---|---|
| gemma4:12b | local | 78 | ✅ 选中 |
| qwen3.5:4b | local | 74 | ✅ 选中 |
| gpt-oss:20b | local | 75 | ✗ 贪心组合算下来边际收益为 0，不占名额 |
| DeepSeek V4-Pro | paid | 88 | ✗ 取消勾选 |
| GLM-5.3 | paid | 84 | ✗ 取消勾选 |
| Kimi K2.7-Code | paid | 82 | ✗ 取消勾选 |
| DeepSeek Flash / V4-Flash | paid | 80 | ✗ 取消勾选 |
| Qwen3.8-27B | paid | 78 | ✗ 取消勾选 |

本地组合：`coding 78 / reasoning 82 / writing 78 / chinese 78 / math 84 / speed 82 / cost 95 / context 86`，
**总分 82**（修复前含付费的组合是 89）。

`best_combo()` 是贪心的：`gemma` 拿推理/数学/编码/上下文，`smart-mini` 拿中文/速度/成本，
`gpt-oss:20b` 与两者能力重叠、加进去不涨分，所以没被选。

### 7.2 落盘结果

`models.json`：

```json
"enabled": ["local:gemma", "local:smart-mini"]
```

6 个付费条目**保留在 `paid` 数组里**，只是不再勾选——以后充值了直接勾上即可，不用重填 key。

`config.lanrun.yaml`：只改了一行

```diff
-  allow_paid_upgrade: true
+  allow_paid_upgrade: false
```

这一行是关键：关掉之后 `router.py` 里的候选池会先过滤掉 `kind == "paid"`
（`pool = [e for e in all_avail if e.kind != "paid"]`），**任何自动路径都不会再打到付费 API**。

### 7.3 验证

- 重新加载磁盘配置 → `enabled = [local:gemma, local:smart-mini]`，其中付费模型 **无**
- 重启 Web UI 的启动横幅由 `总分 89` 变为
  `当前组合: 2 个模型 · 总分 82 · 成本 95 · 上下文 86 · 数学 84 · 推理 82`，新组合已生效
- 路由模式 `mode` 由 `best` 变为 `local`
- 备份留在 `.run/models.json.bak` 与 `.run/config.lanrun.yaml.bak`，要回退直接覆盖回去
