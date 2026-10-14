# 容器化部署准备（云服务器上跑 agent-forge）

本文档说明 agent-forge 的容器化部署**已经准备好的东西**、上服务器时要做的
几步、**本地 / 云端的判定规则**，以及哪些部分在本机（无 Docker）验证过、
哪些要首次部署时确认。

## 一、已准备好的文件

| 文件 | 作用 |
|---|---|
| `Dockerfile` | python:3.11-slim + 阿里云源 + pip 装依赖（requests/PyYAML/rich，全有预编译 wheel，无需编译环境）；依赖层与代码层分离，改代码不重装依赖 |
| `docker-compose.yml` | app 服务 + 可选 ollama 服务（`--profile ollama` 门控）；healthcheck、restart、数据卷 |
| `.dockerignore` | 机密（models.json）与本机状态绝不进镜像 |
| `.env.example` | 三个环境变量的说明（Ollama 上游 / 自动拉起 / 对外端口） |

配套的代码改动（都已进测试回归）：

1. `config.py`：`AGENTFORGE_OLLAMA_HOST` / `AGENTFORGE_OLLAMA_AUTO_START`
   环境变量优先于 config.yaml——镜像里的配置不动，换上游只改 .env；
2. `run.py`：新增 `--host` 参数（默认仍 127.0.0.1，容器里用 0.0.0.0）；
3. `webui.py`：`AGENTFORGE_DATA_DIR` 可把任务台账/偏好指到持久卷，
   默认仍是项目根（本机行为不变）；
4. `webui.py`：`/api/health` 返回 `local` 字段 + `/api/service/restart`
   重启接口——驱动侧栏服务按钮的本地 / 云端自适应（见第四节）。

## 二、容器里的数据 layout

| 容器路径 | 宿主机（./data 下） | 内容 |
|---|---|---|
| `/app/workspace` | `data/workspace` | 智能体工作区：它读写的所有文件 + `.agentforge`（工作区记忆、任务笔记） |
| `/app/data-state` | `data/state` | `.tasks.json`（任务台账）、`.prefs.json`（权限、悬停延迟等偏好） |
| `/app/models.json` | `./models.json` | 模型池（含 API Key）。**挂 rw**：UI 里的模型管理会写回 |
| `/app/skills/learned` | 命名卷 `learned-skills` | 自动沉淀的技能；一手技能（first-party-source 等）留在镜像层 |
| `/root/.ollama`（ollama 服务） | `data/ollama` | 模型权重（用 ollama profile 时） |

备份 = 拷走 `./data` 和 `./models.json`。

## 三、上云服务器的步骤

```bash
# 0. 前置：装好 Docker（含 compose 插件）；用 GPU 跑 Ollama 时另装
#    nvidia-container-toolkit
git clone <你的仓库> && cd Agent-Forge

# 1. 模型池：从模板复制并填 Key（绝不要把填好的 models.json 提交进 git）
cp models.example.json models.json

# 2. 环境变量：按需改 Ollama 上游 / 端口
cp .env.example .env

# 3a. 只用在线模型（models.json 里的 DeepSeek/GLM/Kimi 等）：
docker compose up -d
# 3b. 云服务器上自带 Ollama（CPU 跑小模型 / GPU）：
docker compose --profile ollama up -d
docker compose exec ollama ollama pull qwen3.5:4b   # 拉需要的模型

# 4. 验证
curl http://localhost:8787/api/health
# 浏览器开 http://<服务器IP>:8787
```

## 四、本地还是云端：判定规则与行为差异

界面里有一批**只在本地才有意义**的能力（重启服务、打开工作区文件夹），
界面需要知道自己跑在哪一端。判定不靠配置项、不靠环境变量探测，而是
一条简单可靠的规则：

> **查找控制本地服务的文件在不在：项目根目录同时有 `start.bat` 和
> `stop.bat`（外加 `service.py` 守护脚本）就是本地；不在就是云端。**

为什么这是权威信号：

- 这对 bat 是 Windows 本机的服务启停脚本（双击 start.bat = 装环境 +
  注册开机自启 + 拉起守护进程；stop.bat = 停服务），**只在本机有用**，
  云服务器（Linux 容器）根本用不上；
