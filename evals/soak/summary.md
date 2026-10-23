# 浸泡跑汇总（31/50 轮）

重算于 2026-10-21 23:04:37；每轮明细见 `issues.md`，机器可读见 `rounds.jsonl`。

## 1. 按用例

| 用例 | 轮次 | 通过 | 失败 | 通过率 | 均耗时 | 失败层 |
|---|---|---|---|---|---|---|
| f5-git-search-rea | 31 | 31 | 0 | 100% | 72s | — |
| c6-refactor-discount | 31 | 30 | 1 | 97% | 108s | crash×1 |
| w5-long-notes-outline | 31 | 29 | 2 | 94% | 174s | check×2 |
| q1-idempotent-no-tools | 31 | 31 | 0 | 100% | 81s | — |
| h4-dead-repo | 31 | 28 | 3 | 90% | 71s | tool×2、fuzzy×1 |

## 2. 不稳定用例（同一用例既有过也有失）

`c6-refactor-discount`、`w5-long-notes-outline`、`h4-dead-repo`

## 3. 按问题指纹（出现次数降序）

| 次数 | 规则 | 用例 | 级别 | 说明 | 首见 |
|---|---|---|---|---|---|
| 12 | `write_retry` | c6-refactor-discount | low | 中途写盘失败后自愈（第 2 轮失败 1 次，第 3 轮写入成功） | soak02 |
| 5 | `stamp:no_echo` | w5-long-notes-outline | mid | 答复含「未能核对到」（verdict=pass） | soak03 |
| 3 | `gate` | h4-dead-repo | low | 闸门 2 轮 | soak14 |
| 2 | `eval_fail` | h4-dead-repo | high | tool: 应调用指定工具之一（fetch_url、fetch_json、web_search、github_search），实际用过：ops_batch | soak04 |
| 2 | `tool_error` | h4-dead-repo | mid | 0 次工具调用 | soak04 |
| 2 | `eval_fail` | w5-long-notes-outline | high | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' | soak09 |
| 1 | `eval_fail` | c6-refactor-discount | high | crash: 超时（>480s） | soak04 |
| 1 | `slow` | c6-refactor-discount | low | 960s / 2 步 / 工具 ops_batch、read_file | soak04 |
| 1 | `tool_error` | c6-refactor-discount | mid | 0 次工具调用 | soak09 |
| 1 | `tool_error` | f5-git-search-rea | mid | 0 次工具调用 | soak16 |
| 1 | `eval_fail` | h4-dead-repo | high | fuzzy: 关键词组命中 0/1（要求 ≥1.0），未命中组：[['无法', '不存在', '没有', '失败', '未找到', '打不开', '不可用', 404]] | soak17 |
| 1 | `gate` | f5-git-search-rea | low | 闸门 1 轮 | soak25 |
| 1 | `tool_error` | w5-long-notes-outline | mid | 0 次工具调用 | soak27 |

## 4. 总账

- 轮次 31/50，用例判定 155 次，失败 6 次（3.9%）
- 问题条目 33 条，去重后 13 个指纹
- 总墙钟 261.6 分钟


## 5. 每轮耗时

单位秒。台账的 `墙钟` 是按各用例 `duration` 累加算出来的（bench 不单独记整轮用时），不含 python 启动与轮间清场；31 轮实测真实钟表比它长约 5 分钟，所以可以直接当墙钟看。

| 轮次 | 墙钟 | f5-git-search-rea | c6-refactor-discount | w5-long-notes-outline | q1-idempotent-no-tools | h4-dead-repo | 判定 |
|---|---|---|---|---|---|---|---|
| soak01 | 535 | 77 | 87 | 238 | 66 | 67 | 5/5 |
| soak02 | 534 | 74 | 124 | 149 | 130 | 58 | 5/5 |
| soak03 | 472 | 78 | 71 | 197 | 67 | 60 | 5/5 |
| soak04 | 1352 | 66 | 960 | 143 | 115 | 69 | 3/5 |
| soak05 | 479 | 65 | 83 | 191 | 80 | 59 | 5/5 |
| soak06 | 507 | 64 | 74 | 229 | 73 | 66 | 5/5 |
| soak07 | 471 | 68 | 72 | 152 | 114 | 65 | 5/5 |
| soak08 | 393 | 71 | 90 | 112 | 59 | 61 | 4/5 |
| soak09 | 420 | 63 | 72 | 127 | 99 | 59 | 4/5 |
| soak10 | 431 | 78 | 80 | 131 | 84 | 58 | 5/5 |
| soak11 | 437 | 67 | 84 | 138 | 83 | 65 | 5/5 |
| soak12 | 473 | 65 | 86 | 175 | 64 | 83 | 5/5 |
| soak13 | 456 | 74 | 70 | 171 | 80 | 61 | 5/5 |
| soak14 | 527 | 89 | 65 | 157 | 102 | 114 | 5/5 |
| soak15 | 477 | 64 | 69 | 188 | 93 | 62 | 4/5 |
| soak16 | 447 | 75 | 69 | 159 | 80 | 64 | 5/5 |
| soak17 | 527 | 68 | 70 | 219 | 61 | 110 | 4/5 |
| soak18 | 439 | 68 | 70 | 174 | 64 | 63 | 5/5 |
| soak19 | 446 | 90 | 79 | 102 | 100 | 76 | 5/5 |
| soak20 | 453 | 70 | 74 | 139 | 96 | 75 | 5/5 |
| soak21 | 470 | 73 | 74 | 188 | 66 | 71 | 5/5 |
| soak22 | 493 | 70 | 94 | 180 | 77 | 72 | 5/5 |
| soak23 | 462 | 69 | 64 | 190 | 59 | 81 | 5/5 |
| soak24 | 504 | 72 | 70 | 221 | 68 | 72 | 5/5 |
| soak25 | 479 | 81 | 67 | 189 | 83 | 59 | 5/5 |
| soak26 | 524 | 70 | 91 | 231 | 69 | 64 | 5/5 |
| soak27 | 475 | 66 | 92 | 181 | 72 | 64 | 5/5 |
| soak28 | 474 | 82 | 93 | 136 | 81 | 80 | 5/5 |
| soak29 | 510 | 86 | 74 | 201 | 79 | 70 | 5/5 |
| soak30 | 527 | 70 | 69 | 203 | 85 | 100 | 5/5 |
| soak31 | 498 | 69 | 98 | 191 | 60 | 80 | 5/5 |

- 合计 261.6 分钟；均 8.4、中位 7.9 分钟；最短 6.5（soak08）、最长 22.5（soak04）
- 去掉最长一轮后均值 8.0 分钟 —— 与中位差得远就说明是单轮异常而不是整体变慢
- 按时间分三段的中位：7.9、7.6、8.2 分钟（用中位不用均值；只看趋势，样本少不下结论）
- 每轮最慢的用例：`w5-long-notes-outline` 占 30/31 轮

| 用例 | 均 | 中位 | 最短 | 最长 | 占总墙钟 |
|---|---|---|---|---|---|
| w5-long-notes-outline | 174s | 180s | 102s | 238s | 34% |
| c6-refactor-discount | 108s | 74s | 64s | 960s | 21% |
| q1-idempotent-no-tools | 81s | 80s | 59s | 130s | 16% |
| f5-git-search-rea | 72s | 70s | 63s | 90s | 14% |
| h4-dead-repo | 71s | 66s | 58s | 114s | 14% |

- 最贵的是 `w5-long-notes-outline`（占 34%）：要缩短一轮，先动它；它的中位 180s 基本就是每轮的固定成本