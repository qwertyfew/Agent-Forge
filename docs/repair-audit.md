# 修复清单：哪些是硬约束，哪些只是叮嘱

这份清单回答一个问题：**已经修过的东西，现在是靠代码挡住的，还是靠"写下来提醒"？**
分清楚这个，才能判断"修了还出错"的风险在哪。

判断一件事该用什么方式修，用三问（第 19 步）：

| 三问 | 都是「是」→ | 否则 → |
|---|---|---|
| 1. 这件事的**正确答案**依赖模型判断吗？ | 只能留在模型侧 | **由程序定** |
| 2. 程序能不能**在出错的那一刻**发现它？ | 能 → 加程序侧核查 | 只能靠事后复盘 |
| 3. 三个月后还会不会重犯？ | 会 → 写成常驻断言 | 记在记忆里就够 |

一句话原则：**不需要模型判断的事，就不该让它经过模型。**

## 强度分档

| 档 | 含义 | 破了会怎样 |
|---|---|---|
| **A 程序强制** | 代码里跑的检查 / 收口，模型碰不到也绕不过 | 不会发生（除非改代码） |
| **B 断言锁定** | `tools/test_*` 常驻回归把结论钉住 | 改回去立刻红，且说得出红在哪 |
| **C 提示词 / 技能** | 系统提示、SKILL.md 里的规矩 | **模型不听就什么都不会发生** |
| **D 作业纪律** | 记在 `.agentforge/memory.md`，靠人和 agent 遵守 | 需要事后复盘才能发现 |

A 与 B 常常成对出现：**A 是"挡"，B 是"证明挡得住"**。只有 A 没有 B，改动一次就可能悄悄退化；
只有 B 没有 A，测试锁的是别的东西。理想形态是「A + B」，本清单里标 `A+B` 的都是这个状态。

## 一、能力与架构级（第 1–19 步）

| # | 问题（当初的现场） | 现在靠什么防住 | 档 |
|---|---|---|---|
| 2 | 上下文撑爆：每轮把全部工具定义都发给模型 | 工具按需下发（`toolselect.py`）+ 技能只注入摘要 + 接力简报 | A |
| 4 | 发起任务的连接一断，任务就死、进度就没了 | SSE 事件总线 + 任务台账，刷新后接管续播（`taskstore.py` / `webui.py`） | A |
| 8 | 联网输出分不清「搜索摘要」和「实测结果」 | 工具输出头标注来源性质（`[搜索摘要·转述·未核实]` / `[实测·网页]` / `[实测·API]`） | A（弱：只是标注，靠模型读懂） |
| 9 | 同一个只读调用反复打，一轮 28 分钟 | 只读调用回放共享缓存 + 轮次预算 + 保鲜参数 | A+B（`test_readcache.py`） |
| 11 | 记忆自我污染：模型把自己的猜测写回记忆、下轮当事实 | 记忆防脏闸门（`autonote.py`）+ 输出边界程序化（结论先行 / 中文闸门） | A |
| 12 | 改完代码不验证；模型**谎报**"验证通过了" | 收尾验证闸门：命令由系统亲自跑，模型只有解读权 | A+B（`test_verify_gate.py` 73 项） |
| 13 | 「读不到」被当成「没有」；Ollama 没起就说模型不可用 | 错误分类（读不到 ≠ 空）+ 上游守卫（`ollamaguard.py`）+ 常驻服务 | A |
| 14 | 6 个启动脚本，用户不知道点哪个 | 收成 `start.bat` / `stop.bat`，职责唯一；`.bat` 全路径调用外部命令 | A（文件存在即成立） |
| 15 | 把搜索摘要里的数字当事实写进结论（star 说 1w+，实际 17 万） | 系统侧「转述」核查（`sources.py`，回复末尾由**程序**追加声明） | A+B（`test_sources_gate.py`） |
| 16 | 点了工作区路径，窗口在后台不出来（"点一次没反应"） | `reveal.py`：`AttachThreadInput` + `SetForegroundWindow` 强制抬前台 | A+B（`test_reveal.py`） |
| 17 | 提问 / 头像 / 计时三个口径，脚本发起的任务看不见"我问了什么" | 台账存 `prompt` 原话 + 唯一入口 `enterTaskView()` | A+B（`test_taskstore.py` / `test_task_view.mjs`） |
| 18 | 要读现场的任务被判成「写作」→ 模型去"写"而不是"看"，编出目录清单 | 新增 `inspect` 类 + 优先级；本机事实闸门 + 修正轮 | A+B（`test_planner.py` / `test_local_facts.py`） |
| 18 | **技能加载了 ≠ 规矩生效**（`self-verification` 加载了照样编） | 程序侧核查兜底（`sources.py` + `orchestrator._verify_gate`） | A+B |
| 19 | 头像行 / 计时迟到或缺席（lazy 挂在"第一个 AI 元素"上） | `beginReply()`：提问落盘那一刻无条件建；`takeAIHead` 降为幂等兜底 | A+B（`test_reply_flow.mjs` 用例 A） |
| 19 | 工具条在回答中途冒出来；`error` 的回复永远停在「执行中」 | 收尾唯一出口 `settleReply(status)` + `unsettleReply()` 撤回 + 流读完兜底 | A+B（`test_reply_flow.mjs` 用例 B/C/D/E） |
| 19 | `fetch_url` 失败后只能猜工具名（猜了 `browser`，白烧两轮） | 失败输出由程序写下一步（含 GitHub/npm/PyPI 的接口换算） | A+B（`test_web_tools.py`） |
| 19 | 闸门只看数字，定性时效性转述（「10 月初走红」）静默放行 | 判据补时效性一路 + 要求与搜索词有实词交集 | A+B（`test_sources_gate.py`） |
| 19 | 泛查询白烧一轮（第一枪返回的全是首页与教程站） | `web_search` 判定"没打到"就自动补一枪收紧查询，结果排最前 | A+B（`test_web_tools.py`） |

