# 浸泡跑汇总（10/10 轮）

重算于 2026-10-22 05:52:20；每轮明细见 `issues.md`，机器可读见 `rounds.jsonl`。

## 1. 按用例

| 用例 | 轮次 | 通过 | 失败 | 通过率 | 均耗时 | 失败层 |
|---|---|---|---|---|---|---|
| f5-git-search-rea | 10 | 9 | 1 | 90% | 75s | exact×1 |
| c6-refactor-discount | 10 | 9 | 1 | 90% | 80s | check×1 |
| w5-long-notes-outline | 10 | 7 | 3 | 70% | 147s | check×3 |
| q1-idempotent-no-tools | 10 | 9 | 1 | 90% | 115s | crash×1 |
| h4-dead-repo | 10 | 10 | 0 | 100% | 74s | — |

## 2. 不稳定用例（同一用例既有过也有失）

`f5-git-search-rea`、`c6-refactor-discount`、`w5-long-notes-outline`、`q1-idempotent-no-tools`

## 3. 按问题指纹（出现次数降序）

| 次数 | 规则 | 用例 | 级别 | 说明 | 首见 |
|---|---|---|---|---|---|
| 3 | `write_retry` | c6-refactor-discount | low | 中途写盘失败后自愈（第 2 轮失败 2 次，第 2 轮写入成功） | fixc04 |
| 3 | `eval_fail` | w5-long-notes-outline | high | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' | fixc04 |
| 2 | `tool_error` | c6-refactor-discount | mid | 0 次工具调用 | fixc07 |
| 1 | `eval_fail` | f5-git-search-rea | high | exact: 缺少原文片段：['REA'] | fixc01 |
| 1 | `tool_error` | w5-long-notes-outline | mid | 0 次工具调用 | fixc01 |
| 1 | `write_failed` | w5-long-notes-outline | high | 最后一次写盘仍失败（write_file → reports/outline.md，第 3 轮）｜且答复仍宣称完成 | fixc02 |
| 1 | `eval_fail` | q1-idempotent-no-tools | high | crash: 超时（>480s）：gemma4:12b#1 已连 442s 38102行/96747字 空闲0s | fixc03 |
| 1 | `slow` | q1-idempotent-no-tools | low | 480s / 1 步 / 工具 无 | fixc03 |
| 1 | `eval_fail` | c6-refactor-discount | high | check: 核验命令未过：python -c "import pathlib,subprocess;t=pathlib.Path('src/pricing.py').read_text(encoding='utf-8');o=subprocess.run(['python','src/test_pricing.py'],capture_output=True,text=True);ok=(t.count('def _discount')==1 and t.count('_discount(')>=4 and 'ALL-OK' in o.stdout);print('REFACTOR-OK'  | fixc06 |
| 1 | `write_failed` | c6-refactor-discount | high | 最后一次写盘仍失败（edit_file → src/pricing.py，第 2 轮）｜且答复仍宣称完成 | fixc06 |
| 1 | `gate` | h4-dead-repo | low | 闸门 1 轮 | fixc10 |

## 4. 总账

- 轮次 10/10，用例判定 50 次，失败 6 次（12.0%）
- 问题条目 16 条，去重后 11 个指纹
- 总墙钟 81.8 分钟


## 5. 每轮耗时

单位秒。台账的 `墙钟` 是按各用例 `duration` 累加算出来的（bench 不单独记整轮用时），不含 python 启动与轮间清场；31 轮实测真实钟表比它长约 5 分钟，所以可以直接当墙钟看。

| 轮次 | 墙钟 | f5-git-search-rea | c6-refactor-discount | w5-long-notes-outline | q1-idempotent-no-tools | h4-dead-repo | 判定 |
|---|---|---|---|---|---|---|---|
| fixc01 | 446 | 63 | 74 | 180 | 65 | 64 | 4/5 |
| fixc02 | 490 | 84 | 66 | 158 | 90 | 93 | 5/5 |
| fixc03 | 813 | 67 | 66 | 127 | 480 | 73 | 4/5 |
| fixc04 | 412 | 66 | 74 | 147 | 64 | 62 | 4/5 |
| fixc05 | 459 | 67 | 89 | 160 | 77 | 66 | 5/5 |
| fixc06 | 540 | 85 | 93 | 198 | 78 | 86 | 4/5 |
| fixc07 | 388 | 76 | 82 | 101 | 64 | 65 | 4/5 |
| fixc08 | 416 | 84 | 89 | 74 | 103 | 66 | 5/5 |
| fixc09 | 419 | 71 | 103 | 120 | 58 | 67 | 5/5 |
| fixc10 | 523 | 82 | 67 | 204 | 69 | 100 | 4/5 |

- 合计 81.8 分钟；均 8.2、中位 7.5 分钟；最短 6.5（fixc07）、最长 13.6（fixc03）
- 去掉最长一轮后均值 7.6 分钟 —— 与中位差得远就说明是单轮异常而不是整体变慢
- 按时间分三段的中位：8.2、7.7、7.0 分钟（用中位不用均值；只看趋势，样本少不下结论）
- 每轮最慢的用例：`w5-long-notes-outline` 占 8/10 轮

| 用例 | 均 | 中位 | 最短 | 最长 | 占总墙钟 |
|---|---|---|---|---|---|
| w5-long-notes-outline | 147s | 152s | 74s | 204s | 30% |
| q1-idempotent-no-tools | 115s | 73s | 58s | 480s | 23% |
| c6-refactor-discount | 80s | 78s | 66s | 103s | 16% |
| f5-git-search-rea | 75s | 74s | 63s | 85s | 15% |
| h4-dead-repo | 74s | 66s | 62s | 100s | 15% |

- 最贵的是 `w5-long-notes-outline`（占 30%）：要缩短一轮，先动它；它的中位 152s 基本就是每轮的固定成本