# AgentForge

本机运行的 AI Agent Harness：用 Ollama 上的本地模型（可选接入付费 API）执行真实任务——
读写文件、跑命令、联网检索、SSH 远程运维、写长篇小说，甚至给自己写新工具和 Skill。

前端是零依赖的单文件页面（`web/index.html`），后端是纯标准库的 HTTP 服务（`webui.py`），
没有重框架、没有构建步骤，`git clone` 后装三个依赖就能跑。

## 特性

- **多模型与自动分配**：为每个模型打八维度能力分，任务按维度贪心挑选最优组合（可限定只用本地模型）。
- **本地优先**：默认全部走 Ollama 本地模型，零 API 费用；付费模型（DeepSeek / SiliconFlow）按需勾选启用。
- **任务台账**：每个任务记录状态、耗时、token 与预估费用、产出文件、对话历史，重启后自动把遗留的
  「执行中」标记为已中断。
- **产出追踪**：写文件工具精确记录 + 任务前后工作区快照对比，任务产出了哪些文件一目了然。
- **长期记忆**：任务收尾自动沉淀记忆（规则抽取 + 本地模型提炼，带同义去重），后续任务自动注入；
  侧栏「记忆管理」可查看、补充、删除。
- **协作式停止**：每轮推理与每次工具调用前检查停止标志，随时可中断长任务。
- **Skill 与工具热加载**：Agent Skills 开放标准，命中描述即自动注入；Agent 可自行编写并安装新工具/Skill。
- **上下文节流**：工具 schema 按需下发（核心 + 关键词命中，其余只列名字，`find_tools` 现取用法）；
  Skill 自动注入只给开头几节；多步接力只传「计划 + 各步摘要 + 产出文件」。35 个工具全量下发约
  4.3K tokens/轮，按需下发后常规轮次约 1.7K，多步任务第 N 步不再背负前 N−1 步的工具输出。
- **权限**：默认权限（写文件与执行命令需确认）与完全访问权限可按任务切换，偏好持久化。
- **内置工具**：文件读写、命令执行、网页抓取与截图、SSH 远程运维、git、记忆、小说写作、自我扩展等。

## 安装

需要 Python 3.10+ 与本机运行中的 [Ollama](https://ollama.com/)（默认 `http://localhost:11434`）。

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
# Linux / macOS
.venv/bin/pip install -r requirements.txt
```

## 运行

```bash
# 交互式命令行
python run.py

# 单任务
python run.py -t "在工作区里新建 hello.py 并运行它"

# Web 界面（默认 http://127.0.0.1:8787）
python run.py --web
python run.py -c config.lanrun.yaml --web --port 8787   # 局域网访问
```

Windows 下可直接双击 `start-web.bat` 后台起服务，日志写入 `.run/web.log`。

## 配置

`config.yaml` 是主配置，`config.lanrun.yaml` 是局域网/长时间任务的版本（放宽了推理轮数与工具输出长度）。

```yaml
ollama:
  host: http://localhost:11434
models:
  gemma:            # 模型 key，任务分配时按 key 引用
    name: gemma4:12b
    num_ctx: 32768
    think: true     # 必须为 true，否则推理过程会混进正文
workspace:
  root: D:\path\to\your\workspace   # Agent 可访问的根目录，越界读写一律拒绝
```

以下文件**不入库**（含密钥或机器相关状态）：`models.json`（模型清单与 API Key）、`.tasks.json`、
`.prefs.json`、`.agentforge/`（工作区记忆）、`.sessions/`、`.venv/`、`.run/`。

## 目录结构

```
run.py                 入口（交互 / 单任务 / Web）
webui.py               Web 后端：SSE 事件流 + /api/* 接口
web/index.html         全部前端（零依赖单文件）
agentforge/
  loop.py              单轮智能体循环（推理 → 工具 → 回灌）
  orchestrator.py      任务编排：拆解、分配、限步、停止
  providers.py         模型 Provider 抽象（OpenAI 兼容 / Anthropic / Ollama）
  router.py scores.py  维度路由与评分
  planner.py           任务拆解与步骤分配
  skills.py            技能发现与自动注入
  memory.py autonote.py 长期记忆与自动沉淀
  artifact.py          产出文件追踪（快照对比）
  taskstore.py         任务台账
  pricing.py           费用估算
  tools/               内置工具集
skills/                内置技能（frontend-design / novel-writing / web-browsing / remote-ops ...）
```