## 二、具体缺陷（bug 级）

| 问题 | 修复 | 档 |
|---|---|---|
| `stop.bat` 里裸 `find` 被 Git for Windows 的 GNU find 抢名 | 写全路径 `%SystemRoot%\System32\find.exe` | A（改完即成立）+ D（"外部命令写全路径"这条约定） |
| 工具 shell 里起的服务被进程树带走（起得来、4 秒后消失） | 改用 WMI `Win32_Process.Create` 脱离进程树 | D（作业纪律，只能在日志里事后看出） |
| 测试断言名不副实（T15 说"轮询"其实没轮询） | 改成真轮询 | A（测试本身） |
| SSE 解析写错（类型在 `event:` 行不在 `data:` 行） | 修脚本；采集与呈现解耦（`summarize_events.py`） | A |
| 误判"`fetch_json` 没下发给他" | 离线复现 `ToolSelector.select` 证否：技能正文一注入，三个联网工具一起进 | D（教训：先用证据证否，再改） |
| 「正在回看历史」被当成"别打扰"（后果：那条任务用户永远看不见） | 判据改成 `busy`（用户是不是正在忙），不是"页面够不够干净" | A+B |
| 突变验证时先删了备份，把突变版本恢复进工作区 | 纪律：`备份 → 改坏 → 跑测试 → 恢复 → 重跑全量 → 才删备份` | D |
| `test_planner` 断言写歪（"看一下某源码文件"期望 inspect） | 放宽为 `in ("inspect","coding")` 但不许是 writing | A+B |
| 收尾时清掉 `cur`/`lastSeg` → 续写凭空多开气泡、工具条再也撤不掉 | 收尾不清 `cur`；撤销按 DOM 找（`querySelectorAll('.msg-foot')`） | A+B（`test_reply_flow.mjs` 用例 C） |
| 工具条防重复读内存标志 → "撤回→重挂"留下脏值，把重挂挡掉 | 改问 DOM（`:scope > .msg-foot`） | A+B |

## 三、仍然只有「劝」和「纪律」的（有意留在那儿）

| 事情 | 为什么不能架构化 |
|---|---|
| 结论纪律（`loop.py` 系统提示）：外部事实必须来自一手来源 | 这是模型**怎么做判断**的引导，判据本身依赖语义理解。能做的已做：闸门（A）兜底 + 工具输出标注。**保留 C 是因为它能让模型少犯，A 是因为它能兜住模型犯了** |
| 技能（`first-party-source` / `self-verification` / `long-material`） | 同上：技能是"方法论"，交给模型执行；技能文本里已写明"这条有系统侧核查兜底" |
| `.bat` 里外部命令写全路径 | 写作时的约定，没有运行时可检查的点 |
| 别在自己所在的工具 shell 里启停常驻服务 | 人的作业纪律，不是程序的输入输出 |
| 突变验证的备份顺序 | 能强制的是"改完必须重跑全量回归"这个动作；做没做只有人知道 |

**判断标准**：一件事如果"模型不做也不会出错"（因为程序挡着），那 C 层写不写都安全；
如果"模型不做就出错"，那它就不该待在 C 层——要么上移到 A，要么承认这里有个洞并写清楚。

## 四、本轮（第 19 步）从「劝」搬到「定」的四处

| 原先（劝） | 现在（定） | 证据 |
|---|---|---|
| 头像行 lazy：谁先出现谁负责插（8 个调用点各自记得） | `addUser()` 里无条件建，与任何后端事件无关 | `test_reply_flow.mjs` A1–A6 |
| 工具条挂载散在 `done`/`stopped`/`history`/回看四条路 | `settleReply(status)` 唯一出口，按结局决定 | 同 B4–B8 |
| 「收尾后又动起来」没人管 | `unsettleReply()` 按 DOM 撤回工具条 + 计时回到「执行中」 | 同 C2–C5 |
| `fetch_url` 失败只提示 `browser_snapshot` | 程序按失败类型给下一步，认得 URL 就换算成官方接口 | `test_web_tools.py` |
| 闸门只认数字 / 年份 | 补时效性一路（有实词交集才出手） | `test_sources_gate.py` |
| 泛查询照搜、白烧一轮 | 程序判定"没打到"就自动补一枪收紧查询 | `test_web_tools.py` |

## 五、怎么自己复核这份清单

```bash
# 全部常驻回归（Python）；前端三个需要 jsdom
python tools/test_readcache.py && python tools/test_verify_gate.py \
 && python tools/test_sources_gate.py && python tools/test_local_facts.py \
 && python tools/test_planner.py && python tools/test_reveal.py \
 && python tools/test_taskstore.py && python tools/test_web_tools.py
node tools/test_task_view.mjs && node tools/test_tool_panel.mjs && node tools/test_reply_flow.mjs
```

复核"某条到底是 A 还是 C"，就问一句：**把这条从代码里删掉，测试会不会红？**
红 → 它是 A+B；不红 → 它只是 C/D，得说明为什么允许它待在那一层。

明细见 `README.md`《架构演进》各步；本清单只做分类与交叉索引。
