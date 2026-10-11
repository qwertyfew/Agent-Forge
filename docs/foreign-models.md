# 国外（非中国厂商）本地模型调研

> 调研时间：2026-10-11 ｜ 目标硬件：RTX 5060 Ti **16GB** + Windows ｜ 用途：本地 Agent（需**工具调用**）+ 编码 + 推理
> **本报告只做调研，未下载任何模型。**

---

## 0. 先说一个容易被忽略的事实

你现在的 `gemma4:12b` **本身就是国外模型**——Google 出品（Gemma 系列），
不是国产。你的池子里真正的国产品牌只有 `qwen3.5:4b`（阿里）。

所以「只有国产模型」这个判断，实际只对了一半。不过 Gemma 之外确实还有几个
值得看的国外选择，尤其是 OpenAI 开源权重的那款。

---

## 1. 结论速览：最值得试的 3 个

| 排序 | 模型 | 厂商 | 为什么值得试 |
|---|---|---|---|
| 🥇 | **`gpt-oss:20b`** | OpenAI（美） | 14GB 装进 16GB 卡且 **100% GPU**，实测约 **140 tok/s**；MoE 只激活 3.6B，跑得飞快；工具调用强，是当下"消费级卡上的 frontier"首选 |
| 🥈 | **`ministral-3:14b`** | Mistral AI（法） | 13GB 全 GPU，约 70 tok/s；Mistral 最新一代小模型，指令遵循好，欧洲厂商、Apache 2.0 友好 |
| 🥉 | **`llama3.1:8b`** | Meta（美） | 6–8GB，128K 上下文，**工具调用是 Ollama 最早官方支持的一批**，生态文档最全；轻量稳妥的兜底 |

**如果你的用途偏"自己写代码"**，把 🥈 换成 **`devstral:24b`**（Mistral，
agent-first 训练的编码模型），但见下方 §3 的显存警告。

---

## 2. 16GB 显存能跑的国外模型全表

体积均为 Q4_K_M 量化；「GPU 占比」是在 16GB 卡上的实测/推算。

| 模型 | 拉取名 | 厂商 | 体积 | 原生上下文 | 工具调用 | 16GB 可行性 |
|---|---|---|---|---|---|---|
| **gpt-oss 20B** | `gpt-oss:20b` | OpenAI | 14GB | 128K | ✅ 强 | ✅ 100% GPU，~140 tok/s |
| **Ministral 3 14B** | `ministral-3:14b` | Mistral AI | 13GB | 未确认 | ✅ | ✅ 100% GPU，~70 tok/s |
| **Mistral Small 3.2 24B** | `mistral-small3.2:24b` | Mistral AI | 15GB | 128K | ✅ 原生 | ⚠️ 19GB → **18%/82% CPU 分割**，掉到 ~18 tok/s |
| **Devstral Small 2 24B** | `devstral-small-2:24b` | Mistral AI | 15GB | 128K~256K | ✅ agent 专用 | ⚠️ 同上，且 KV cache 几乎没有余量 |
| **Magistral 24B**（推理向） | `magistral:24b` | Mistral AI | 14GB | 128K | 未确认 | ⚠️ 偏紧 |
| **Gemma 3 12B** | `gemma3:12b` | Google | 8.1GB | 128K | 一般 | ✅ 全 GPU（你已有 4 代 12B，不必重复） |
| **Gemma 3 27B** | `gemma3:27b` | Google | 17GB | 128K | 一般 | ❌ 略超 16GB |
| **Llama 3.1 8B** | `llama3.1:8b` | Meta | 6–8GB | 128K | ✅ 原生 | ✅ 全 GPU，余量充足 |
| **Llama 3.3 70B** | `llama3.3:70b` | Meta | 43GB | 128K | ✅ | ❌ 需 40GB+ |
| **Llama 4 Scout** | `llama4:16x17b` | Meta | 67GB | 10M | ✅ | ❌ 全部专家都要装载 |
| **Phi-4 14B** | `phi4:14b` | Microsoft | 9.1GB | 16K | 弱 | ✅ 全 GPU（注意上下文只有 16K） |
| **Mistral Nemo 12B** | `mistral-nemo:12b` | Mistral AI | ~7GB | 128K | ✅ | ✅ 12GB 卡都够 |
| **Codestral 22B** | `codestral:22b` | Mistral AI | 13GB | 32K | FIM 补全 | ✅ 偏紧；适合 IDE 补全而非 agent |
| **Command-R 35B** | `command-r:35b` | Cohere（加） | ~20GB+ | 128K | ✅ 多步强 | ❌ 超 16GB（多步工具调用+RAG 是它的强项） |
| **Granite 4 3B** | `granite4:3b` | IBM（美） | 2.1GB | 128K | ✅ | ✅ 极小，可做分类/路由等轻活 |
| **Llama-3-Groq Tool-Use 8B** | `llama3-groq-tool-use:8b` | Groq | ~5GB | — | ✅ BFCL 89% | ✅ 专为工具调用微调，格式最稳 |

