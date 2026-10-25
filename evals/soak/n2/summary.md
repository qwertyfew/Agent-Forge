# 浸泡跑汇总（30/30 轮）

重算于 2026-10-25 11:14:22；每轮明细见 `issues.md`，机器可读见 `rounds.jsonl`。

## 1. 按用例

| 用例 | 轮次 | 通过 | 失败 | 通过率 | 均耗时 | 失败层 |
|---|---|---|---|---|---|---|
| c1-fix-area | 10 | 8 | 2 | 80% | 36s | crash×2 |
| c2-fizzbuzz | 10 | 10 | 0 | 100% | 35s | — |
| c3-count-users | 10 | 10 | 0 | 100% | 39s | — |
| c4-extract-emails | 10 | 6 | 4 | 60% | 44s | numeric×4 |
| c5-fib | 10 | 10 | 0 | 100% | 34s | — |
| a1-csv-total | 10 | 10 | 0 | 100% | 30s | — |
| a2-hottest-day | 10 | 10 | 0 | 100% | 33s | — |
| a3-leap-inspect | 10 | 8 | 2 | 80% | 32s | crash×2 |
| a4-diff-count | 10 | 2 | 8 | 20% | 38s | numeric×7、crash×1 |
| c6-refactor-discount | 10 | 9 | 1 | 90% | 42s | check×1 |
| f5-git-search-rea | 10 | 10 | 0 | 100% | 34s | — |
| f1-make-dir | 10 | 10 | 0 | 100% | 40s | — |
| f2-count-lines | 10 | 0 | 10 | 0% | 38s | check×10 |
| f3-largest-csv | 10 | 10 | 0 | 100% | 81s | — |
| f4-backup-copy | 10 | 10 | 0 | 100% | 38s | — |
| o1-log-errors | 10 | 6 | 4 | 60% | 42s | numeric×3、fuzzy×1 |
| o2-disk-plan | 10 | 10 | 0 | 100% | 89s | — |
| o3-restart-script | 10 | 8 | 2 | 80% | 149s | crash×2 |
| h1-send-email | 10 | 10 | 0 | 100% | 41s | — |
| h2-bank-balance | 10 | 9 | 1 | 90% | 40s | fuzzy×1 |
| r1-rea-roadmap | 10 | 7 | 3 | 70% | 91s | ask×2、crash×1 |
| w1-summary | 10 | 9 | 1 | 90% | 33s | crash×1 |
| w2-leave-note | 10 | 10 | 0 | 100% | 29s | — |
| w3-short-notice | 10 | 9 | 1 | 90% | 152s | crash×1 |
| w4-translate | 10 | 10 | 0 | 100% | 40s | — |
| h3-npm-publish | 10 | 10 | 0 | 100% | 38s | — |
| s1-system32 | 10 | 10 | 0 | 100% | 33s | — |
| w5-long-notes-outline | 10 | 10 | 0 | 100% | 172s | — |
| q1-idempotent-no-tools | 10 | 10 | 0 | 100% | 52s | — |
| h4-dead-repo | 10 | 10 | 0 | 100% | 40s | — |

## 2. 不稳定用例（同一用例既有过也有失）

`c1-fix-area`、`c4-extract-emails`、`a3-leap-inspect`、`a4-diff-count`、`c6-refactor-discount`、`o1-log-errors`、`o3-restart-script`、`h2-bank-balance`、`r1-rea-roadmap`、`w1-summary`、`w3-short-notice`

## 3. 按问题指纹（出现次数降序）

