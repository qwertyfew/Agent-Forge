# 重构计划：按功能拆分层级（2026-10-27 起）

与上一个项目（`D:\AI_develop\show_agent`：`src/agent`、`src/api`、`src/memory`、
`src/rag`、`src/utils`）同一套做法：**按功能分层，每层一个文件夹**。

## 原则

1. **一步一提交，一步一评测**：每一步只搬一个职责组，搬完立刻跑同一组评测用例，
   与基线对比；恶化就回滚到上一个提交，不在坏的基础上继续。
2. **先搬文件，再切大文件**：模块之间的边界先理清（本阶段），之后才是把
   `loop.py`(1971 行)、`orchestrator.py`(1228 行)、`verify.py`(1434 行) 这类
   大文件按职责切开——顺序反了会边搬边改，diff 无法审查。
3. **不改行为**：本阶段只做移动与改导入，禁止顺手改逻辑。任何行为变化必须
   单独一步、单独说明。
4. **护栏优先**：每步落一组断言（已有的 `tools/test_*.py` + 新增
   `tools/test_runstate.py`），保证「搬完还能跑」。

## 目标结构

```
agentforge/
  core/      配置与模型：config.py  modelstore.py  scores.py  router.py  pricing.py
  llm/       模型接入：llm.py  providers.py  streamprobe.py
  runtime/   运行期状态：runstate.py  runcontrol.py  ollamaguard.py
  agent/     执行主体：loop.py  orchestrator.py  planner.py  toolselect.py
             clarify.py  warmup.py  steering.py  interact.py  resume.py
  gates/     核查闸门：verify.py  grounding.py  sources.py  skeleton.py
             evidence.py  honesty_guard.py  autocheck.py  claims?（如拆出）
  ledger/    台账与案卷：taskstore.py  journal.py  record.py  artifact.py
             layout.py  tasksession.py  session.py  prefs.py  writepolicy.py
  memory/    记忆与沉淀：memory.py  autonote.py  autoskill.py  skills.py  gaps.py
  serve/     Web 服务（由 webui.py 拆出）
  evals/     评测台：evalsdesk.py  evals.py
  tools/     工具注册表与具体工具（保持）
  cli.py     命令行入口（保持）
```

## 步骤（每步 = 一次提交 + 一轮评测）

| # | 内容 | 风险 | 状态 |
|---|---|---|---|
| 0 | 建立评测基线（代表集 6 条） | — | 进行中 |
| 1 | `runtime/`（runstate + runcontrol + ollamaguard） | 低 | 待做 |
| 2 | `llm/`（llm + providers + streamprobe） | 中（导入面广） | 待做 |
| 3 | `core/`（config + modelstore + scores + router + pricing） | 中 | 待做 |
| 4 | `ledger/`（taskstore/journal/record/artifact/layout/tasksession/session/prefs/writepolicy） | 中 | 待做 |
| 5 | `gates/`（verify/grounding/sources/skeleton/evidence/honesty_guard/autocheck） | 高（loop 与 webui 大量引用） | 待做 |
| 6 | `memory/`（memory/autonote/autoskill/skills/gaps） | 中 | 待做 |
| 7 | `agent/`（loop/orchestrator/planner/toolselect/clarify/warmup/steering/interact/resume） | 高 | 待做 |
| 8 | `serve/`：webui.py 拆成 handlers | 高 | 待做 |
| 9 | 大文件切分（loop / orchestrator / verify） | 高 | 待做 |
| 10 | 前端 `web/index.html` 拆分（JS 抽成模块） | 高 | 待做 |

## 评测口径

- 代表集（每步都跑）：
  `c2-fizzbuzz, c5-fib, f1-make-dir, f2-count-lines, w1-summary, h5-fake-key`
  命令：`python tools/bench_tasks.py --label <step-label> --cases <上面这串>`
- 里程碑（每 3~4 步或大改动后）跑全量 40 条。
- 结果落在 `evals/results/<label>.json`，与基线 `refactor-base` 对比：
  **通过数不得下降**；下降即视为回归，回到上一步提交。

## 步骤日志

评测结果文件本身不入库（`evals/results/` 已 gitignore），所以每步的读数抄在这里，
便于跨步对比与回溯。

### 步骤 0 · 基线（2026-10-27 04:30）

命令：`python tools/bench_tasks.py --label refactor-base --cases <代表集>`

| 用例 | 层 | 耗时 | 结果 |
|---|---|---|---|
| c2-fizzbuzz | check | 26.1s | PASS |
| c5-fib | check | 24.7s | PASS |
| f1-make-dir | check | 7.7s | PASS |
| f2-count-lines | check | 4.8s | PASS |
| w1-summary | fuzzy | 15.5s | PASS |
| h5-fake-key | fuzzy | 4.6s | PASS |

**6/6 通过，总耗时 86.3s** → 这就是后面每一步的对照基准。

### 步骤 1 · 抽 `runtime/`（2026-10-27 04:33）

搬动：`runstate.py` / `runcontrol.py` / `ollamaguard.py` → `agentforge/runtime/`，
新增包 `__init__.py` 说明这一层的判据（「随任务结束而消失的是 runtime，要落盘
事后回看的是 ledger」）。改了 11 处导入
（interact / loop / orchestrator / steering / tools.memory / tools.notes /
service / webui / 三个测试），注释里的路径同步更新。`config.py` 里的路径注释也改了。

评测：`--label step01-runtime`，**6/6 通过，79.5s**（与基线一致，用例逐条 PASS）。

结论：无回归，保留。

## 回滚办法

每步一个提交，回滚即 `git revert <该步提交>` 或 `git checkout <上一步> -- <路径>`。
评测结果与判断同步记入本文档末尾的「步骤日志」。
