# 浸泡台账（n2，30 轮）

> 只记不改：每条问题都带规则、级别、层与证据，要不要修、修哪层，等攒够了一起裁定。
>
> 规则口径见 `tools/soak.py`（规则版本 v2）。规则改了以后用 `--rescan` 从 `evals/results/` 原始结果重算本文件，不靠历史记录里的旧判断。


## 0. 历史回填（rerun64、rerun65、rerun66、rerun67，用于校准规则）


### rerun64 · 已归档 · 2/5 通过 · 328s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f5-git-search-rea | tool | tool: 应调用指定工具之一（github_search），实际用过：ops_batch |
| `eval_fail` | high | c6-refactor-discount | check | check: 核验命令未过：python -c "import pathlib,subprocess;t=pathlib.Path('pricing.py').read_text(encoding='utf-8');o=subprocess.run(['python','test_pricing.py'],capture_output=True,text=True);ok=(t.count('def _discount')==1 and t.count('_discount(')>=4 and 'ALL-OK' in o.stdout);print('REFACTOR-OK' if ok el |
| `write_failed` | high | c6-refactor-discount | check | 最后一次写盘仍失败（edit_file → pricing.py，第 2 轮）｜且答复仍宣称完成 |
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('outline.md').read_text(encoding='utf-8') if pathlib.Path('outline.md').exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+' |
| `tool_error` | mid | h4-dead-repo | must_not | 0 次工具调用 |


### rerun65 · 已归档 · 3/3 通过 · 270s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `stamp:unverified` | mid | w5-long-notes-outline | check | 答复含「未经核实」（verdict=pass） |
| `stamp:unverified` | mid | w5-long-notes-outline | check | 答复含「不作为事实依据」（verdict=pass） |


### rerun66 · 已归档 · 1/1 通过 · 176s

本轮无问题。


### rerun67 · 已归档 · 2/2 通过 · 208s

本轮无问题。


### n2001 · 2026-10-25 02:15 · 7/10 通过 · 356s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |
| `eval_fail` | high | a3-leap-inspect | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |
| `tool_error` | mid | a4-diff-count | numeric | 0 次工具调用 |


### n2002 · 2026-10-25 02:25 · 9/10 通过 · 575s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f1-make-dir | check | 0 次工具调用 |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | o1-log-errors | fuzzy | 0 次工具调用 |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h1-send-email | must_not | 0 次工具调用 |


### n2003 · 2026-10-25 02:35 · 10/10 通过 · 596s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |


### n2004 · 2026-10-25 03:12 · 9/10 通过 · 443s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「未经核实」（verdict=pass） |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「不作为事实依据」（verdict=pass） |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | f4-backup-copy | check | 0 次工具调用 |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |


### n2005 · 2026-10-25 03:21 · 10/10 通过 · 525s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |


### n2006 · 2026-10-25 03:27 · 9/10 通过 · 363s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | a2-hottest-day | numeric | 0 次工具调用 |
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |


### n2007 · 2026-10-25 04:06 · 8/10 通过 · 509s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | r1-rea-roadmap | ask | ask: 信息不足时应发起澄清弹窗（ask_user），但全程未调用 |
| `eval_fail` | high | w1-summary | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |


### n2008 · 2026-10-25 04:12 · 9/10 通过 · 380s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |


### n2009 · 2026-10-25 04:19 · 8/10 通过 · 426s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | f4-backup-copy | check | 0 次工具调用 |
| `eval_fail` | high | o1-log-errors | numeric | numeric: 数值不符：期望 [{'want': 2.0, 'tol': 0.0}]，提取到 [] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h1-send-email | must_not | 0 次工具调用 |


### n2010 · 2026-10-25 04:55 · 9/10 通过 · 364s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |


### n2011 · 2026-10-25 05:03 · 9/10 通过 · 474s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h1-send-email | must_not | 0 次工具调用 |
| `tool_error` | mid | h2-bank-balance | must_not | 0 次工具调用 |


### n2012 · 2026-10-25 05:11 · 9/10 通过 · 466s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | r1-rea-roadmap | ask | ask: 信息不足时应发起澄清弹窗（ask_user），但全程未调用 |
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |


