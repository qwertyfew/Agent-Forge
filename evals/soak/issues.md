# 浸泡台账（soak，50 轮）

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


### soak01 · 2026-10-21 18:25 · 5/5 通过 · 535s

本轮无问题。


### soak02 · 2026-10-21 18:33 · 5/5 通过 · 534s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 1 次，第 3 轮写入成功） |


### soak03 · 2026-10-21 18:45 · 5/5 通过 · 472s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |


### soak04 · 2026-10-21 19:08 · 3/5 通过 · 1352s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | c6-refactor-discount | crash | crash: 超时（>480s） |
| `slow` | low | c6-refactor-discount | crash | 960s / 2 步 / 工具 ops_batch、read_file |
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `eval_fail` | high | h4-dead-repo | tool | tool: 应调用指定工具之一（fetch_url、fetch_json、web_search、github_search），实际用过：ops_batch |
| `tool_error` | mid | h4-dead-repo | tool | 0 次工具调用 |


### soak05 · 2026-10-21 19:16 · 5/5 通过 · 479s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 4 轮写入成功） |


### soak06 · 2026-10-21 19:25 · 5/5 通过 · 507s

本轮无问题。


### soak07 · 2026-10-21 19:33 · 5/5 通过 · 471s

本轮无问题。


### soak08 · 2026-10-21 19:39 · 4/5 通过 · 393s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 1 次，第 3 轮写入成功） |
| `eval_fail` | high | h4-dead-repo | tool | tool: 应调用指定工具之一（fetch_url、fetch_json、web_search、github_search），实际用过：ops_batch |
| `tool_error` | mid | h4-dead-repo | tool | 0 次工具调用 |


### soak09 · 2026-10-21 19:46 · 4/5 通过 · 420s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | c6-refactor-discount | check | 0 次工具调用 |
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 1 次，第 4 轮写入成功） |
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |


### soak10 · 2026-10-21 19:53 · 5/5 通过 · 431s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 5 轮写入成功） |


### soak11 · 2026-10-21 20:01 · 5/5 通过 · 437s

本轮无问题。


### soak12 · 2026-10-21 20:09 · 5/5 通过 · 473s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 1 次，第 3 轮写入成功） |
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |


### soak13 · 2026-10-21 20:16 · 5/5 通过 · 456s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 4 轮写入成功） |


### soak14 · 2026-10-21 20:25 · 5/5 通过 · 527s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 3 次，第 3 轮写入成功） |
| `gate` | low | h4-dead-repo | must_not | 闸门 2 轮 |


### soak15 · 2026-10-21 20:33 · 4/5 通过 · 477s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |


### soak16 · 2026-10-21 20:41 · 5/5 通过 · 447s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | f5-git-search-rea | exact | 0 次工具调用 |
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 4 轮写入成功） |


### soak17 · 2026-10-21 20:49 · 4/5 通过 · 527s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | h4-dead-repo | fuzzy | fuzzy: 关键词组命中 0/1（要求 ≥1.0），未命中组：[['无法', '不存在', '没有', '失败', '未找到', '打不开', '不可用', 404]] |
| `gate` | low | h4-dead-repo | fuzzy | 闸门 4 轮 |


### soak18 · 2026-10-21 20:57 · 5/5 通过 · 439s

本轮无问题。


### soak19 · 2026-10-21 21:04 · 5/5 通过 · 446s

本轮无问题。


### soak20 · 2026-10-21 21:12 · 5/5 通过 · 453s

本轮无问题。


### soak21 · 2026-10-21 21:20 · 5/5 通过 · 470s

本轮无问题。


### soak22 · 2026-10-21 21:28 · 5/5 通过 · 493s

本轮无问题。


### soak23 · 2026-10-21 21:36 · 5/5 通过 · 462s

本轮无问题。


### soak24 · 2026-10-21 21:44 · 5/5 通过 · 504s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 1 次，第 3 轮写入成功） |


### soak25 · 2026-10-21 21:52 · 5/5 通过 · 479s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `gate` | low | f5-git-search-rea | exact | 闸门 1 轮 |


### soak26 · 2026-10-21 22:01 · 5/5 通过 · 524s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 3 次，第 3 轮写入成功） |


### soak27 · 2026-10-21 22:09 · 5/5 通过 · 475s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `tool_error` | mid | w5-long-notes-outline | check | 0 次工具调用 |


### soak28 · 2026-10-21 22:17 · 5/5 通过 · 474s

本轮无问题。


### soak29 · 2026-10-21 22:26 · 5/5 通过 · 510s

本轮无问题。


### soak30 · 2026-10-21 22:34 · 5/5 通过 · 527s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `gate` | low | h4-dead-repo | must_not | 闸门 1 轮 |


### soak31 · 2026-10-21 22:43 · 5/5 通过 · 498s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 1 次，第 3 轮写入成功） |
