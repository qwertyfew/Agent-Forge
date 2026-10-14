# 评测与标尺：bench 怎么跑、怎么读、怎么用它拦住过度设计

> 第 22 步的产物。在此之前，改进靠「实测事故」驱动——能抓住已发生的问题，
> 回答不了「我改了 X，agent 到底变好没有」。这一页是标尺的说明书。

## 一、评测集（evals/tasks.yaml）

24 条任务，七大类：编码 / 文件操作 / 写作 / 分析 / 运维（各 4~5 条，
测「会不会做」）+ 诚实性 / 安全拒绝（各 2~3 条，测「做不到时说不说实话」）。
每条任务三件套：

- **prompt**：给 agent 的原始指令（中文，与真实使用同构）；
- **seed**：预置文件，runner 先写进沙盒再开跑；
- **expect**：判定条件。**判定全部走程序、零模型调用**——这是刻意的：
  用 LLM 当裁判会引入新的不确定性，「裁判准不准」本身又成了要评测的问题。

判定链六层（从松到严，第一个失败的层就是失败归因）：

| 层 | 判什么 | 归因含义 |
|---|---|---|
| `contains` | 原文一字不差包含 | 执行质量：关键内容缺失 |
| `contains_norm` | 全半角/大小写/空白归一后包含 | 同上（格式自由不算错） |
| `numbers` | 提取数字与期望比对（带容差） | 计算/统计错了 |
| `keywords` | 关键词组命中比例（同义词容错） | 写作/分析类没按要求做 |
| `must_not` | 禁词出现即 fail | 约束没守住 |
| `check` | 在沙盒里真跑命令，退出码+输出双验 | **说得好但产物是错的** |

编码/文件类任务一律带 `check`——回复再漂亮，脚本跑不出结果就是没做成。
沙盒说明：每条任务在 `evals/.run/<label>/<case-id>/` 有独立沙盒，agent 的
workspace_root 被钉在里面，不碰真实项目；权限是完全访问（沙盒即边界）。

## 二、跑法

```bash
.venv/Scripts/python.exe tools/bench_tasks.py                # 全量 24 条
.venv/Scripts/python.exe tools/bench_tasks.py -c coding      # 只跑编码类
.venv/Scripts/python.exe tools/bench_tasks.py --cases c1-fix-area,w1-summary
.venv/Scripts/python.exe tools/bench_tasks.py --label base   # 结果存 evals/results/base.json
.venv/Scripts/python.exe tools/bench_tasks.py --label b --model qwen   # 钉模型跑 B 组
.venv/Scripts/python.exe tools/bench_tasks.py --repeat 3    # 每条 3 遍，报 pass^3
```

`--repeat N` 是 tau-bench 的 pass^k 口径：同一条任务跑 N 遍、**全部通过才算
过**，并报逐条通过比例。单次通过有运气成分；pass^k 量的是稳定性——对
agent 来说这是比单次通过率更诚实的信号（Sierra 在 tau-bench 论文里的
核心发现：强模型 pass^8 掉到 25% 以下，稳定性和能力是两回事）。

耗时预期：本地模型全量一轮约 20~30 分钟（单条 30~120 秒）。跑批前确认
Ollama 在线（`curl http://localhost:11434/api/tags`）。

## 三、A/B 对比（换模型/改配置的证据）

```bash
.venv/Scripts/python.exe tools/bench_compare.py base b
```

输出三层：逐任务翻转表（修复 ← / 退化 ←，默认只列有变化的）、失败层分布对比、
平均耗时。结论判据：**只修不破才算严格更优；打平则保留更简单的配置**。

典型用法：换 dispatcher 模型前后各跑一轮；开/关 `allow_upgrade` 各跑一轮；
改 system prompt 前后各跑一轮。没有 A/B，这些决定都只是吵吵。

## 四、脚手架只减不增（用评测拦住过度设计）

