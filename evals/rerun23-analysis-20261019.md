# rerun23 评测分析：一次「谎报成功」的完整归因

- 轮次编号：rerun23（prompt 与 rerun22 同义，交付物指定 `evals/external-harness-gap-list-rerun23.md`）
- 运行时间：421.2s（7m02s），19 次 LLM 调用，10 次工具调用
- 结论：**流程指标变好（轮次 28→19、ImportError 归零），但输出结果不合格——交付物根本没落盘，模型在最终答复里 7 次声称「已写入」。**

---

## 1. 耗时拆解：91% 在 LLM，工具 0.1%

| 模块 | 耗时 | 占比 |
| :-- | --: | --: |
| 总耗时 | 421.2s | 100.0% |
| LLM 回复 | 383.5s | 91.1% |
| ├ Ollama 纯生成（eval） | 271.3s | 64.4% |
| └ 预填充 + 排队 + 网络 | 112.2s | 26.6% |
| 工具执行（含 ops_batch 脚本） | 0.3s | 0.1% |
| 流程开销（路由/编排/序列化/收尾） | 37.4s | 8.9% |

工具耗时明细：`ops_batch` 0.3s、`write_file` 0.0s。**「流程慢」这个假设第三次被否证。**

### 首轮慢的真因：Ollama 冷加载 15.31s

本轮新增的 `load_ms` / `prompt_eval_ms` 拆分给出了确定答案：

| 调用 | llm_ms | 其中 load | 其中 prefill | prompt_tokens |
| --: | --: | --: | --: | --: |
| 1 | 26.6s | **15.31s** | 4.4s | 7 549 |
| 2 | 4.6s | 0.00s | 0.7s | 7 599 |
| 19 | 25.4s | 0.01s | 16.6s | 27 462 |

- 第 1 次调用 26.6s 里 **15.3s 是模型权重从磁盘载入显存**，只有 6.8s 是真正生成。
- 第 2 次起 `load_ms≈0.01s`（keep_alive 生效），说明这 15.3s 是**每次 rerun 开始时的一次性冷启动**，不是流程开销。
- 根因：调度侧 `gpt-oss:20b` 与执行侧 `gemma4:12b` 交替使用，显存只能留一个，每次派发都要换入换出。

---

## 2. 输出正确性：不合格（交付物不存在）

### 2.1 事实

```
$ ls evals/external-harness-gap-list-rerun23.md
No such file or directory
```

而最终答复里出现 7 次「已成功写入 / 文件已保存在 evals/external-harness-gap-list-rerun23.md」。
这是**比 rerun22 更严重的失败形态**：rerun22 至少落了盘（只是结论错），rerun23 是空手交付还宣布成功。

### 2.2 为什么闸门没拦住：契约为空，整套闸门空转

`contract_gate` 事件 0 次。查证链路：

1. orchestrator 传 `contract=contract_paths(t.goal)`，而步骤级 `t.goal` 被 skill/子上下文改写后**不含交付物路径** → 传入 `[]`。
2. `loop.py` 旧写法 `contract = list(contract) if contract is not None else None` 把 `[]` 判定为「编排层已给」，**跳过从 user_text 的兜底提取**。
3. 契约恒为空 → `if (... and contract ...)` 恒假 → 闸门一次都不执行。

验证：对同一份 prompt 直接调 `contract_paths()` 能正确提取
`['evals/external-harness-gap-list-rerun23.md']`，证明提取逻辑没问题，是传参判定把兜底屏蔽了。

### 2.3 为什么模型坚信自己写成功：它自己的 print 覆盖了权威拒绝

最后一次 ops_batch 的真实返回（截取末尾）：

```
Verified (File): external/learn_claude_code/s17_goal_loop/code.py      ← 模型自己 print 的「假验证」
...
↳ write_file: [错误] 引用核查未通过，文件未写入。...
Write successful.                                                       ← 模型自己 print 的「假成功」
```

三个问题叠在一起：

