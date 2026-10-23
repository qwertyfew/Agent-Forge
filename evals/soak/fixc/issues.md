# 浸泡台账（fixc，10 轮）

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


### fixc01 · 2026-10-22 03:41 · 4/5 通过 · 446s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | f5-git-search-rea | exact | exact: 缺少原文片段：['REA'] |
| `tool_error` | mid | w5-long-notes-outline | check | 0 次工具调用 |


### fixc02 · 2026-10-22 03:49 · 5/5 通过 · 490s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_failed` | high | w5-long-notes-outline | check | 最后一次写盘仍失败（write_file → reports/outline.md，第 3 轮）｜且答复仍宣称完成 |


### fixc03 · 2026-10-22 04:03 · 4/5 通过 · 813s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | q1-idempotent-no-tools | crash | crash: 超时（>480s）：gemma4:12b#1 已连 442s 38102行/96747字 空闲0s |
| `slow` | low | q1-idempotent-no-tools | crash | 480s / 1 步 / 工具 无 |


### fixc04 · 2026-10-22 04:10 · 4/5 通过 · 412s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 2 轮写入成功） |
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |


### fixc05 · 2026-10-22 04:17 · 5/5 通过 · 459s

本轮无问题。


### fixc06 · 2026-10-22 04:26 · 4/5 通过 · 540s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c6-refactor-discount | check | check: 核验命令未过：python -c "import pathlib,subprocess;t=pathlib.Path('src/pricing.py').read_text(encoding='utf-8');o=subprocess.run(['python','src/test_pricing.py'],capture_output=True,text=True);ok=(t.count('def _discount')==1 and t.count('_discount(')>=4 and 'ALL-OK' in o.stdout);print('REFACTOR-OK'  |
| `write_failed` | high | c6-refactor-discount | check | 最后一次写盘仍失败（edit_file → src/pricing.py，第 2 轮）｜且答复仍宣称完成 |


### fixc07 · 2026-10-22 04:33 · 4/5 通过 · 388s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | c6-refactor-discount | check | 0 次工具调用 |
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |


### fixc08 · 2026-10-22 04:40 · 5/5 通过 · 416s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 2 轮写入成功） |


### fixc09 · 2026-10-22 04:47 · 5/5 通过 · 419s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | c6-refactor-discount | check | 0 次工具调用 |


### fixc10 · 2026-10-22 05:49 · 4/5 通过 · 523s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 2 轮写入成功） |
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |
| `gate` | low | h4-dead-repo | must_not | 闸门 1 轮 |