---

## 3. 几条必须说清楚的取舍

**① 24B 那一档在 16GB 上是"看得见吃不着"。**
`mistral-small3.2:24b` 和 `devstral:24b` 纸面都很强（后者 SWE-bench Verified
约 68%，是 16GB 级别里最高的 agentic 编码分），但它们实际占 ~19GB，
在 16GB 卡上会被迫 **18%/82% CPU/GPU 分割**，速度从 50+ 掉到 **约 18 tok/s**。
你这个 256K 窗口的 gemma4 已经占了 13.8GB —— 同时常驻会打架。

**② 你已有的 `gemma4:12b` 其实排位很高。**
有评测把它列为「16GB 档最佳 agent 倾向选择」，理由是原生工具调用 + 输出格式
稳定 + 128K 上下文。真正能全面压过它的，主要是 `gpt-oss:20b`（速度与推理）
和 24B 档（但那档在你卡上跑不快）。

**③ 中文能力是要付代价的。**
这是国外模型最现实的短板。综合多方评测：
- Gemma 4：**优秀**（140+ 语言，多语言是它的招牌）
- Llama 3.x / Gemma 3：**中等偏良好**
- Mistral / Phi / Command-R：**中等或偏弱**

也就是说，从国产模型换到国外模型，中文对话与中文写作的体验**大概率会下降**。
如果你大量用中文写长文，建议**保留 gemma4 作为中文主力**，把国外模型放在
编码、英文资料、速度敏感的场景。

---

## 4. 如果只试一个

```bash
ollama pull gpt-oss:20b
```

理由：唯一一个"既明显强于现状、又能在你这张 16GB 卡上满速跑"的国外模型。
MoE 架构让它以 3.6B 的算力成本生成，实测约 140 tok/s（比 gemma4 的 ~84 更快），
且原生工具调用能力强，适合作为 agent 的第二主力。

想再稳一手就先拉 `llama3.1:8b`（6–8GB）试试水，成本最低。

> 拉下来之后**不用手改配置**：AgentForge 现在会在发现新模型时自动探测它的
> 原生上下文长度并写好配置，你在设置页勾选即可用。

---

## 5. 信息可靠性说明

- 本报告基于 2026-10 的公开评测与厂商资料整理（含 ollama.com/library 的
  tag/体积/上下文），**未在本机实测**（按你的要求没有下载）。
- 标「未确认」的字段表示公开资料中没有可信来源，未做猜测填充。
- 实际体积、上下文与速度会随量化档位、Ollama 版本、KV cache 设置而变；
  拉取前建议在 ollama.com/library 上核对一次当前 tag。
- 表中混有个别社区评测口径的数据（如 SWE-bench Verified 分数），
  不同来源的测试脚手架不同，**只宜横向比较量级，不宜当精确值用**。
