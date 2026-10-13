# REA 项目核心信息摘要

## 1. 项目定位
REA (Reverse Engineer Anything) 是一个旨在通过智能体（Agents）实现跨二进制文件、应用程序及运行时行为逆向工程的 MCP 服务。它允许用户通过智能体来理解软件功能的底层原理，包括从应用行为到原生二进制文件的深度分析。

## 2. 核心工作机制 (MCP 与逆向分析的关系)
- **MCP 桥接**：REA 通过 Model Context Protocol (MCP) 将逆向工程工具集连接到 AI Agent（如 Claude Code, Cursor 等）。
- **智能体驱动**：Agent 作为主要操作者，通过 REA 提供的 MCP 服务调用底层的逆向引擎和脚本。
- **证据导向**：分析过程强调“证据”，结果包含每项结论背后的证据以及局限性。
- **工作流自动化**：通过 `npx rea-agents setup` 命令，REA 会为 Agent 安装匹配的工作流指令（Workflow Instructions），自动化逆向分析的步骤。

## 3. 依赖的分析引擎角色
REA 支持多种类型的分析，并依赖不同的引擎：
- **Hopper / Ghidra / IDA**：用于原生二进制文件的静态分析。setup 过程中可以配置现有安装，或在获得授权的情况下由 REA 安装 Hopper。
- **JavaScript/Electron 分析**：不需要原生分析引擎。支持对提取出的 JS/Electron 应用目录或 ASAR 文件进行静态分析。
- **.NET 汇编分析**：支持对 .NET 程序集进行分析。
- **网页分析**：支持对网站进行逆向研究。

## 4. 支持的 AI Agent 类型
REA 支持与多种主流 AI 工具集成，包括但不限于：
- Claude Code
- Cursor
- Gemini CLI
- Grok Build
- 其他支持 MCP 的智能体

## 5. 本地执行特性
- **本地运行**：所有的分析过程都在用户本地机器上运行。
- **CLI 支持**：除了与 Agent 集成，REA 也提供独立的 CLI 工具（`rea` 命令），允许用户直接在终端中进行分析。
- **配置备份**：在安装和设置过程中，REA 会保留并备份现有的配置信息。
