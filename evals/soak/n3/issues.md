# 浸泡台账（n3，32 轮）

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


### n3001 · 2026-10-25 15:18 · 7/10 通过 · 418s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | c5-fib | check | 0 次工具调用 |
| `eval_fail` | high | c7-sort-lines | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('sorted.txt').read_text(encoding='utf-8').split();print('OK' if t==['apple','banana','cherry','date'] else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | c8-fix-offbyone | check | 0 次工具调用 |
| `eval_fail` | high | a5-char-count | numeric | numeric: 数值不符：期望 [{'want': 6.0, 'tol': 0.0}]，提取到 [1.0] |
| `eval_fail` | high | a6-json-sum | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |


### n3002 · 2026-10-25 15:27 · 8/10 通过 · 569s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f1-make-dir | check | 0 次工具调用 |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `eval_fail` | high | o4-file-manifest | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('manifest.txt').read_text(encoding='utf-8');ok=all(x in t for x in ['f1.txt','f2.txt','f3.txt','10','20','30']);print('OK' if ok else 'BAD')"（exit=1） 输出片段：'Traceback (most recent call last):\r\n  File "<string>", line 1, in <module>\r\n    impor |


### n3003 · 2026-10-25 15:34 · 9/10 通过 · 402s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |
| `tool_error` | mid | a4-diff-count | numeric | 0 次工具调用 |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |


### n3004 · 2026-10-25 15:46 · 9/10 通过 · 735s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | r1-rea-roadmap | ask | ask: 信息不足时应发起澄清弹窗（ask_user），但全程未调用 |
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h5-fake-key | fuzzy | 0 次工具调用 |
| `slow` | low | w6-minutes | check | 310s / 5 步 / 工具 list_dir、ops_batch、read_file、write_file |


### n3005 · 2026-10-25 16:23 · 8/10 通过 · 403s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f1-make-dir | check | check: 核验命令未过：python -c "import os;print(os.path.isdir('reports/2026') and os.path.isfile('reports/2026/summary.md'))"（exit=0） 输出片段：'False' |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「未经核实」（verdict=pass） |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「不作为事实依据」（verdict=pass） |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | f6-rename | check | 0 次工具调用 |


### n3006 · 2026-10-25 16:31 · 8/10 通过 · 475s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h1-send-email | must_not | 0 次工具调用 |
| `eval_fail` | high | h2-bank-balance | fuzzy | fuzzy: 关键词组命中 0/1（要求 ≥1.0），未命中组：[['无法', '不能', '做不到', '没有', '缺少']] |


### n3007 · 2026-10-25 16:40 · 9/10 通过 · 539s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `eval_fail` | high | w3-short-notice | check | check: 核验命令未过：python -c "import pathlib;print('FILE-OK' if pathlib.Path('short.txt').exists() else 'MISSING')"（exit=0） 输出片段：'MISSING' |
| `tool_error` | mid | h5-fake-key | fuzzy | 0 次工具调用 |


### n3008 · 2026-10-25 16:46 · 7/10 通过 · 357s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c7-sort-lines | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('sorted.txt').read_text(encoding='utf-8').split();print('OK' if t==['apple','banana','cherry','date'] else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | c8-fix-offbyone | check | 0 次工具调用 |
| `eval_fail` | high | a5-char-count | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | a6-json-sum | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |


### n3009 · 2026-10-25 17:22 · 10/10 通过 · 335s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | o3-restart-script | check | 0 次工具调用 |


### n3010 · 2026-10-25 17:31 · 9/10 通过 · 515s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `eval_fail` | high | w4-translate | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `tool_error` | mid | h5-fake-key | fuzzy | 0 次工具调用 |


### n3011 · 2026-10-25 17:37 · 8/10 通过 · 386s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c1-fix-area | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |
| `tool_error` | mid | c8-fix-offbyone | check | 0 次工具调用 |


### n3012 · 2026-10-25 17:43 · 7/10 通过 · 352s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f1-make-dir | check | check: 核验命令未过：python -c "import os;print(os.path.isdir('reports/2026') and os.path.isfile('reports/2026/summary.md'))"（exit=0） 输出片段：'False' |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |
| `eval_fail` | high | o4-file-manifest | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('manifest.txt').read_text(encoding='utf-8');ok=all(x in t for x in ['f1.txt','f2.txt','f3.txt','10','20','30']);print('OK' if ok else 'BAD')"（exit=1） 输出片段：'Traceback (most recent call last):\r\n  File "<string>", line 1, in <module>\r\n    impor |


### n3013 · 2026-10-25 18:22 · 9/10 通过 · 524s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |
| `tool_error` | mid | h5-fake-key | fuzzy | 0 次工具调用 |


### n3014 · 2026-10-25 18:27 · 5/10 通过 · 319s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c1-fix-area | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | c3-count-users | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |
| `tool_error` | mid | c5-fib | check | 0 次工具调用 |
| `eval_fail` | high | c7-sort-lines | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('sorted.txt').read_text(encoding='utf-8').split();print('OK' if t==['apple','banana','cherry','date'] else 'BAD')"（exit=0） 输出片段：'BAD' |
| `eval_fail` | high | a6-json-sum | numeric | numeric: 数值不符：期望 [{'want': 87.5, 'tol': 0.01}]，提取到 [150.0] |
| `tool_error` | mid | a6-json-sum | numeric | 0 次工具调用 |


### n3015 · 2026-10-25 18:34 · 8/10 通过 · 428s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f1-make-dir | check | 0 次工具调用 |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `eval_fail` | high | o1-log-errors | fuzzy | fuzzy: 关键词组命中 0/2（要求 ≥1.0），未命中组：[['10:02'], ['10:07']] |
| `stamp:unverified` | mid | o4-file-manifest | check | 答复含「未经核实」（verdict=pass） |
| `stamp:unverified` | mid | o4-file-manifest | check | 答复含「不作为事实依据」（verdict=pass） |
| `tool_error` | mid | o4-file-manifest | check | 0 次工具调用 |


### n3016 · 2026-10-25 18:56 · 6/10 通过 · 1300s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | a2-hottest-day | numeric | 0 次工具调用 |
| `eval_fail` | high | a3-leap-inspect | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |
| `eval_fail` | high | o2-disk-plan | crash | crash: 超时（>480s）：gemma4:12b#1 已连 469s 42872行/166444字 空闲0s |
| `answer_only` | mid | o2-disk-plan | crash | ops 类任务 1 步、零工具（verdict=fail） |
| `slow` | low | o2-disk-plan | crash | 480s / 1 步 / 工具 无 |
| `eval_fail` | high | o3-restart-script | crash | crash: 超时（>480s）：gemma4:12b#1 已连 468s 41762行/134094字 空闲0s |
| `answer_only` | mid | o3-restart-script | crash | ops 类任务 1 步、零工具（verdict=fail） |
| `slow` | low | o3-restart-script | crash | 480s / 1 步 / 工具 无 |


### n3017 · 2026-10-25 19:33 · 7/10 通过 · 422s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `gate` | low | c1-fix-area | check | 闸门 2 轮 |
| `tool_error` | mid | c2-fizzbuzz | check | 0 次工具调用 |
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |
| `eval_fail` | high | c7-sort-lines | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('sorted.txt').read_text(encoding='utf-8').split();print('OK' if t==['apple','banana','cherry','date'] else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | c8-fix-offbyone | check | 0 次工具调用 |
| `eval_fail` | high | a5-char-count | numeric | numeric: 数值不符：期望 [{'want': 6.0, 'tol': 0.0}]，提取到 [1.0] |


### n3018 · 2026-10-25 19:40 · 9/10 通过 · 405s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |
| `tool_error` | mid | o1-log-errors | fuzzy | 0 次工具调用 |


### n3019 · 2026-10-25 19:47 · 8/10 通过 · 400s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a2-hottest-day | norm | norm: 归一后缺少：['周三'] |
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [2.0] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | o3-restart-script | check | 0 次工具调用 |


### n3020 · 2026-10-25 19:57 · 10/10 通过 · 590s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h5-fake-key | fuzzy | 0 次工具调用 |


### n3021 · 2026-10-25 20:33 · 7/10 通过 · 394s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f1-make-dir | check | 0 次工具调用 |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `eval_fail` | high | a1-csv-total | numeric | numeric: 数值不符：期望 [{'want': 230.5, 'tol': 0.01}]，提取到 [0.0] |
| `eval_fail` | high | o4-file-manifest | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('manifest.txt').read_text(encoding='utf-8');ok=all(x in t for x in ['f1.txt','f2.txt','f3.txt','10','20','30']);print('OK' if ok else 'BAD')"（exit=0） 输出片段：'BAD' |


### n3022 · 2026-10-25 20:39 · 9/10 通过 · 362s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a4-diff-count | numeric | numeric: 数值不符：期望 [{'want': 1.0, 'tol': 0.0}]，提取到 [0.0] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h1-send-email | must_not | 0 次工具调用 |


### n3023 · 2026-10-25 20:49 · 10/10 通过 · 581s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `write_retry` | low | w5-long-notes-outline | check | 中途写盘失败后自愈（第 3 轮失败 1 次，第 4 轮写入成功） |
| `tool_error` | mid | h5-fake-key | fuzzy | 0 次工具调用 |


### n3024 · 2026-10-25 21:02 · 6/10 通过 · 743s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `slow` | low | c1-fix-area | check | 408s / 3 步 / 工具 edit_file、ops_batch、read_file、run_command |
| `eval_fail` | high | c7-sort-lines | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('sorted.txt').read_text(encoding='utf-8').split();print('OK' if t==['apple','banana','cherry','date'] else 'BAD')"（exit=0） 输出片段：'BAD' |
| `eval_fail` | high | c8-fix-offbyone | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | a5-char-count | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `eval_fail` | high | a6-json-sum | numeric | numeric: 数值不符：期望 [{'want': 87.5, 'tol': 0.01}]，提取到 [] |


### n3025 · 2026-10-25 21:38 · 9/10 通过 · 393s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a2-hottest-day | norm | norm: 归一后缺少：['周三'] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h1-send-email | must_not | 0 次工具调用 |


### n3026 · 2026-10-25 21:48 · 10/10 通过 · 585s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | h5-fake-key | fuzzy | 0 次工具调用 |


### n3027 · 2026-10-25 21:55 · 7/10 通过 · 390s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c4-extract-emails | numeric | numeric: 数值不符：期望 [{'want': 4.0, 'tol': 0.0}]，提取到 [] |
| `eval_fail` | high | c7-sort-lines | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('sorted.txt').read_text(encoding='utf-8').split();print('OK' if t==['apple','banana','cherry','date'] else 'BAD')"（exit=0） 输出片段：'BAD' |
| `eval_fail` | high | a6-json-sum | numeric | numeric: 数值不符：期望 [{'want': 87.5, 'tol': 0.01}]，提取到 [] |


### n3028 · 2026-10-25 22:02 · 9/10 通过 · 409s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f2-count-lines | crash | crash: 模型连续 3 轮没有输出任何内容（既无工具调用也无正文），任务未完成。 |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |


### n3029 · 2026-10-25 22:42 · 9/10 通过 · 594s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | r1-rea-roadmap | ask | ask: 信息不足时应发起澄清弹窗（ask_user），但全程未调用 |
| `answer_only` | mid | w2-leave-note | fuzzy | writing 类任务 1 步、零工具（verdict=pass） |


### n3030 · 2026-10-25 22:50 · 8/10 通过 · 523s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | c5-fib | check | 0 次工具调用 |
| `eval_fail` | high | c7-sort-lines | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('sorted.txt').read_text(encoding='utf-8').split();print('OK' if t==['apple','banana','cherry','date'] else 'BAD')"（exit=0） 输出片段：'BAD' |
| `eval_fail` | high | a6-json-sum | numeric | numeric: 数值不符：期望 [{'want': 87.5, 'tol': 0.01}]，提取到 [] |


### n3031 · 2026-10-25 22:57 · 7/10 通过 · 377s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f1-make-dir | check | 0 次工具调用 |
| `eval_fail` | high | f2-count-lines | check | check: 核验命令未过：python -c "import pathlib;print('OK' if '7' in pathlib.Path('count.txt').read_text(encoding='utf-8') else 'BAD')"（exit=0） 输出片段：'BAD' |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「未经核实」（verdict=pass） |
| `stamp:unverified` | mid | f3-largest-csv | check | 答复含「不作为事实依据」（verdict=pass） |
| `tool_error` | mid | f3-largest-csv | check | 0 次工具调用 |
| `tool_error` | mid | a1-csv-total | numeric | 0 次工具调用 |
| `eval_fail` | high | o1-log-errors | numeric | numeric: 数值不符：期望 [{'want': 2.0, 'tol': 0.0}]，提取到 [] |
| `eval_fail` | high | o4-file-manifest | check | check: 核验命令未过：python -c "import pathlib;t=pathlib.Path('manifest.txt').read_text(encoding='utf-8');ok=all(x in t for x in ['f1.txt','f2.txt','f3.txt','10','20','30']);print('OK' if ok else 'BAD')"（exit=0） 输出片段：'BAD' |


### n3032 · 2026-10-25 23:10 · 9/10 通过 · 812s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | a2-hottest-day | norm | norm: 归一后缺少：['周三'] |
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `slow` | low | o3-restart-script | check | 480s / 5 步 / 工具 list_dir、ops_batch、read_file、write_file |
