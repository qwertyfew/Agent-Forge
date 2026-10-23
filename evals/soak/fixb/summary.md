# 浸泡跑汇总（10/10 轮）

重算于 2026-10-22 03:03:54；每轮明细见 `issues.md`，机器可读见 `rounds.jsonl`。

## 1. 按用例

| 用例 | 轮次 | 通过 | 失败 | 通过率 | 均耗时 | 失败层 |
|---|---|---|---|---|---|---|
| f5-git-search-rea | 10 | 10 | 0 | 100% | 79s | — |
| c6-refactor-discount | 10 | 10 | 0 | 100% | 95s | — |
| w5-long-notes-outline | 10 | 9 | 1 | 90% | 145s | check×1 |
| q1-idempotent-no-tools | 10 | 8 | 2 | 80% | 158s | crash×2 |
| h4-dead-repo | 10 | 10 | 0 | 100% | 71s | — |

## 2. 不稳定用例（同一用例既有过也有失）

`w5-long-notes-outline`、`q1-idempotent-no-tools`

## 3. 按问题指纹（出现次数降序）

| 次数 | 规则 | 用例 | 级别 | 说明 | 首见 |
|---|---|---|---|---|---|
| 2 | `tool_error` | c6-refactor-discount | mid | 0 次工具调用 | fixb02 |
| 2 | `stamp:no_echo` | w5-long-notes-outline | mid | 答复含「未能核对到」（verdict=pass） | fixb02 |
| 2 | `eval_fail` | q1-idempotent-no-tools | high | crash: 超时（>480s） | fixb02 |
| 2 | `slow` | q1-idempotent-no-tools | low | 480s / 1 步 / 工具 无 | fixb02 |
| 1 | `eval_fail` | w5-long-notes-outline | high | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' | fixb01 |
| 1 | `write_retry` | w5-long-notes-outline | low | 中途写盘失败后自愈（第 4 轮失败 1 次，第 5 轮写入成功） | fixb02 |
| 1 | `gate` | f5-git-search-rea | low | 闸门 1 轮 | fixb04 |
| 1 | `write_retry` | c6-refactor-discount | low | 中途写盘失败后自愈（第 2 轮失败 2 次，第 2 轮写入成功） | fixb04 |
| 1 | `gate` | h4-dead-repo | low | 闸门 1 轮 | fixb08 |

## 4. 总账

- 轮次 10/10，用例判定 50 次，失败 3 次（6.0%）
- 问题条目 13 条，去重后 9 个指纹
- 总墙钟 91.3 分钟


## 5. 每轮耗时

单位秒。台账的 `墙钟` 是按各用例 `duration` 累加算出来的（bench 不单独记整轮用时），不含 python 启动与轮间清场；31 轮实测真实钟表比它长约 5 分钟，所以可以直接当墙钟看。

| 轮次 | 墙钟 | f5-git-search-rea | c6-refactor-discount | w5-long-notes-outline | q1-idempotent-no-tools | h4-dead-repo | 判定 |
|---|---|---|---|---|---|---|---|
| fixb01 | 499 | 91 | 64 | 184 | 79 | 81 | 4/5 |
| fixb02 | 926 | 76 | 84 | 223 | 480 | 63 | 4/5 |
| fixb03 | 466 | 83 | 91 | 148 | 62 | 82 | 5/5 |
| fixb04 | 511 | 130 | 62 | 164 | 77 | 78 | 5/5 |
| fixb05 | 982 | 64 | 256 | 117 | 480 | 64 | 4/5 |
| fixb06 | 392 | 61 | 87 | 82 | 98 | 64 | 5/5 |
| fixb07 | 419 | 64 | 69 | 144 | 82 | 60 | 5/5 |
| fixb08 | 436 | 81 | 92 | 96 | 69 | 97 | 5/5 |
| fixb09 | 434 | 67 | 64 | 182 | 63 | 58 | 5/5 |
| fixb10 | 416 | 68 | 84 | 114 | 89 | 62 | 5/5 |

- 合计 91.3 分钟；均 9.1、中位 7.5 分钟；最短 6.5（fixb06）、最长 16.4（fixb05）
- 去掉最长一轮后均值 8.3 分钟 —— 与中位差得远就说明是单轮异常而不是整体变慢
- 按时间分三段的中位：8.3、8.5、7.1 分钟（用中位不用均值；只看趋势，样本少不下结论）
- 每轮最慢的用例：`w5-long-notes-outline` 占 6/10 轮

| 用例 | 均 | 中位 | 最短 | 最长 | 占总墙钟 |
|---|---|---|---|---|---|
| q1-idempotent-no-tools | 158s | 81s | 62s | 480s | 29% |
| w5-long-notes-outline | 145s | 146s | 82s | 223s | 27% |
| c6-refactor-discount | 95s | 84s | 62s | 256s | 17% |
| f5-git-search-rea | 79s | 72s | 61s | 130s | 14% |
| h4-dead-repo | 71s | 64s | 58s | 97s | 13% |

- 最贵的是 `q1-idempotent-no-tools`（占 29%）：要缩短一轮，先动它；它的中位 81s 基本就是每轮的固定成本