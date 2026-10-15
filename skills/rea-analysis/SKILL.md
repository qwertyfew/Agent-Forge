---
name: rea-analysis
description: 用本机已安装的 REA CLI（rea-agents）做逆向分析——搞懂某个应用/二进制怎么实现某功能、拿到证据后复刻。覆盖 JS/Electron 应用（含 asar）、.NET 程序集、Go/Rust 二进制、SQLite 快照、原生二进制（需 Hopper/Ghidra/IDA）。涉及"这个软件的这个功能是怎么实现的""逆向""反编译""分析这个 app/exe/dll/asar""给我证据并复刻"时加载本 skill。
version: 1.0
when_to_use: 用户想理解一个没有源码的应用/二进制的实现方式；想复刻某功能并要求证据；分析 asar/提取目录/.NET dll/Go 或 Rust 二进制/SQLite 数据库文件；需要引用 Evidence 而不是猜
when_not_to_use: 分析工作区内的自有源码（直接 read_file/list_dir）；纯前端样式或自身项目开发；目标路径不存在且用户给不出目标时
keywords:
  - 逆向
  - 逆向工程
  - 反编译
  - reverse engineering
  - rea
  - rea-agents
  - 分析应用
  - 分析软件
  - asar
  - Electron
  - 二进制
  - .NET
  - C#
  - IL
  - Ghidra
  - Hopper
  - IDA
  - SQLite
  - Evidence
  - 证据
  - 复刻功能
  - 搞懂实现
  - 某功能怎么实现
---

# REA 逆向分析（本机 CLI 通道）

REA（Reverse Engineer Anything）已全局安装在本机（`rea` 命令，npm 包 `rea-agents`，
源码在 `D:\AI_develop\project\rea`）。它把分析结果连同 **Evidence（证据：出处、摘要、
局限）**一起返回——引用结论时必须带上证据，不许只说结论。

## 通道与总纪律

- **通道是 CLI**：用 `run_command` 调 `rea <子命令> <绝对路径> --json`。没有 MCP 依赖。
- **动手前先体检**：`rea doctor --json` 一次看清 node/provider/skill 各项健康度。
  没配原生 provider 时原生分析不可用，静态分析（JS/.NET/SQLite/Go/Rust）不受影响。
- **绝对路径**：目标一律用绝对路径（`D:\apps\example` 这类）。
- **全量输出先落盘**：真实应用的 Evidence 可达几百 MB，**绝不直接灌进对话**。
  固定套路：`rea ... --json > 文件.json` 落盘，再用 `inspect-analysis-view` 投影出
  10KB 级摘要/分页视图来读。
- **`rea` 不在 PATH 时的兜底**：`npx -y rea-agents@latest <子命令> ...`。
- **运行时捕获会以你的用户权限启动目标程序**：只在用户明确要求"看运行行为"时才做，
  且先说清楚要运行什么。

## 命令速查（按目标类型选路）

### JS / Electron 应用（无需任何原生引擎，最常用）

目标：提取后的应用目录或 `.asar` 文件。标准三步（全量落盘 → 造 view → 读投影）：

```bash
# ① 全量 Evidence 落盘（真实应用可达几百 MB，绝不直接读）
rea analyze-javascript-application D:\apps\example --json > app-evidence.json
# ② 造 view 文件 app-view.json（结构见下）
rea inspect-analysis-view app-view.json   # ③ 投影：334MB 的 Evidence → ~10KB 摘要
```

view 文件结构（手写 JSON 文件）：

```json
{"source": {"kind": "inline", "evidence": <把 app-evidence.json 的整个对象放这里>},
 "view": {"kind": "summary"}}
```

分页读模块列表：`view` 换成 `{"kind": "page", "collection": "modules", "offset": 0, "limit": 32}`；
只读某个模块按 `modules` 里的索引选。详见 rea 仓库 `docs/javascript-application-workflows.md`。

### .NET 程序集（读元数据与 CIL，不执行目标）

```bash
rea inspect-managed-artifact D:\path\Assembly.dll --json          # 身份与部署形态
rea inspect-managed-members D:\path\Assembly.dll --json            # 全部成员
rea inspect-managed-members D:\path\Assembly.dll --type Example.Parser --json
```

`--type` 用成员检查结果里的精确 `full_name`。注意：.NET 单文件宿主与 IL2CPP 不归它管。

### SQLite 快照

```bash
rea inspect-sqlite-database D:\path\db.sqlite --json
rea inspect-sqlite-database D:\path\db.sqlite --json --table NAME --row-limit 20
```

### Go / Rust 二进制（专用静态检查）

```bash
rea inspect-go-binary D:\path\program --json
rea inspect-rust-binary D:\path\program --json
```

### 原生二进制（需先装好 Hopper / Ghidra / IDA 之一）

```bash
rea providers --json          # 看本机有哪些 provider、各支持什么操作
rea capabilities --json
rea analyze D:\path\program --provider ghidra --json
rea search     D:\path\program "关键词" --provider ghidra --json
rea function   D:\path\program main --provider ghidra --json
rea decompile  D:\path\program 0x1000 --provider ghidra --json
rea xrefs      D:\path\program 0x1000 --provider ghidra --json
```

多个 provider 都支持时用 `--provider` 显式指定；环境变量 `REA_ANALYSIS_PROVIDER`
可设长期偏好。用户没装任何 provider 时如实说明并建议装 Ghidra，不要硬猜。

### 运行时行为（浏览器 / Electron / 进程捕获）

按 guide 选路：浏览器走 CDP 观察（browser-observation）、Electron 走
electron-observation / electron-fuses、进程行为对比走 process-capture。
这些会真的运行目标，动手前先向用户确认。

## 输出纪律

1. 每个结论标注它来自哪段 Evidence（文件/函数/模块/字节地址），REA 的输出自带
   provenance 字段，引用时保留它。
2. 分不清「观察事实」和「你的推断」时分开说——推断要标"推断"。
3. Evidence 与局限一起报：REA 会明确说覆盖不到的地方（如"成员引用仍是全程序集
   清单，输出可能很大"），照实转达。
