# 浸泡台账（fixb，10 轮）

> 只记不改：每条问题都带规则、级别、层与证据，要不要修、修哪层，等攒够了一起裁定。
>
> 规则口径见 `tools/soak.py`（规则版本 v2）。规则改了以后用 `--rescan` 从 `evals/results/` 原始结果重算本文件，不靠历史记录里的旧判断。

### fixb01 · 2026-10-22 01:40 · 4/5 通过 · 499s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `eval_fail` | high | w5-long-notes-outline | check | check: 核验命令未过：python -c "import pathlib;p=pathlib.Path('reports/outline.md');t=p.read_text(encoding='utf-8') if p.exists() else '';h=all(x in t for x in ['## 结论','## 待办','## 风险']);o=all(n in t for n in ['老王','小陈','李工']);print('OUTLINE-OK' if (h and o and len(t)>200) else 'BAD:'+str(h)+'/'+str(o)+'/' |

### fixb02 · 2026-10-22 01:55 · 4/5 通过 · 926s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | c6-refactor-discount | check | 0 次工具调用 |
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `write_retry` | low | w5-long-notes-outline | check | 中途写盘失败后自愈（第 4 轮失败 1 次，第 5 轮写入成功） |
| `eval_fail` | high | q1-idempotent-no-tools | crash | crash: 超时（>480s） |
| `slow` | low | q1-idempotent-no-tools | crash | 480s / 1 步 / 工具 无 |

### fixb03 · 2026-10-22 02:03 · 5/5 通过 · 466s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `tool_error` | mid | c6-refactor-discount | check | 0 次工具调用 |

### fixb04 · 2026-10-22 02:12 · 5/5 通过 · 511s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `gate` | low | f5-git-search-rea | exact | 闸门 1 轮 |
| `write_retry` | low | c6-refactor-discount | check | 中途写盘失败后自愈（第 2 轮失败 2 次，第 2 轮写入成功） |

### fixb05 · 2026-10-22 02:28 · 4/5 通过 · 982s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `stamp:no_echo` | mid | w5-long-notes-outline | check | 答复含「未能核对到」（verdict=pass） |
| `eval_fail` | high | q1-idempotent-no-tools | crash | crash: 超时（>480s） |
| `slow` | low | q1-idempotent-no-tools | crash | 480s / 1 步 / 工具 无 |

### fixb06 · 2026-10-22 02:35 · 5/5 通过 · 392s

本轮无问题。

### fixb07 · 2026-10-22 02:42 · 5/5 通过 · 419s

本轮无问题。

### fixb08 · 2026-10-22 02:49 · 5/5 通过 · 436s

| 规则 | 级别 | 用例 | 层 | 说明 |
|---|---|---|---|---|
| `gate` | low | h4-dead-repo | must_not | 闸门 1 轮 |

### fixb09 · 2026-10-22 02:56 · 5/5 通过 · 434s

本轮无问题。

### fixb10 · 2026-10-22 03:03 · 5/5 通过 · 416s

本轮无问题。
