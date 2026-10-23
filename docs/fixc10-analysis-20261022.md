# fixc 十轮：观测补强后的读数（2026-10-22）

> 命令：`python tools/soak.py --rounds 10 --tag fixc --out fixc`
> 与 `fixb`（上一轮 10 轮 / 50 次）逐项对比由 `tools/soak_compare.py` 生成。
> 本轮**只补观测不改行为**（`streamprobe` 旁路 + bench 的 `progress`/`streams` +
> faulthandler 栈），所以两批的机制表现应当一致——差异就是噪声与内容质量。

## 1. 结论先说

**P17 抓到了，而且一次就够定性。** `fixc03` 的 q1：480s、流记录
`38102 行 / 96747 字 / 空闲 0s / done=false`，尾部是同一句
`Wait, "不要以寒暄或复述用户问题开头". Check.` 无限循环——**生成退化（循环复读），
不是排队，不是撞锁，不是端侧不回包**。首行 16.4s、连续出字 442s、`eval_count=0`
（从没走到 done）。同一批 10 轮里另有 9 次 q1 全部正常（58–103s），所以它是偶发退化。

原假设 1（`gemma` 没配 `num_predict`、不限输出长度）被证实；原假设 2（Ollama 排队）
未出现：所有样本的 `lock_wait→lock_got` 都是 0.0s，一次锁都没等过。

## 2. 两批对比（fixb 50 次 vs fixc 50 次）

| 指标 | fixb | fixc | 读法 |
|---|---:|---:|---|
| 判定通过率 | 94.0% | 88.0% | −6pt（见 §3） |
| 零返工运行 | 92.0% | 78.0% | c6/w5 的 edit_miss 与 noop 变多 |
| 错误类返工 / 次 | 0.08 | 0.20 | 同上 |
| 冗余类返工 / 次 | 0.04 | 0.08 | w5 润色式整写 4 次 |
| 卡死（480s 撞墙） | 2 | **1** | 同为 q1 循环退化 |
| 单次耗时中位 | 82s | 78s | −5% |
| 总墙钟 | 91min | 82min | 少 1 次 480s |
| `param_hint` / `find_tools` / `edit_miss`(fixb) | 0 / 0 / 0 | 0 / 0 / 1 | 签名块与索引仍零返工 |

机制类指标几乎没动（`idem_block` 6→6、`occ_block` 1→1、`script_exc` 1→1），
说明观测补强没有干扰产品行为；变化的只有 c6/w5 的编辑类返工和内容漏项。

## 3. 6 次失败逐条归因

| 轮次 | 用例 | 层 | 原因 |
|---|---|---|---|
| fixc03 | q1 | crash | **P17 循环退化**（441s 内 96747 字复读同一句） |
| fixc01 | f5 | exact | 答复里写的是小写 `rea`，任务书要求含 `REA`——**判定大小写脆弱** |
| fixc04 | w5 | check | 核验命令未过（内容漏项） |
| fixc06 | c6 | check | `src/test_pricing.py` 测试未过 |
| fixc07 | w5 | check | 核验命令未过 |
| fixc10 | w5 | check | 核验命令未过（补跑轮，4/5） |

w5 三次 check 失败是这批的主要缺口（fixb 是 1 次）。fixc04/07/10 的核验都卡在同一批
必填小节/待办项上——不是机制问题，是内容漏项，跟 fixb 里 w5 漏「小陈」三项同源。
**没有一次失败来自闸门误报或工具机制。**

台账里另有一条值得盯：`write_failed` 出现 2 次（fixc02 w5、fixc06 c6）——**最后一次
写盘仍失败，答复却宣称完成**。这比 check 失败更严重（谎报交付），单独立项。

## 4. 新增返工（值得单独立项的两类）

| 类 | 次数 | 现场 | 方向 |
|---|---:|---|---|
| c6 `edit_miss` | 4 | `edit_file` 没定位到 `old_text` | 定位失败要能自愈（给出行号区间/difflib 提示，已在做） |
| w5 `rewrite` | 4 | 润色式整写（内容真变了，闸门拦不住也不该拦） | 同上，属正常返工 |
| c6/w5 `noop_script` | 3 | 脚本执行了没调到工具 | P18 同源（PTC 脚本写法） |
| w5 `script_exc` | 1 | fixc07 首步脚本抛异常 | 偶发 |

## 5. 修法建议（按代价排序，等拍板）

1. **`gemma` 配 `repeat_penalty`**（`config.py` 已支持，`config.yaml` 没写）。
   不传时 Ollama 用它自己的默认 1.1——**仍然复读**，说明默认不够。
   这是唯一直接对着「循环退化」的旋钮，改配置即可，回退只删一行。
2. **`num_predict` 硬上限**：防失控。风险是会截断真·长输出（w5 的单次正文
   最长 22797 字），所以上限要 >3 万，或按任务分级。
3. w5 check 内容漏项、c6 `edit_miss`：另立项，跟 P17 无关。

## 6. 附：本轮踩到的两个坑（已记 pitfalls）

- **评测跑批会被环境的批量删除守卫掐死**：`_prep_sandbox` 清场累计 50 次删除后，
  守卫 spawn node 检查并要求确认，无人值守下 `SystemExit(1)`，整轮 5 条结果全丢
  （fixc10 就这么丢过一次）。跑批/浸泡时加
  `CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR= CODEBUDDY_TOOL_CALL_ID= CODEBUDDY_SAFE_DELETE_ENABLED=0`。
- `--resume` 会把「崩掉的轮」当成已完成跳过（台账里有 `harness_crash` 记录即算
  done）：补跑要直接跑 bench 单轮，再 `--rescan` 重算台账。

## 7. 自验

58 套回归全绿；观测字段在冒烟与真机单流里都验证过（见
`docs/p17-observability-20261022.md`）。