mini-swe-agent 用 100 行、唯一工具 bash 拿到 SWE-bench Verified >74%
（在强模型上）。它的存在不是让我们拆掉验证闸门——本地小模型下，重闸门
有存在理由——但它是一面镜子：

> **每想给 harness 加一个功能，先用评测集问：「mini-swe-agent 会需要这个吗？
> 加了它，哪条任务的判定会从红变绿？」**

- 答得出具体任务和数字 → 加，并用 bench 钉住增益；
- 答不出，只有「感觉更稳了」→ 不加；已经加了的，列入待删清单。

同一条纪律反向也成立：**删功能前先跑一轮 bench**——删完 20 条全绿才真删。
本项目的断言哲学（删掉它测试会不会红）在这里升级成：删掉它 bench 会不会红。

## 五、加一条评测 = 加一段 YAML

判定器（`agentforge/evals.py`）不改。在 tasks.yaml 里加一段 case 即可，
注意三条：

1. `expect` 必须客观可判——你自己拿一段标准答案能判出对错；
2. 编码/文件类给 `check`；写作/分析类给 `keywords` 组并接受同义词；
3. 断言刻意宽松是特性不是缺陷：评测测「有没有按要求做」，不测文风。

改完跑 `tools/test_evals.py`（任务集合法性是它断言的一部分）。

## 六、簇覆盖对照（对权威口径，防"评测绿了实际还是差"）

用户的原始担忧成立：**评测集只测它有的簇**。评测全绿只说明"这 24 条都行"，
不说明"什么任务都行"。对照业界权威评测的簇分类，本集覆盖情况如下：

| 簇 | 权威出处 | 本集覆盖 |
|---|---|---|
| 编码（改 bug/写脚本） | SWE-bench、Aider polyglot | ✅ c1~c5 + check 层硬证据 |
| 文件/系统操作 | GAIA L1、OSWorld（思路） | ✅ f1~f4 + check |
| 工具选择：该调时调对 | BFCL Simple/Multiple/Parallel | ⚠️ 部分（隐含在各任务里，无独立簇） |
| 工具边界：没有该用的工具时**不得调用、应明说** | BFCL irrelevance detection、multi-turn Miss Func | ✅ 诚实性簇 h1~h3（不可满足变体） |
| 诚实性：缺能力/不可满足时不编造 | CAR-bench（Anthropic，不可满足变体测法） | ✅ h1~h3，must_not 禁编造话术 |
| 安全拒绝：不该做的不做 | 各家 refusal eval + CAR | ✅ s1（黑名单闸门兜底 + 表述层判定） |
| 稳定性（同任务多次全过） | tau-bench pass^k | ✅ `--repeat N`（机制），待积累数据 |
| 策略遵循（按规则约束行为） | tau-bench policy | ❌ 未覆盖（无领域规则库，暂不做） |
| 多跳检索/多源汇总 | GAIA L2/L3、HotpotQA | ❌ 未覆盖（需联网，评测沙盒离线优先） |
| 多轮对话中段纠偏 | BFCL multi-turn、tau-bench 对话 | ❌ 未覆盖（单轮任务制，多轮属对话管线） |
| 抗干扰（无关信息诱导跑偏） | GAIA 干扰项设计 | ❌ 未覆盖 |

**已知最大的缺口是多跳检索和抗干扰**——这两个簇恰恰是"查资料"类任务的高发
失败区（第 21 步的 GitHub 事故就属于抗干扰簇）。补法已明确：等评测允许联网
（或给 web 工具加录制回放层）后，加"两个来源打架时信哪个"的任务。在那之前，
评测全绿要读成"**离线五类 + 诚实性都行**"，不是"全能"。

另一个结构性提醒：评测集自己也会过拟合——加任务时只加"真实使用中真的栽过
跟头"或"权威评测证明高危"的簇，不为凑数加（脚手架只减不增，对评测集同样
适用）。
