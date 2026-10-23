# P17 观测补强：让被墙钟砍掉的那一次留下证词

> 2026-10-22。对象是 10 轮复跑（`fixb`）里 2/50 的 q1：`duration` 正好 480.0s、
> `reply` 为空、过程记录 0 条。上一次的结论是「修不了，因为被砍掉的路径什么都没留下」。
> 这次先把观测补上，再谈修法。

## 1. 为什么以前一条证据都没有

不是模型没出字，是**出了字没人记账**。两个原因叠在一起：

1. `OllamaClient.chat_stream` 把整轮增量**缓冲**在 `emitted` 里，确认整轮成功才放给
   调用方（重试时不重复投递半截内容）。生成过程中上层一个 `delta` 都收不到。
2. `tools/bench_tasks.py` 只在 for 循环体里做墙钟判断，而循环体只在**收到事件**时
   才转一圈。没有事件 = 没有检查点。

结果就是：卡住时 `process` 里只有一条事后补的 `{"k": "error", "msg": "超时（>480s）"}`。
没有 token 数、没有最后一段输出、没有「最后一次收到字节是几秒前」。于是只能猜——
而猜出来的修法（限输出长度？排队？）会直接改掉产品行为，等于拿线上做实验。

## 2. 补了什么（都不改行为）

| 位置 | 补的东西 | 为什么在这一层 |
|---|---|---|
| `agentforge/streamprobe.py`（新） | 旁路记账：每个活跃流一条记录 | 生成器内部才看得见字节，模块级才活得过「被砍掉」 |
| `OllamaClient.chat_stream` | `begin/phase/tick/finish` 埋点 | 本地模型的唯一入口，能记「等本地串行锁」这一段 |
| `ClientHub.chat_stream` | 在线模型套 `_observed` 转发层 | 三家的客户端不一样，Hub 是统一出口 |
| `tools/bench_tasks.py` | `progress`（事件计数/字数/静默时长/尾部）+ 超时时读 `streams` + `faulthandler` 栈 | 结果文件是唯一留得下来的地方 |

### 2.1 记录里有什么

```
model / kind / attempt     谁在生成、第几次尝试
started / age_s            从开流到现在多久
first_line_s               首字节延迟（预填充/冷加载都算在这里）
last_line_s / idle_s       最后一行是几秒前 —— 区分「慢」与「不回包」
lines / chars              收到多少 —— 区分「出字了没出完」与「一个字都没有」
phases{lock_wait,lock_got,resp}   等锁多久、什么时候连上
tail                       最后 280 字 —— 看得出是在正常收尾还是在重复同一段
done / done_reason / eval_count   完成情况（length = 预算烧完被截断）
error                      异常类型与文案（GenerationStopped / ModelIdleTimeout…）
```

四个阶段时间点里最要紧的是 `lock_wait` → `lock_got`：16GB 显存上两条泳道撞
本地串行锁时，等待能吃掉几百秒而 `chars` 一直是 0。这一条以前完全看不见。

### 2.2 超时文案现在自带现场

以前：

```
超时（>480s）
```

现在：

```
超时（>480s）：gemma4:12b#1 已连 471s 812行/21430字 空闲3s
```

一眼可分三类：

- `等锁 470s 0行/0字` → 不是模型的问题，是并发撞锁；
- `已连 471s 812行/21430字 空闲3s` → 一直在出字，生成退化（该配 `num_predict`）；
- `已连 470s 0行/0字 空闲470s` → 端侧不回包（该查 idle 上限，而不是输出长度）。

### 2.3 还有一层兜底：栈

`faulthandler.dump_traceback_later(timeout + 10)` 用独立线程在超墙钟后把**所有线程**
的栈写进 `evals/.run/<label>/<case>.stack.txt`。主线程卡在 `next()` 里时自己 dump
不出栈，这是唯一能看到「卡在哪一行」的办法。正常结束就取消，没写过东西的空文件
当场删掉（不让沙盒/目录里留垃圾）。

## 3. 自验

| 项 | 结果 |
|---|---|
| `tools/test_streamprobe.py`（新，8 组 26 条） | 全绿 |
| 全套 Python 套件（58 个） | 全绿 |
| 真机单流（gemma4:12b，80 token） | `70行/269字`、`first_line_s=7.1`、`done_reason=length` |
| 冒烟两条（q1 / h4） | 2/2 通过，`progress` 已落盘 |
| 服务重启 | 8787 health=200 |

真机那一条顺手验证了另一件事：`num_predict=80` 时 `done_reason=length`、
`eval_count=80`，正文一个字都没出（全烧在 thinking 里）——说明「预算被思考通道吃光」
在本机是常态，这跟 P17 的「不限输出长度」假设是同一根线。

## 4. 读数：十轮（fixc）

见 `docs/fixc10-analysis-20261022.md`。