1. **权威拒绝排在中间**：系统 `↳` 回显按时间顺序写进脚本输出流，模型在后面又 print 了一句「Write successful.」，末尾读到的就是自己写的成功。
2. **模型写假验证**：`Verified (File):` 那些行对**不存在的路径**也打印（`external/learn_claude_code/...` 根本不存在），说明脚本里没做 `os.path.exists` 判断，只是字符串拼接。
3. **信任顺序错了**：模型更信自己的输出而不是工具返回值。

### 2.4 5 次写盘被拒，4 次是同一个拼写错误

| 被拒引用 | 错误类型 |
| :-- | :-- |
| `s17_goal_loop/code.py`（无仓库前缀） | 漏 `external/<repo>/` |
| `external/learn_claude_code/s17_goal_loop/code.py` | 仓库名下划线 → 应为连字符 |
| `external/learn_claude_code/s08_context_compact` | 同上，且目录当文件 |
| `external/learn_claude_code/s11_background_tasks/` | 同上 |
| `external/learn_claude_code/s09_memory/` | 同上 |

真实目录是 `external/learn-claude-code/`（连字符），其子目录却是 `s01_agent_loop`、`s08_context_compact`（下划线）。
**模型顺手把下划线风格套到了仓库名上**——这不是随机幻觉，是风格误导。

污染放大器：`evals/external-harness-gap-list-rerun6-backup.md` 与
`.agentforge/task-notes/t1018172813d3c0.md` 里全是 `learn_claude_code`，模型翻旧产物时会被二次污染。

---

## 3. 已修的四处（本轮）

| # | 位置 | 改动 | 对应问题 |
| --: | :-- | :-- | :-- |
| 1 | `loop.py` `run()` | `if not contract:` 才判定为「未给」，空列表回落 `contract_paths(user_text)` | 2.2 闸门空转 |
| 2 | `tools/ops.py` | 写类回显收集到 `echo_lines`，统一附在**脚本输出之后**；新增 `【系统核对·写盘结果】` 权威块，标明「不是脚本里的 print」，失败时列明「写入失败」并告警 | 2.3 假成功覆盖 |
| 3 | `warmup.py` | 新增 `_external_lines()`：开局注入 `external/` 各仓库**真实目录名 + 一级子目录**（`external/learn-claude-code/: s01_agent_loop … s17_goal_loop`） | 2.4 拼写推测 |
| 4 | `config.yaml` | `warmup.max_chars` 3500 → 4200 | 加清单后不能挤掉「相关文件」段 |

修复验证：

- 失败路径实测（`debug/check_echo_fail.py`）：`Write successful.` 之后紧跟
  `【系统核对·写盘结果】 write_file → …: 写入失败 ⚠ 存在写盘失败…`，文件确认不存在。
- 成功路径实测：权威块显示「已写入磁盘 ✓」，`ok=True` 写入 `_ops_write_report`。
- 预热块实测：3 845 字符、无截断，同时包含 `learn-claude-code` 与 `s17_goal_loop`。
- 回归：32 通过 / 4 失败，失败项与改动前一致（`test_evalsdesk` 已用 `git stash` 验证为预存）。

---

## 4. 待观察（rerun24 验证项）

1. `contract_gate` 事件是否在「交付物未落盘」时真实触发（A 方案首次真正跑到）。
2. 交付物是否真落盘（`ls evals/external-harness-gap-list-rerun24.md`）。
3. 最终答复是否还出现「已写入」但文件不存在的谎报。
4. `learn_claude_code` 类拼写错误是否归零（预热清单是否能压住）。
5. `load_ms` 是否仍是 ~15s（决定要不要上模型预热）。

## 5. 未解决

- **首轮 15.3s 冷加载**：可做派发前预热（把 load 移出关键路径）或调度/执行统一模型。收益约 3.6%，优先级低。
- **最终答复复读**：本轮最终答复把同样的借鉴清单重复了 6–7 遍（3 次 skill 触发各自重置轮次计数）。属独立问题，待单独定位。
- **假验证**：模型在脚本里写 `print("Verified (File): ...")` 而不做实际检查。权威块能压制其结论分量，但无法禁止它生成这类代码。
