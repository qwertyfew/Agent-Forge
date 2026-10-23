# 三件「和模型无关」的事怎么修（2026-10-22）

用户三个追问，逐条给取证、成熟项目对照、落到代码的判断：

1. 「写对了还写」没有任何机制在管 → **加机制**（幂等写 + 乐观并发 + fail-open）；
2. 「脚本不校验参数名」→ **校验早就有了**，缺的是「把错名字就地改对」；
   加别名表 + 就近提示，成本微秒级，收益只覆盖 20 次里的 3 次；
3. 「增加工具签名和用途」→ **加了**，索引从 401 字符到 1594 字符，每轮多 ~250 token。

真正的答案不是三个都「加」，而是三个都先量清楚：哪一层能省掉一次模型调用，
哪一层只能让报错更好看。**能省调用的才值得做。**

---

## 1. 写对了还写：机制怎么加

### 先确认问题本身

31 轮 w5（长文提纲）里，`write_file` 对同一个最终文件 `reports/outline.md`：

- 81 次整写落盘，50 次被后一次覆盖（前 N-1 次等于白生成）；
- 其中约 **20 次是逐字节相同**的重写——回执本来就印着「内容未变」，模型照写；
- 分布：整写 1 次的有 8 轮，2 次 2 轮，3 次 16 轮，4 次 4 轮，5 次 1 轮。

也就是说：**23/31 轮不止一次整写，21/31 轮 ≥3 次**，而每次都是一整篇的生成成本。

### 成熟项目怎么处理「同一个文件被反复重写」

| 做法 | 出处 | 规则 | 对我们的适配 |
|---|---|---|---|
| 创建与修改是两个动词，`create` 不许覆盖 | Anthropic `str_replace_editor`（SWE-bench 那套） | 「The `create` command cannot be used if the specified `path` already exists as a file」 | 我们用不了这么硬：整写仍要允许「推到重来」式的大改 |
| 改内容只认唯一匹配的字符串替换 | 同上 | `old_str` 必须**恰好匹配一处**，多了少了都报错重试 | 已经是我们的 `edit_file` |
| 内容寻址 + 幂等提交 | git | 内容相同的 blob 不产生新对象；工作区没变化就是「nothing to commit」 | **直接照搬**：逐字节相同 → 不落盘、不建版本 |
| 乐观并发（If-Match / if_version） | RFC 9110 `If-Match`；markland 的 `if_version`；AWS LLM-Wiki 的 `base_version + If-Match` | 每次更新必须声明「我基于第几版改」，版本对不上就拒，让调用方重新读 | **照搬**：整写要带 `base_version` |
| 不整篇重写，只做窄块编辑 | Doco 六步循环 | 「Agents should never rewrite whole documents; narrow block edits with version checks reduce blast radius」 | 我们的 `fill_section` 就是这个，问题是模型不选它 |

四条里最贵的教训是第一条的反面：**光有「create 不能覆盖」还不够，得让「再写一次」这件事本身
在语义上没有意义**。所以机制分三层，从「零风险」到「有代价」。

### 落地的三层

| 层 | 触发 | 行为 | 风险 |
|---|---|---|---|
| ① 幂等写 | 新内容与磁盘**逐字节相同** | 不落盘、不建版本，回执：「已是最新内容……不要再拿相同内容调一次」 | **零**（同样的内容写不写都一样） |
| ② 乐观并发 | 同一文件**第 3 次**整写 | 必须带 `base_version=N`（回执里印的那个数），否则拒收并给出确切写法 | 低：真要大改，补一个数字即可 |
| ③ fail-open | 同一路径被 ② 连拦 **2 次** | 放行，回执注明「本次放行，但重复生成不产生新信息」 | 零：闸门只该把「重复生成」换成「改几行」，不该卡死交付 |

为什么定「第 3 次」：第 1 次是首稿，第 2 次是「想清楚了大改」，都该放行；第 3 次起
几乎都是措辞润色——w5 的实测序列是 471→471→478 字符，第一版就已经三段齐全。

代码位置：

- `agentforge/tools/fs.py`：`write_file(path, content, base_version=None)`，`_OCC_AFTER = 2`；
- `agentforge/record.py`：`whole_writes()` / `note_whole_write()`（**只数 write_file**）；
- `agentforge/writepolicy.py`：`note_occ_block()` / `clear_occ()`（fail-open 计数）；
- `write_file` 的 schema 增加可选 `base_version`（integer）。

### 一个真实的误判，和它换来的断言

第一版用「版本号」当整写次数，`occprobe` 里 w5 立刻被误伤：它写了 1 次整写、填了 3 节
`fill_section`，版本号已经到 4，于是**第 2 次整写就被拦了**——

```
- s=2 write_file  reports/outline.md 已写入磁盘
- s=4 fill_section ×3
- s=5 write_file  [OCC拦] 缺 base_version
```

`fill_section` 是增量填充（一节一行），不是重写。改成只数整写后复跑（`occprobe2`）：
w5 一次整写通过，第二次整写被**幂等层**拦下（内容一致），141s 通过，没再有误伤。

这条已写成断言：`test_write_once.py::骨架/分节填充不计入整写次数`。

---

## 2. 参数校验：能不能加，划不划算

### 先纠正一个前提

