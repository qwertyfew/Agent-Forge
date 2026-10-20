# Harness 教程对照审查报告（2026-10-16）

对照对象：GitHub 主流「从零搭建 harness」教程与开源项目。
被审对象：AgentForge（agentforge/ 核心 13,138 行 + tools/ 测试 6,272 行）。

## 一、对照的教程与项目

| 项目 | 定位 | 参考价值 |
|---|---|---|
| SWE-agent/mini-swe-agent (MIT) | 100 行核心达到 SWE-Bench 74%，普林斯顿+斯坦福 | 「机制 vs 策略」设计哲学的标尺 |
| djscruggs/build-your-own-harness | 17 层从零教程（API shell→RL） | 层级清单，逐层对照查缺 |
| adamaoc/harness-101 | 逐行教程（Planner/Implementer/Reviewer） | 教学基准 |
| shareAI-lab/learn-claude-code | 12 session 复刻 Claude Code 工程思路 | 上下文工程、子代理、权限 |
| FareedKhan-dev/claude-code-from-scratch | 23 组件复现 | 组件清单 |
| HKUDS/nanobot、nousresearch/hermes-agent | 生产级精简/记忆与技能自动沉淀 | 与 autoskill/autonote 思路同源，可对照 |

## 二、审查结论：做对的（无需担心）

1. **核心循环**：模型驱动停止、原生 tool_calls 流式合并（分片按 index 合并）+ 文本解析兜底——与所有教程的最佳实践一致。
2. **上下文管理**：L1 截断 / L2 摘要两级压缩，轮次边界切割防协议 400，token 估算留 40% 余量防 Ollama 静默截头——比多数教程的「滑动窗口」精细一个档次。
3. **任务笔记跨压缩持久注入**（task_notes）：多数教程没有这一层，属于超纲做法。
4. **失败结果强注脚 + 连续失败升级警告**：guardrails 高级做法，直指小模型「无视错误谎报成功」的真实故障模式。
5. **评测体系**（evalsdesk/scores/verify 闸门）：教程公认最被忽视的部分，你已有且在用。
6. **权限分级 + 路径边界 + 命令黑名单**、**长期记忆克制设计（只放偏好）**、**多 agent 调度 + 串行闸门**、**产出追踪（工具亲写 vs 快照推测分开）**——均达到或超过教程水准。

## 三、确实存在的问题（按优先级）

### P1 沙箱隔离是最大的结构性缺口
- 现状：命令继承用户权限直接跑在宿主机，只有路径边界 + 黑名单命令拦截。
- 教程共识（mini-swe-agent 文档原话）：**bash-only 不等于隔离**。黑名单拦不住「删除黑名单之外的文件」「curl 拉脚本再执行」。
- Dockerfile 是上云部署用的，不是沙箱。
- 缓解建议：高风险命令（删除/网络下载后执行）走受限通道；或长线把 run_command 挂到容器/受限代理后面。个人本机自用可接受，但要清楚这条底线在哪。

### P2 会话恢复缺失（教程 Layer 15）
- 现状：重启后遗留「执行中」只标记为已中断，不能从断点续跑。长任务（实测有过 28 分钟的）中断即白跑。
- 建议：对话历史已持久化在任务台账，补一个「带着历史重建上下文继续执行」的入口，是低成本高收益改动。

### P3 harness 侧规则越积越厚——机制 vs 策略的张力
- mini-swe-agent 的核心教训：把「聪明胶水」从 harness 删掉，让模型自己干。
- AgentForge 反向而行：系统提示 + verify gate + honesty_guard + 任务定性规则 + 来源等级规则全是 harness 侧硬规则。
- 对 gemma4:12b 这类本地小模型，这是**必要的补偿**，不是错误；但两个衍生风险：
  1. 关键词式规则（「改动物件+改动动作」定性）是脆弱的，长尾任务会误判；
  2. 规则堆积 → 系统提示膨胀 → 小模型注意力稀释（你自己已经发现要往消息末尾追加提醒，这就是症状）。
- 建议：做一次「规则瘦身评测」——每条规则跑一轮「删掉它，evals 掉几分」，掉分才留。你有 evals 体系，做这个有现成基础。

### P4 MCP 客户端未做（路径 B，已知）
- 教程 Layer 7。做上 tools/mcp_client.py 后接入生态工具不必逐个写适配。

### P5 钩子框架不通用
- verify/权限检查是写死的特定闸门；教程做法是通用 pre/post-action hook 表，实验能力不必改核心循环。现有闸门已是雏形，抽象一层即可。

### P6 小点：token 估算可校准
- measure_tokens 是启发式；Ollama 响应里的 prompt_eval_count 是实测值，可用首次真实响应回填校准窗口判断。

## 四、REA 分析高质量 harness 的可行性

**可行，基础设施已就绪**（external/ 机制、rea-analysis skill、路由验证均通过）。客观限制：

1. **模型能力是瓶颈**：gemma4:12b 对中小仓库够用；对 OpenHands 级别的数千文件生产仓库会吃力，可勾选付费 API 补。
2. **「分析」≠「借鉴」**：REA 产出结构化事实与证据；变成改进决策需要人做判断。建议任务书直接要求「产出与 AgentForge 的逐项差距清单」，不要泛泛的架构报告。
3. **选型建议**（对象比工具重要）：
   - 首选 **mini-swe-agent**（MIT、小、强、设计取舍有文档）与 **learn-claude-code**（教程型、贴近 Claude Code 工程思路）；
   - 二阶段可上 **hermes-agent**（记忆/技能自动沉淀与 autoskill 同源，对照价值高）；
   - OpenHands 体量过大，本地模型性价比低。
4. 许可证均为 MIT/开源，克隆入 external/（已 gitignore）合规无虞。

## 五、建议的行动顺序

1. P1 沙箱评估（先划清底线：哪些命令必须过受限通道）
2. P2 会话恢复（低成本高收益）
3. P3 规则瘦身评测（防系统提示无限膨胀，这是「意识不到的问题」里最隐蔽的一个）
4. external/ 收编 mini-swe-agent + learn-claude-code，跑 REA 对比分析
5. P4 MCP 路径 B
