# 语义闸门与轮次账本（rerun22 复盘）

2026-10-19。对应改动：`tools` 壳 `__getattr__`、`evidence.audit_claims`、预热模块清单、
工具参数名回传、首轮耗时拆解（`load_ms` / `prompt_eval_ms`）。

## 1. 为什么会出现「需要闸门来拦」的情况

不是单一原因，是三层叠加，而且**闸门不是第一选择**：

| 层 | 现象（rerun22 实证） | 正确解法 |
|---|---|---|
| 信息不对称 | 模型 `list_dir("agentforge/core")` 已收到「路径不存在」，仍把它写进交付物——它没有一张真实的模块表，只能凭印象猜 | 开局给**源码模块清单**（预热注入），把「猜」变成「看」 |
| 无法自检 | 模型写「AgentForge 缺乏上下文压缩」，但它无法判断这句话真假——`context.py` 它没读，读了也未必能把「Compressor」和「上下文压缩」对上 | 语义闸门：用确定性探测把「否定断言 vs 真实定义」对上，**并回传真实 `path:line`** |
| 接口摩擦 | `from tools import list_dir` 连续 7 轮 ImportError；`search_code(query=)` 参数名错又猜两轮 | 壳支持 import 形态；TypeError 回传该工具接受的参数名 |

所以答案分两半：

- **生产阶段就做对**（治本）：预热模块清单、参数名回传、import 形态。这三项直接砍掉
  rerun22 里 28 轮中的约 13 轮无效轮次。
- **闸门只做最后一道确定性兜底**（治标但必要）：模型的能力边界决定它会给错结论，
  而错的代价是「用户照着重造轮子」。闸门的判定不靠模型自省，只做「否定词 + 功能名 →
  代码里找定义行」这一件事；找到才报错，报错必带真实位置，因此修复是**一次改对**，
  不是整篇返工。

边界刻意收得很窄：找不到定义就保持沉默（不做「肯定」判断），非本仓库主语的断言不碰
（「mini-swe-agent 缺乏子代理」是对的，不能误伤）。

## 2. rerun22 的 28 轮都花在哪

任务 prompt 与 rerun11/21 同源（2347 vs 2231 字符），**不是任务变大了**：

| 类别 | 轮次 | 说明 |
|---|---:|---|
| 有效调研 / 写作 | ~12 | 读两个仓库、检索、写报告 |
| `from tools import ...` ImportError | 7 | 命中的是仓库根没有 `__init__` 的 `tools/` 测试包 |
| `find_tools` 无匹配 + 重复调用被缓存 | 4 | 第 1 次没匹配，后面 3 次吃「重复调用」缓存 |
| 引用核查拒写 → 重读重写 | 4 | 路径漏 `external/` 前缀，每次重试还要重读 5–9 个文件 |
| `search_code(query=)` 参数名错 | 1 | 30 次调用全废，只拿到 list_dir 的结果 |
| 顶层直接 `write_file` 被 PTC 守卫拒 | 1 | 正确拦截，模型改走脚本 |
| verify 两次失败触发的续跑 | 2 段（6+7 轮） | 交付物没落盘 / 缺「按性价比排序」一节 |

对照：rerun21 同样任务 10 轮跑完——差距主要来自上面 7 轮 ImportError 与 4 轮写盘返工。

## 3. 首轮到底慢在哪

rerun22 首轮：`llm_ms=23.9s`，其中 Ollama 纯生成 6.6s，剩下 **17.3s 是预填充 + 冷加载**。

harness 侧启动实测（config + registry + agent + 预热 + 系统提示拼装）合计 **99ms**——不是流程慢。
所以首轮慢只可能来自模型侧两件事，新增两个字段把它拆开：

- `load_ms`：Ollama `load_duration`（冷加载权重）
- `prompt_eval_ms`：Ollama `prompt_eval_duration`（预填充）

rerun23 直接读这两个数：**load 占大头 → 上模型预热（服务启动即加载 + keep_alive）；
prompt_eval 占大头 → 压系统提示**（当前 8250 字符，其中预热块 3100）。

上下文逐轮重发是剩下的结构性问题：单轮 prompt 从 7.5K 涨到 32.8K，单轮耗时从 13s 涨到 65s
（第 25 轮）。这是 O(n²) 的固有成本，只能靠减少轮次与压缩历史，不能靠并行。

## 4. 改动清单

| 文件 | 改动 |
|---|---|
| `agentforge/tools/ops.py` | `tools` 壳改成模块对象（PEP 562 `__getattr__`），脚本内 `import tools` / `from tools import read_file` / `tools.read_file(...)` 全部等价于 `call(...)`；只用脚本自己的 builtins 替换 `__import__`，不污染 `sys.modules` |
| `agentforge/evidence.py` | 新增 `audit_claims`：9 类能力（压缩/权限/任务/记忆/skill/子代理/会话/后台/钩子）的否定断言探测，命中原语给出首选定义行 |
| `agentforge/tools/fs.py` | `write_file` 在引用核查之后加语义核查，命中即拒写并回传真实位置 |
| `agentforge/warmup.py` | 预热块新增「源码模块清单」，按任务词相关度排序；跳过 `test_*`/`bench*` |
| `agentforge/loop.py` | 参数错误回传该工具接受的参数名；stats 增 `load_ms` / `prompt_eval_ms` |
| `agentforge/llm.py` | 解析 `prompt_eval_duration` |
| `webui.py` | stats 白名单透传两个新字段 |
| `tools/test_claims_gate.py` | 12 项断言：真矛盾要抓、真缺失/对方侧不误伤、端到端拒写与改正后落盘 |
| `tools/test_ops_batch.py` | 新增 4 项：`import tools` 属性调用、`from tools import X`、`tools.call` 别名未知属性回落、参数名回传 |

## 5. 测试

全套 36 个测试文件：32 通过，4 失败（`test_ambiguity_gate` / `test_evalsdesk` /
`test_multipath` / `test_service_ctrl`）。其中 `test_evalsdesk` 已用 `git stash` 验证
在改动前同样失败（stub 结果对象缺 `done_reason`），其余三个为已知预存失败。