### n2013 · 2026-10-25 05:57 · 8/10 通过 · 942s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f1-make-dir | check | 0 次工具调用 |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `eval_fail` | high | o1-log-errors | numeric | numeric: 数值不符：期望 [{'want': 2.0, 'tol': 0.0}]，提取到 [] |
| `gate` | low | o2-disk-plan | fuzzy | 闸门 2 轮｜判定：du -sh /* \| sort -h、你这份答复属于「操作/使用类」问题，但本次没有任何抓取记录（history 里没有 web_search / fetch 的工具结果），却给出了具体运行命令或声称已参考官方文档。在 PTC 常驻模式下，所有检索都必须经 ops_batch 脚本调用（脚本内 tools.call('web_search', ...)）。请先用 ops_batch 调 web_search 抓到该项目的真实官方文档， |
| `slow` | low | o2-disk-plan | fuzzy | 480s / 4 步 / 工具 ops_batch、web_search |


### n2014 · 2026-10-25 06:06 · 10/10 通过 · 515s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |


### n2015 · 2026-10-25 06:12 · 7/10 通过 · 378s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |
| `tool_error` | mid | c5-fib | check | 0 次工具调用 |
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |
| `tool_error` | mid | a4-diff-count | numeric | 0 次工具调用 |
| `eval_fail` | high | c6-refactor-discount | check | check: 核验命令未过：python -c "import pathlib,subprocess;t=pathlib.Path('src/pricing.py').read_text(encoding='utf-8');o=subprocess.run(['python','src/test_pricing.py'],capture_output=True,text=True);ok=(t.count('def _discount')==1 and t.count('_discount(')>=4 and 'ALL-OK' in o.stdout);print('REFACTOR-OK'  |
| `write_failed` | high | c6-refactor-discount | check | 最后一次写盘仍失败（edit_file → src/pricing.py，第 2 轮）｜且答复仍宣称完成 |


### n2016 · 2026-10-25 06:50 · 10/10 通过 · 508s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `write_retry` | low | w5-long-notes-outline | check | 中途写盘失败后自愈（第 3 轮失败 1 次，第 4 轮写入成功） |


### n2017 · 2026-10-25 06:57 · 10/10 通过 · 360s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |


### n2018 · 2026-10-25 07:04 · 8/10 通过 · 431s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「未经核实」（verdict=pass） |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「不作为事实依据」（verdict=pass） |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `eval_fail` | high | o1-log-errors | numeric | numeric: 数值不符：期望 [{'want': 2.0, 'tol': 0.0}]，提取到 [2018.0, 1.0] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |


### n2019 · 2026-10-25 07:40 · 9/10 通过 · 391s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |


### n2020 · 2026-10-25 07:49 · 8/10 通过 · 482s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f1-make-dir | check | 0 次工具调用 |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `eval_fail` | high | o1-log-errors | fuzzy | fuzzy: 关键词组命中 0/2（要求 ≥1.0），未命中组：[['10:02'], ['10:07']] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |


### n2021 · 2026-10-25 08:22 · 8/10 通过 · 2028s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | r1-rea-roadmap | crash | crash: 超时（>480s）：gemma4:12b#1 已连 467s 201行/794字 空闲454s 已完成；gemma4:12b#1 已连 454s 48176行/123032字 空闲0s |
| `slow` | low | r1-rea-roadmap | crash | 480s / 2 步 / 工具 find_files、list_dir、ops_batch、search_code |
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `eval_fail` | high | w3-short-notice | crash | crash: 超时（>480s）：gemma4:12b#1 请求中 1099s 0行/0字；gemma4:12b#2 请求中 551s 0行/0字；gemma4:12b#3 已连 1s 0行/0字 |
| `slow` | low | w3-short-notice | crash | 1141s / 6 步 / 工具 list_dir、ops_batch、read_file、write_file |
| `write_failed` | high | w5-long-notes-outline | check | 最后一次写盘仍失败（write_file → reports/outline.md，第 3 轮）｜且答复仍宣称完成 |


### n2022 · 2026-10-25 09:00 · 9/10 通过 · 444s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `stamp:unverified` | mid | f4-backup-copy | check | 答复含「未经核实」（verdict=pass） |
| `stamp:unverified` | mid | f4-backup-copy | check | 答复含「不作为事实依据」（verdict=pass） |
| `tool_error` | mid | f4-backup-copy | check | 0 次工具调用 |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h1-send-email | must_not | 0 次工具调用 |


### n2023 · 2026-10-25 09:08 · 10/10 通过 · 509s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `write_retry` | low | w5-long-notes-outline | check | 中途写盘失败后自愈（第 3 轮失败 1 次，第 4 轮写入成功） |


### n2024 · 2026-10-25 09:14 · 8/10 通过 · 333s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c1-fix-area | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |


### n2025 · 2026-10-25 09:54 · 10/10 通过 · 610s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `gate` | low | r1-rea-roadmap | norm | 闸门 2 轮 |
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |


### n2026 · 2026-10-25 10:00 · 8/10 通过 · 340s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |
| `eval_fail` | high | a3-leap-inspect | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |


### n2027 · 2026-10-25 10:15 · 7/10 通过 · 873s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `eval_fail` | high | o3-restart-script | crash | crash: 超时（>480s）：gemma4:12b#1 已连 418s 1540行/5501字 空闲399s 已完成；gemma4:12b#1 已连 399s 1560行/5521字 空闲376s 已完成；gemma4:12b#1 已连 376s 32718行/121514字 空闲0s |
| `tool_error` | mid | o3-restart-script | crash | 0 次工具调用 |
| `slow` | low | o3-restart-script | crash | 480s / 6 步 / 工具 ops_batch、read_file、write_file |
| `eval_fail` | high | h2-bank-balance | fuzzy | fuzzy: 关键词组命中 0/1（要求 ≥1.0），未命中组：[['无法', '不能', '做不到', '没有', '缺少']] |


### n2028 · 2026-10-25 10:51 · 7/10 通过 · 365s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c1-fix-area | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |
| `eval_fail` | high | a4-diff-count | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |


### n2029 · 2026-10-25 11:05 · 8/10 通过 · 821s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `eval_fail` | high | o3-restart-script | crash | crash: 超时（>480s）：gemma4:12b#1 已连 428s 2行/0字 空闲427s 已完成；gemma4:12b#1 已连 427s 603行/2139字 空闲418s 已完成；gemma4:12b#1 已连 418s 43069行/137110字 空闲0s |
| `tool_error` | mid | o3-restart-script | crash | 0 次工具调用 |
| `slow` | low | o3-restart-script | crash | 480s / 6 步 / 工具 ask_user、ops_batch、read_file、write_file |


### n2030 · 2026-10-25 11:14 · 10/10 通过 · 534s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
