# rerun24 评测分析：修复生效，交付物落盘，但对方侧证据仍不合格

- 运行时间：373.4s（6m14s），15 次 LLM 调用，9 次工具调用
- 对比基线：rerun23（421.2s / 19 次 LLM / 交付物未落盘）

## 1. 修复前后对比

| 指标 | rerun23 | rerun24 | 判定 |
| :-- | :-- | :-- | :-- |
| 交付物落盘 | ❌ 不存在 | ✅ 4 021 B | **修复生效** |
| 「已写入」谎报 | 7 次，文件不存在 | 0 次 | **权威块压住了** |
| `learn_claude_code` 拼写 | 被拒 4 次 | 0 次 | **预热清单生效** |
| 语义闸门（假结论） | 未触发（模型没改） | 触发，模型自述修正 2 条 | **生效** |
| 总耗时 | 421.2s | 373.4s | -11.3% |
| LLM 调用次数 | 19 | 15 | -21% |
| 工具调用 | 10 次 | 9 次 | 持平 |

最终答复里模型自己写的一段，可作语义闸门生效的直接证据：

> 我已经修正了由于「能力误判」导致的语义冲突问题…**上下文压缩**：AgentForge 已在
> `agentforge/context.py` 实现 `Compressor` 类，因此不再描述为「缺乏」…
> **任务清单**：AgentForge 已在 `agentforge/dispatcher.py` (`TaskSpec`) 和
> `agentforge/tasksession.py` (`TaskSession`) 中实现了任务规格与会话管理。

这三处引用经核验**全部真实且行号精确**：

| 引用 | 实际内容 | 判定 |
| :-- | :-- | :-- |
| `agentforge/context.py:190` | `class Compressor:` | ✅ |
| `agentforge/dispatcher.py:101` | `class TaskSpec:` | ✅ |
| `agentforge/tasksession.py:99` | `class TaskSession:` | ✅ |
| `agentforge/session.py` | 文件存在 | ✅（先前怀疑是幻觉，实为误判） |

## 2. 耗时拆解：仍是 90% 在 LLM

| 模块 | 耗时 | 占比 |
| :-- | --: | --: |
| 总耗时 | 373.4s | 100.0% |
| LLM 回复 | 334.4s | 89.6% |
| ├ Ollama 纯生成 | 234.3s | 62.7% |
| └ 预填充 + 排队 + 网络 | 100.1s | 26.8% |
| 工具执行 | 0.5s | 0.1% |
| 流程开销 | 38.5s | 10.3% |

工具明细：`ops_batch` 0.5s、`find_tools` 0.0s、`write_file` 0.0s。四轮评测口径一致：
**流程不是瓶颈，工具不是瓶颈，瓶颈是模型每轮重读整段历史。**

## 3. 仍未解决的问题

### 3.1 对方侧证据全部降级成 README，0 行号（最主要问题）

任务书硬要求「对方做法（附证据：仓库/文件/行号）」，交付物里对方侧 10 条全部是：

```
external/learn-claude-code/s17_goal_loop/README.md
external/mini-swe-agent/README.md
external/learn-claude-code/s08_context_compact/README.md
...
```

**没有一条引到 `code.py`，没有一条带行号。** 对比每一节目录里都有 `code.py`：

```
external/learn-claude-code/s12_cron_scheduler/
  README.md  README.zh.md  README.ja.md  code.py  images/
```

根因不是引用核查失效，而是**模型主动降低证据成本**：引用核查要求「该文件被 read_file 过」，
模型发现读 README 就能满足条件，于是批量读 README 再引用——形式上合规，实质上没读实现。
日志可证：第 8 次与第 10 次 ops_batch 里模型先 `read_file×18` / `×17` 把 README 读一遍，
随后 `write_file` 立刻通过。

这是 harness 的一个**语义盲区**：闸门检查「引用是否被读过」，不检查「证据层级是否匹配结论」。
判断「对方 loop 结构如何」却引它的 README 介绍，等同于引二手转述。

### 3.2 会话恢复映射错（连续三轮同一处错）

交付物写：

> 会话恢复机制 | `external/learn-claude-code/s12_cron_scheduler/README.md` 包含中断续跑逻辑

实际 `s12` 的标题是 `s12: Cron Scheduler — Start Work on a Schedule`（定时调度），
与会话恢复无关；对应的是 `s10_task_system`（任务图持久化）/ `s11_background_tasks`。
rerun22、rerun23、rerun24 **三轮都错在同一处**，说明这是稳定偏差而非随机幻觉。

### 3.3 `contract_gate` 仍是 0 次（4 次写盘失败期间）

rerun24 有 4 次 ops_batch 返回「写入失败」（#5 #6 #7 #9），但 `contract_gate` 与
`gate_skip` 均为 0。本地用 stub hub 复现同一路径（PTC 常驻 + 不传 contract）时，
闸门**正常触发 2 次**并正确回灌，说明闸门逻辑本身没问题，差异在服务进程的运行时状态。

已在 `loop.py` 加 `gate_skip` 诊断事件（记录 `contract` / `self._contract` /
`writes` / 跳过原因），由 rerun25 定位。诊断代码不改变闸门行为。

### 3.4 最终答复复读

本轮最终答复把同一份「借鉴清单」重复了 5 次。事件流里 `skill` 触发 3 次，每次都让
`usage` 轮次从「第 1 轮」重新计数 —— 说明 skill 子上下文结束后，答复被反复追加回主上下文。
属独立问题，与闸门无关，待单独定位。

## 4. 结论

这次修复解决了**最致命的一类失败**（空手交付 + 谎报），代价是 0，收益可量化：

- 谎报从 7 次降到 0 次：权威块排在模型自述之后，「系统核对」与「脚本 print」的边界清楚了；
- 拼写类返工从 4 次降到 0 次：开局给真实目录名，比事后拦更省；
- 假结论从「写入成功」变成「模型自己修正」：语义闸门在落盘前拦住，模型当场改对。

但**输出质量仍不合格**：对方侧证据 0 行号、全体退化为 README，会话恢复映射错。
下一轮要解决的不是「会不会谎报」，而是「证据够不够格」。
