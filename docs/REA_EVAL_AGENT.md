# REA 项目总结报告

## 项目概述
REA (Reverse Engineer Anything) 是一个旨在将 AI Agent 连接到多种逆向工程工具的 MCP (Model Context Protocol) 服务器。它允许用户利用 AI Agent 来分析原生二进制文件、JavaScript 应用、Electron 应用、.NET 程序集以及网站的行为和底层代码。

## 核心功能与特性
- **全方位分析支持**：涵盖从应用行为层到原生二进制层的多种分析维度。
- **Agent 集成友好**：通过 `npx rea-agents setup` 命令，可自动为 Claude Code、Cursor、Gemini CLI 等 Agent 配置 MCP 服务器及相应的 Workflow 指令。
- **证据驱动的结论**：REA 提供分析结果的同时包含证据链和局限性说明，确保 AI 输出的可验证性。
- **多工具集成**：支持对接 Hopper、Ghidra、IDA 等主流逆向工程工具。
- **混合模式**：支持通过 CLI 直接进行快速分析（如 JS/Electron 应用），也支持复杂的原生代码深度分析。

## 技术架构关键点
- **.agents/skills**: 存储 Agent 可调用的特定技能定义和配套的工作流。
- **native/**: 包含针对不同操作系统（如 Windows）的底层逻辑处理。
- **bridge/** 与 **src/**: 处理核心逻辑、多工具间的桥接及数据流。

## 安装与快速上手
1. 使用 `npx rea-agents setup` 进行 Agent 配置。
2. 支持手动 MCP 注册。
3. 提供 CLI 工具用于独立分析 JavaScript/Electron 应用目录或 ASAR 文件。

## 相关资源
- 官方网站: rea.tools
- 支持的工具提供商: Hopper, Ghidra, IDA 等。
