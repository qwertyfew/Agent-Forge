# 浸泡台账（p19，3 轮）

> 只记不改：每条问题都带规则、级别、层与证据，要不要修、修哪层，等攒够了一起裁定。
>
> 规则口径见 `tools/soak.py`（规则版本 v2）。规则改了以后用 `--rescan` 从 `evals/results/` 原始结果重算本文件，不靠历史记录里的旧判断。

### p1901 · 2026-10-26 00:17 · 5/5 通过 · 533s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `tool_error` | mid | c8-fix-offbyone | check | 0 次工具调用 |

### p1902 · 2026-10-26 00:25 · 4/5 通过 · 500s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `eval_fail` | high | a6-json-sum | numeric | numeric: 数值不符：期望 [{'want': 87.5, 'tol': 0.01}]，提取到 [1902.0, 6.0] |
| `tool_error` | mid | a6-json-sum | numeric | 0 次工具调用 |

### p1903 · 2026-10-26 00:31 · 3/5 通过 · 330s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `answer_only` | mid | o2-disk-plan | fuzzy | ops 类任务 1 步、零工具（verdict=pass） |
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |
| `eval_fail` | high | a6-json-sum | numeric | numeric: 数值不符：期望 [{'want': 87.5, 'tol': 0.01}]，提取到 [1903.0, 6.0] |