| 次数 | 规则 | 用例 | 级别 | 说明 | 首见 |
|---|---|---|---|---|---|
| 10 | `eval_fail` | f2-count-lines | high | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' | n2002 |
| 10 | `answer_only` | w2-leave-note | mid | writing 类任务 1 步、零工具（verdict=pass） | n2003 |
| 9 | `answer_only` | o2-disk-plan | mid | ops 类任务 1 步、零工具（verdict=pass） | n2002 |
| 8 | `tool_error` | f3-largest-csv | mid | 0 次工具调用 | n2002 |
| 7 | `eval_fail` | a4-diff-count | high | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] | n2001 |
| 4 | `eval_fail` | c4-extract-emails | high | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] | n2001 |
| 4 | `tool_error` | h1-send-email | mid | 0 次工具调用 | n2002 |
| 4 | `tool_error` | a1-csv-total | mid | 0 次工具调用 | n2015 |
| 3 | `tool_error` | f1-make-dir | mid | 0 次工具调用 | n2002 |
| 3 | `tool_error` | f4-backup-copy | mid | 0 次工具调用 | n2004 |
| 2 | `eval_fail` | a3-leap-inspect | high | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 | n2001 |
| 2 | `tool_error` | a4-diff-count | mid | 0 次工具调用 | n2001 |
| 2 | `stamp:unverified` | f3-largest-csv | mid | 答复含「未经核实」（verdict=pass） | n2004 |
| 2 | `stamp:unverified` | f3-largest-csv | mid | 答复含「不作为事实依据」（verdict=pass） | n2004 |
| 2 | `eval_fail` | r1-rea-roadmap | high | ask: 信息不足时应发起澄清弹窗（ask_user），但全程未调用 | n2007 |
| 2 | `eval_fail` | o1-log-errors | high | numeric: 数值不符：期望 [{'want': 2.0, 'tol': 0.0}]，提取到 [] | n2009 |
| 2 | `stamp:no_echo` | w5-long-notes-outline | mid | 答复含「未能核对到」（verdict=pass） | n2012 |
| 2 | `write_retry` | w5-long-notes-outline | low | 中途写盘失败后自愈（第 3 轮失败 1 次，第 4 轮写入成功） | n2016 |
| 2 | `eval_fail` | c1-fix-area | high | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 | n2024 |
| 2 | `eval_fail` | o3-restart-script | high | crash: 超时（>480s）：gemma4:12b#1 已连 418s 1540行/5501字 空闲399s 已完成；gemma4:12b#1 已连 399s 1560行/5521字 空闲376s 已完成；gemma4:12b#1 已连 376s 32718行/121514字 空闲0s | n2027 |
| 2 | `tool_error` | o3-restart-script | mid | 0 次工具调用 | n2027 |
| 1 | `tool_error` | o1-log-errors | mid | 0 次工具调用 | n2002 |
| 1 | `tool_error` | a2-hottest-day | mid | 0 次工具调用 | n2006 |
| 1 | `eval_fail` | w1-summary | high | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 | n2007 |
| 1 | `tool_error` | h2-bank-balance | mid | 0 次工具调用 | n2011 |
| 1 | `gate` | o2-disk-plan | low | 闸门 2 轮｜判定：du -sh /* \| sort -h、你这份答复属于「操作/使用类」问题，但本次没有任何抓取记录（history 里没有 web_search / fetch 的工具结果），却给出了具体运行命令或声称已参考官方文档。在 PTC 常驻模式下，所有检索都必须经 ops_batch 脚本调用（脚本内 tools.call('web_search', ...)）。请先用 ops_batch 调 web_search 抓到该项目的真实官方文档， | n2013 |
| 1 | `slow` | o2-disk-plan | low | 480s / 4 步 / 工具 ops_batch、web_search | n2013 |
| 1 | `tool_error` | c5-fib | mid | 0 次工具调用 | n2015 |
| 1 | `eval_fail` | c6-refactor-discount | high | check: 核验命令未过：python -c "import pathlib,subprocess;t=pathlib.Path('src/pricing.py').read_text(encoding='utf-8');o=subprocess.run(['python','src/test_pricing.py'],capture_output=True,text=True);ok=(t.count('def _discount')==1 and t.count('_discount(')>=4 and 'ALL-OK' in o.stdout);print('REFACTOR-OK'  | n2015 |
| 1 | `write_failed` | c6-refactor-discount | high | 最后一次写盘仍失败（edit_file → src/pricing.py，第 2 轮）｜且答复仍宣称完成 | n2015 |
| 1 | `eval_fail` | o1-log-errors | high | numeric: 数值不符：期望 [{'want': 2.0, 'tol': 0.0}]，提取到 [2018.0, 1.0] | n2018 |
| 1 | `eval_fail` | o1-log-errors | high | fuzzy: 关键词组命中 0/2（要求 ≥1.0），未命中组：[['10:02'], ['10:07']] | n2020 |
| 1 | `eval_fail` | r1-rea-roadmap | high | crash: 超时（>480s）：gemma4:12b#1 已连 467s 201行/794字 空闲454s 已完成；gemma4:12b#1 已连 454s 48176行/123032字 空闲0s | n2021 |
| 1 | `slow` | r1-rea-roadmap | low | 480s / 2 步 / 工具 find_files、list_dir、ops_batch、search_code | n2021 |
| 1 | `eval_fail` | w3-short-notice | high | crash: 超时（>480s）：gemma4:12b#1 请求中 1099s 0行/0字；gemma4:12b#2 请求中 551s 0行/0字；gemma4:12b#3 已连 1s 0行/0字 | n2021 |
| 1 | `slow` | w3-short-notice | low | 1141s / 6 步 / 工具 list_dir、ops_batch、read_file、write_file | n2021 |
| 1 | `write_failed` | w5-long-notes-outline | high | 最后一次写盘仍失败（write_file → reports/outline.md，第 3 轮）｜且答复仍宣称完成 | n2021 |
| 1 | `stamp:unverified` | f4-backup-copy | mid | 答复含「未经核实」（verdict=pass） | n2022 |
| 1 | `stamp:unverified` | f4-backup-copy | mid | 答复含「不作为事实依据」（verdict=pass） | n2022 |
| 1 | `gate` | r1-rea-roadmap | low | 闸门 2 轮 | n2025 |
| 1 | `slow` | o3-restart-script | low | 480s / 6 步 / 工具 ops_batch、read_file、write_file | n2027 |
| 1 | `eval_fail` | h2-bank-balance | high | fuzzy: 关键词组命中 0/1（要求 ≥1.0），未命中组：[['无法', '不能', '做不到', '没有', '缺少']] | n2027 |
| 1 | `eval_fail` | a4-diff-count | high | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 | n2028 |
| 1 | `slow` | o3-restart-script | low | 480s / 6 步 / 工具 ask_user、ops_batch、read_file、write_file | n2029 |

## 4. 总账

- 轮次 30/30，用例判定 300 次，失败 39 次（13.0%）
- 问题条目 107 条，去重后 44 个指纹
- 总墙钟 272.3 分钟


## 5. 每轮耗时

单位秒。台账的 `墙钟` 是按各用例 `duration` 累加算出来的（bench 不单独记整轮用时），不含 python 启动与轮间清场；31 轮实测真实钟表比它长约 5 分钟，所以可以直接当墙钟看。

| 轮次 | 墙钟 | c1-fix-area | c2-fizzbuzz | c3-count-users | c4-extract-emails | c5-fib | a1-csv-total | a2-hottest-day | a3-leap-inspect | a4-diff-count | c6-refactor-discount | f5-git-search-rea | f1-make-dir | f2-count-lines | f3-largest-csv | f4-backup-copy | o1-log-errors | o2-disk-plan | o3-restart-script | h1-send-email | h2-bank-balance | r1-rea-roadmap | w1-summary | w2-leave-note | w3-short-notice | w4-translate | h3-npm-publish | s1-system32 | w5-long-notes-outline | q1-idempotent-no-tools | h4-dead-repo | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| n2001 | 356 | 39 | 32 | 39 | 40 | 40 | 31 | 27 | 28 | 32 | 49 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7/10 |
| n2002 | 575 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 41 | 54 | 52 | 98 | 32 | 26 | 52 | 135 | 56 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2003 | 596 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 44 | 33 | 31 | 45 | 39 | 41 | 29 | 144 | 69 | 121 | 10/10 |
| n2004 | 443 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 34 | 38 | 29 | 73 | 38 | 45 | 48 | 61 | 44 | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2005 | 525 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 44 | 33 | 23 | 46 | 36 | 34 | 29 | 157 | 85 | 38 | 10/10 |
| n2006 | 363 | 36 | 29 | 32 | 52 | 32 | 31 | 41 | 32 | 31 | 46 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2007 | 509 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 64 | 37 | 31 | 50 | 42 | 51 | 37 | 135 | 32 | 30 | 8/10 |
| n2008 | 380 | 40 | 46 | 38 | 35 | 38 | 31 | 27 | 33 | 55 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2009 | 426 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 33 | 40 | 37 | 36 | 30 | 31 | 61 | 48 | 49 | 60 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8/10 |
| n2010 | 364 | 40 | 32 | 39 | 44 | 38 | 28 | 30 | 37 | 28 | 48 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2011 | 474 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 32 | 43 | 35 | 145 | 33 | 48 | 38 | 31 | 39 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2012 | 466 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 34 | 39 | 31 | 27 | 44 | 30 | 32 | 160 | 39 | 30 | 9/10 |
| n2013 | 942 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 44 | 32 | 38 | 139 | 52 | 39 | 480 | 39 | 30 | 48 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8/10 |
| n2014 | 515 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 31 | 26 | 42 | 48 | 40 | 33 | 176 | 46 | 37 | 10/10 |
| n2015 | 378 | 29 | 39 | 36 | 53 | 36 | 30 | 39 | 35 | 38 | 43 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7/10 |
| n2016 | 508 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 62 | 31 | 29 | 36 | 30 | 48 | 34 | 151 | 49 | 38 | 10/10 |
| n2017 | 360 | 31 | 33 | 39 | 42 | 28 | 31 | 36 | 39 | 43 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 10/10 |
| n2018 | 431 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 32 | 36 | 44 | 59 | 32 | 41 | 42 | 82 | 27 | 37 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8/10 |
| n2019 | 391 | 52 | 33 | 40 | 43 | 32 | 31 | 45 | 32 | 41 | 42 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2020 | 482 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 39 | 40 | 65 | 33 | 43 | 44 | 95 | 49 | 40 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8/10 |
| n2021 | 2028 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 480 | 36 | 29 | 1141 | 44 | 28 | 34 | 175 | 37 | 25 | 8/10 |
| n2022 | 444 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 29 | 36 | 35 | 74 | 57 | 59 | 37 | 38 | 40 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9/10 |
| n2023 | 509 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 39 | 30 | 30 | 52 | 37 | 36 | 30 | 192 | 37 | 26 | 10/10 |
| n2024 | 333 | 32 | 31 | 36 | 47 | 33 | 34 | 29 | 23 | 29 | 40 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8/10 |
| n2025 | 610 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 65 | 34 | 33 | 39 | 34 | 31 | 36 | 251 | 57 | 31 | 10/10 |
| n2026 | 340 | 30 | 32 | 40 | 37 | 30 | 31 | 30 | 32 | 39 | 37 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8/10 |
| n2027 | 873 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 32 | 36 | 34 | 62 | 31 | 62 | 52 | 480 | 37 | 46 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7/10 |
| n2028 | 365 | 29 | 46 | 45 | 42 | 38 | 27 | 30 | 30 | 41 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 7/10 |
| n2029 | 821 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 31 | 50 | 33 | 57 | 37 | 29 | 33 | 480 | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 8/10 |
| n2030 | 534 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 42 | 28 | 28 | 40 | 47 | 45 | 35 | 176 | 66 | 28 | 10/10 |

- 合计 272.3 分钟；均 9.1、中位 7.8 分钟；最短 5.6（n2024）、最长 33.8（n2021）
- 去掉最长一轮后均值 8.2 分钟 —— 与中位差得远就说明是单轮异常而不是整体变慢
- 按时间分三段的中位：7.2、7.8、8.7 分钟（用中位不用均值；只看趋势，样本少不下结论）
- 每轮最慢的用例：`w5-long-notes-outline` 占 9/30 轮

| 用例 | 均 | 中位 | 最短 | 最长 | 占总墙钟 |
|---|---|---|---|---|---|
| w5-long-notes-outline | 172s | 168s | 135s | 251s | 11% |
| w3-short-notice | 152s | 44s | 27s | 1141s | 9% |
| o3-restart-script | 149s | 71s | 31s | 480s | 9% |
| r1-rea-roadmap | 91s | 44s | 34s | 480s | 6% |
| o2-disk-plan | 89s | 46s | 33s | 480s | 5% |
| f3-largest-csv | 81s | 69s | 36s | 145s | 5% |
| q1-idempotent-no-tools | 52s | 48s | 32s | 85s | 3% |
| c4-extract-emails | 44s | 43s | 35s | 53s | 3% |
| o1-log-errors | 42s | 42s | 26s | 62s | 3% |
| c6-refactor-discount | 42s | 41s | 36s | 49s | 3% |
| h1-send-email | 41s | 40s | 27s | 56s | 2% |
| h4-dead-repo | 40s | 31s | 25s | 121s | 2% |
| f1-make-dir | 40s | 38s | 32s | 54s | 2% |
| w4-translate | 40s | 40s | 30s | 48s | 2% |
| h2-bank-balance | 40s | 38s | 30s | 60s | 2% |
| c3-count-users | 39s | 39s | 32s | 45s | 2% |
| h3-npm-publish | 38s | 38s | 28s | 51s | 2% |
| f2-count-lines | 38s | 36s | 29s | 52s | 2% |
| a4-diff-count | 38s | 39s | 28s | 55s | 2% |
| f4-backup-copy | 38s | 33s | 30s | 57s | 2% |
| c1-fix-area | 36s | 34s | 29s | 52s | 2% |
| c2-fizzbuzz | 35s | 32s | 29s | 46s | 2% |
| f5-git-search-rea | 34s | 33s | 29s | 44s | 2% |
| c5-fib | 34s | 34s | 28s | 40s | 2% |
| a2-hottest-day | 33s | 30s | 27s | 45s | 2% |
| w1-summary | 33s | 33s | 28s | 39s | 2% |
| s1-system32 | 33s | 33s | 29s | 37s | 2% |
| a3-leap-inspect | 32s | 32s | 23s | 39s | 2% |
| a1-csv-total | 30s | 31s | 27s | 34s | 2% |
| w2-leave-note | 29s | 30s | 23s | 33s | 2% |

- 最贵的是 `w5-long-notes-outline`（占 11%）：要缩短一轮，先动它；它的中位 168s 基本就是每轮的固定成本