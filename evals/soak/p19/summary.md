# 浸泡跑汇总（3/3 轮）

重算于 2026-10-26 00:31:29；每轮明细见 `issues.md`，机器可读见 `rounds.jsonl`。

## 1. 按用例

| 用例 | 轮次 | 通过 | 失败 | 通过率 | 均耗时 | 失败层 |
|---|---|---|---|---|---|---|
| o2-disk-plan | 3 | 3 | 0 | 100% | 52s | — |
| o3-restart-script | 3 | 3 | 0 | 100% | 114s | — |
| w5-long-notes-outline | 3 | 2 | 1 | 67% | 211s | check×1 |
| c8-fix-offbyone | 3 | 3 | 0 | 100% | 42s | — |
| a6-json-sum | 3 | 1 | 2 | 33% | 35s | numeric×2 |

## 2. 不稳定用例（同一用例既有过也有失）

`w5-long-notes-outline`、`a6-json-sum`

## 3. 按问题指纹（出现次数降序）

| 次数 | 规则 | 用例 | 级别 | 说明 | 首见 |
|---|---|---|---|---|---|
| 3 | `answer_only` | o2-disk-plan | mid | ops 类任务 1 步、零工具（verdict=pass） | p1901 |
| 2 | `eval_fail` | a6-json-sum | high | numeric: 数值不符：期望 [{'want': 87.5, 'tol': 0.01}]，提取到 [1902.0, 6.0] | p1902 |
| 1 | `tool_error` | c8-fix-offbyone | mid | 0 次工具调用 | p1901 |
| 1 | `stamp:no_echo` | w5-long-notes-outline | mid | 答复含「未能核对到」（verdict=pass） | p1902 |
| 1 | `tool_error` | a6-json-sum | mid | 0 次工具调用 | p1902 |
| 1 | `eval_fail` | w5-long-notes-outline | high | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' | p1903 |

## 4. 总账

- 轮次 3/3，用例判定 15 次，失败 3 次（20.0%）
- 问题条目 9 条，去重后 6 个指纹
- 总墙钟 22.7 分钟


## 5. 每轮耗时

单位秒。台账的 `墙钟` 是按各用例 `duration` 累加算出来的（bench 不单独记整轮用时），不含 python 启动与轮间清场；31 轮实测真实钟表比它长约 5 分钟，所以可以直接当墙钟看。

| 轮次 | 墙钟 | o2-disk-plan | o3-restart-script | w5-long-notes-outline | c8-fix-offbyone | a6-json-sum | 判定 |
|---|---|---|---|---|---|---|---|
| p1901 | 533 | 62 | 154 | 249 | 34 | 34 | 5/5 |
| p1902 | 500 | 48 | 135 | 227 | 54 | 36 | 4/5 |
| p1903 | 330 | 46 | 54 | 157 | 39 | 34 | 3/5 |

- 合计 22.7 分钟；均 7.6、中位 8.3 分钟；最短 5.5（p1903）、最长 8.9（p1901）
- 去掉最长一轮后均值 6.9 分钟 —— 与中位差得远就说明是单轮异常而不是整体变慢
- 每轮最慢的用例：`w5-long-notes-outline` 占 3/3 轮

| 用例 | 均 | 中位 | 最短 | 最长 | 占总墙钟 |
|---|---|---|---|---|---|
| w5-long-notes-outline | 211s | 227s | 157s | 249s | 46% |
| o3-restart-script | 114s | 135s | 54s | 154s | 25% |
| o2-disk-plan | 52s | 48s | 46s | 62s | 11% |
| c8-fix-offbyone | 42s | 39s | 34s | 54s | 9% |
| a6-json-sum | 35s | 34s | 34s | 36s | 8% |

- 最贵的是 `w5-long-notes-outline`（占 46%）：要缩短一轮，先动它；它的中位 227s 基本就是每轮的固定成本