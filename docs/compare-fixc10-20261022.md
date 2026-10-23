# 两批浸泡跑对比

- 旧：`evals/results/fixb[0-9]*.json`，50 次运行
- 新：`evals/results/fixc[0-9]*.json`，50 次运行
- 口径与 `tools/event_audit.py` 完全一致（一次调用只归一类；写盘只认「系统核对」块）

## 总量

| 指标 | 旧 | 新 | 变化 |
| --- | ---: | ---: | ---: |
| 判定通过率 | 94.0% | 88.0% | -6.0pt |
| 零返工运行占比 | 92.0% | 78.0% | -14.0pt |
| 零错误运行占比 | 92.0% | 82.0% | -10.0pt |
| 错误类返工 / 次 | 0.08 | 0.20 | +0.12 |
| 冗余类返工 / 次 | 0.04 | 0.08 | +0.04 |
| 回合 中位 | 3 | 3 | +0 |
| 调用 中位 | 1 | 1 | +0 |
| 单次耗时 中位 (s) | 82 | 78 | -5% |
| 总墙钟 (min) | 91 | 82 | - |

## 分任务

| 任务 | 运行(旧/新) | 回合 中位 | 调用 中位 | 耗时 中位 (s) | 零返工 | 错误类/次 | 冗余类/次 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| f5-git-search-rea | 10/10 | 2 → 2 | 1 → 1 | 72 → 74 | 10/10 → 10/10 | 0.00 → 0.00 | 0.00 → 0.00 |
| c6-refactor-discount | 10/10 | 3 → 3 | 2 → 2 | 84 → 78 | 7/10 → 4/10 | 0.30 → 0.70 | 0.00 → 0.00 |
| w5-long-notes-outline | 10/10 | 8 → 8 | 4 → 4 | 146 → 152 | 9/10 → 5/10 | 0.10 → 0.30 | 0.20 → 0.40 |
| q1-idempotent-no-tools | 10/10 | 1 → 1 | 0 → 0 | 81 → 73 | 10/10 → 10/10 | 0.00 → 0.00 | 0.00 → 0.00 |
| h4-dead-repo | 10/10 | 2 → 2 | 1 → 1 | 64 → 66 | 10/10 → 10/10 | 0.00 → 0.00 | 0.00 → 0.00 |

## 机制触发（回包里出现即计一次运行）

| 机制 | 旧 | 新 | 含义 |
| --- | ---: | ---: | --- |
| `param_hint` | 0 | 0 | 工具回包「参数不对」——签名/参数名发错 |
| `idem_block` | 6 | 6 | 幂等层拦下逐字节相同的重写 |
| `occ_block` | 1 | 1 | OCC 拦下无版本号的整写 |
| `fail_open` | 0 | 0 | 连拦两次后放行 |
| `alias_fix` | 0 | 0 | 纯改名别名自动改对 |
| `script_exc` | 1 | 1 | PTC 脚本抛异常 |
| `noop` | 2 | 3 | 脚本执行了但没调到工具 |
| `edit_miss` | 0 | 1 | edit_file 没定位到 old_text |
| `edit_ambig` | 1 | 3 | edit_file 命中多处 |
| `unchanged` | 1 | 0 | 回执告知内容与上次相同 |
| `find_tools` | 0 | 0 | 调 find_tools 打听工具名/参数 |

## 新一轮里仍有返工的运行（11 次）

| 轮次 | 任务 | 回合 | 调用 | 判定 | 返工 |
| --- | --- | ---: | ---: | --- | --- |
| - | c6-refactor-discount | 3 | 2 | pass/check | s2 edit_miss |
| - | c6-refactor-discount | 3 | 2 | fail/check | s2 edit_miss |
| - | c6-refactor-discount | 5 | 4 | pass/check | s3 noop_script；s4 noop_script |
| - | c6-refactor-discount | 3 | 2 | pass/check | s2 edit_miss |
| - | c6-refactor-discount | 6 | 4 | pass/check | s2 noop_script |
| - | c6-refactor-discount | 3 | 2 | pass/check | s2 edit_miss |
| - | w5-long-notes-outline | 10 | 6 | pass/check | s2 noop_script |
| - | w5-long-notes-outline | 8 | 4 | pass/check | s3 write_fail；rewrite |
| - | w5-long-notes-outline | 7 | 4 | fail/check | s1 script_exc；rewrite |
| - | w5-long-notes-outline | 10 | 6 | pass/check | rewrite |
| - | w5-long-notes-outline | 10 | 6 | fail/check | rewrite |