「脚本不校验参数名」不成立——**校验一直在**，而且必然发生：

```
模型脚本 → ops.py 的 sdk_call() → Agent._execute() → tool.run(**kwargs) → TypeError
```

桥（`sdk_call`）只校验「工具名存在」「权限够不够」，kwargs 原样透传，
**Python 的参数绑定就是校验点**。`_execute` 已经捕获 `TypeError` 并把该工具接受的
参数名整串回给模型（`agentforge/loop.py`）。所以：

- **加校验的时间成本 ≈ 0**：一次 `dict` 查找，微秒级，可以忽略；
- 但它**救不回那一轮**：错误已经发生，模型必须再花一次生成才能改对。

31 轮里的 20 次参数错：`edit_file.content=` 12 次、`edit_file.old_content=` 3 次、
缺 `new_text` 1 次、`github_search.type=` 4 次。

### 真正省时间的是「纯改名就地改对」

| 错法 | 次数 | 能不能自动改 | 理由 |
|---|---:|---|---|
| `old_content=` / `new_content=` | 3 | ✅ 改成 `old_text` / `new_text` | 同一件事的两种叫法，改过去行为不变 |
| `old_string=` / `new_string=` / `file_path=` | 0（本批） | ✅ 同上 | 各家 SDK 的常见写法 |
| `edit_file(content=)` | 12 | ❌ | `content` 是「整篇内容」；塞给 `new_text` 会把整篇文档当成一处替换——**语义不同就是不许猜** |
| `github_search(type=)` | 4 | ❌ | `type` 是 GitHub API 的概念，不是 `sort` 的同义词 |

这与 Anthropic 那条「`old_str` 必须唯一匹配、否则报错重试」同源：**能确定的才替它做，
不能确定的必须让它看见**。所以：

- `ops.py` 增加 `_KW_RENAMES`：只收纯改名，命中后在脚本里**当场改对**，并打印
  「[参数] 已按同名参数处理：new_content → new_text」（不静默改写，模型才知道真名）；
- `loop.py` 的 `TypeError` 分支增加 `difflib.get_close_matches`：把错名指向最近的
  正确参数；`content` 单独给一句「整篇替换用 write_file，局部替换用 edit_file」。

**划算吗**：校验本身免费，别名只能救 3/20（约 0.5% 墙钟）。剩下 12 次靠的是把签名
摊在眼前（第 3 节），以及这条错误信息本身足够照着改——`test_write_once` 里
「报错指向最接近的参数并给两条出路」就是锁这个。

---

## 3. 工具签名和用途：加到索引里

### 依据

上一轮查到的阈值：工具定义占上下文 **<10%** 时，全量下发比「检索再取」更划算
（Claude Code SDK 的默认行为）；工具数超过 30~50 才明显掉选择准确率（Anthropic）；
Anthropic 官方也说「少于约 10 个工具时全量下发更快」。

我们 43 个工具、全量 schema ≈ 4100 token、系统提示 7660 字符。**全量仍然太贵，
但只给名字又太薄**：31 轮里 14 次 `find_tools("github_search")` 就是为了问「它要什么参数」，
而索引就在同一份提示词里——信息本该一步到位。

### 做法

`toolselect.index_text()` 从「按前缀分组的一串名字」改成一行一个：

```
- git_commit(message) — 提交暂存区的改动
- fill_section(path, heading, content) — 把一段正文填进骨架里的某一个 `## 小节`
- fetch_json(url) — 抓取 JSON 接口并返回真实数据
```

- 只写**必填参数**（可选参数交给 `find_tools`），用途取描述首句并压到 26 字内；
- 每行硬上限 62 字符（`- ` + 签名 + ` — ` + 用途），参数名长的工具（`ssh_upload`）
  自动压缩用途——索引是每轮重贴的固定开销；
- PTC 模式下，签名块里已摊开的 10 个热工具**不再在索引里重复**；
- 索引 401 → 1594 字符（约 +250 token/轮），换来的是不再为一个参数名多跑一跳。

顺带把两个头部说明改了口径：原来是「需要哪个就先 find_tools 取它的参数」，
现在参数就在下面，改成「括号里是必填参数，照抄即可；细节才去 find_tools」。

---

## 4. 验证

| 项 | 结果 |
|---|---|
| 回归 | 57 套 Python 全绿（新增 `tools/test_write_once.py` 24 条断言） |
| `occprobe` | c6 65s / w5 165s 通过；暴露 OCC 误判（版本号 ≠ 整写次数） |
| 修正后 `occprobe2` | w5 **141s** 通过（8 回合）：一次整写 + 第二次被幂等层拦下 |
| 提示词体积 | 系统提示 7660 字符（索引段 1332 字符，签名块 726 字符） |

## 5. 还没做的

- **`edit_file` 定位失败**（`old_text` 与文件不逐字节相等，6 次）：本次没动。方向是
  「按行号区间改」或失败时用 difflib 回「最接近的是第 1-10 行」，属于另一次改动。
- **P2 的语义漏洞**（把 REA 编成「Research and Evaluation Architecture」）：仍然只能靠
  语义判断，闸门抓不到。
- 三层机制都只在**一个任务内**生效（记录器按任务建）；跨任务重复整写不受约——这是
  刻意的，否则换任务写同一个文件会被上一单的计数误伤。