- `.dockerignore` 明确排除了 `*.bat` 与 `service.py`——云端镜像里
  **必然没有**这两个文件，不会出现「文件在但用不了」的中间态；
- 代码入口：`webui._is_local_deployment()`（文件在不在，零副作用），
  结果随 `/api/health` 的 `local` 字段下发给界面。

判定带来的行为差异：

| 能力 | 本地（bat 在） | 云端（bat 不在） |
|---|---|---|
| 侧栏服务按钮 | 实时状态灯：绿=服务与 Ollama 都在、红=有一个没开、红褐=重启中；点击确认后复用 `stop.bat → start.bat` 重启 | **一律绿**「云端部署 · 服务运行中」，不可点（点击只提示），重启接口直接 400 拒绝 |
| 侧栏工作区路径行 | 显示（点击在资源管理器打开） | **整行隐藏**——云端容器里没有可打开的本机文件夹 |
| Ollama 自动拉起 | 无响应时本机拉起 ollamaguard | 按 `.env` 的 `AGENTFORGE_OLLAMA_HOST` 指向上游，拉不起就地转红提示（云端用 ollama profile 时上游在容器网络里） |
| 评测台「开始评测」 | 可用（执行核心 `tools/bench_tasks.py` 在项目里） | 见第六节注意事项：`tools/` 不进镜像，云端跑评测要么在宿主机跑、要么把 `tools/` 加回镜像 |

同步规则（哪些东西**永远留在本地**、不随代码上云）：`*.bat`、`service.py`、
`models.json`（API Key）、`.prefs.json`、`.tasks.json`、`.agentforge/`、
`.run/`、`evals/results/`——全部列在 `.dockerignore` 里，改这个文件时要
守住「机密与本机状态不入镜像」这条线。

## 五、部署前务必想清楚的三件事

1. **端口暴露**：compose 把 8787 映射到 `0.0.0.0`。Web UI 本身没有登录鉴权，
   直接暴露公网 = 任何人都能指挥你的 agent（默认权限还是完全访问）。
   上公网前务必：改 `AGENTFORGE_PORT` 只绑内网、套 nginx + BasicAuth/TLS、
   或走 SSH 隧道（`ssh -L 8787:localhost:8787`）。
2. **权限模式**：容器就是沙盒（workspace 钉在卷里），但仍建议在界面里把
   默认权限设回「默认权限」，写文件/执行命令留一道人工确认。
3. **API Key 在 models.json**：它不入库不入镜像，靠你在服务器上手工放置；
   泄露 = 别人花你的钱。这台机器的 git 历史里也从来没有过它。

## 六、验证状态（诚实声明）

- **本机验证过的**：代码改动的行为（全量 23 个 Python 套件 + 7 个前端
  回归通过，本机默认路径行为不变）；compose/Dockerfile 的结构与 show
  项目的在用配置同构（同款基础镜像、源、分层策略）；云端判定逻辑有
  常驻断言（`tools/test_service_ctrl.py`）与前端行为断言
  （`tools/test_svc_button.mjs`，云端常绿不可点 + 工作区行隐藏）。
- **本机没法验证的**（这台机器没有 Docker）：镜像实际构建、容器启动、
  healthcheck、卷挂载权限。首次在服务器上 `docker compose up --build` 时
  如有报错，大概率在这四处，按报错修即可。

## 七、常见问题

- **界面提示「Ollama 无响应」**：只走在线模型时这是正常现象（本地判定下
  按钮会转红）；或 `.env` 里 `AGENTFORGE_OLLAMA_HOST` 指错了地址。
- **任务历史丢了**：`AGENTFORGE_DATA_DIR` 没生效（compose 里没挂
  `data/state`）——检查 `docker compose exec agentforge env | grep AGENTFORGE`。
- **云端想跑评测**：镜像里带了 `evals/tasks.yaml`（评测台能看任务、能改），
  但执行核心 `tools/` 没进镜像——云端跑评测要么在宿主机跑
  `python tools/bench_tasks.py --label cloud`，要么把 `tools/` 加回
  Dockerfile（注意里面有测试与本机脚本，按需裁剪）。
- **服务按钮显示不对**：先确认部署形态与文件事实一致——本地项目根目录
  被人为删掉 `start.bat`/`stop.bat` 会被当成云端（按钮常绿不可点）；
  这是判定规则的直接推论，不是 bug。
