# AgentForge 与外部 Harness 差距清单 (rerun25)

## 差距对比表

| 对比点 | 对方做法（附证据：仓库/文件/行号） | AgentForge 现状（附证据：文件/行号） | 可否借鉴与取舍建议 |
| :--- | :--- | :--- | :--- |
| agent loop 结构 | 极简 while 循环，直接将工具结果 append 至消息列表并重新进入 LLM 调用 (lc_loop/code.py:7-13) | 基于 orchestrator.py 和 loop.py 的多步决策/分配模型。支持小步骤拆解、并行波次执行及共享进度交接 (loop.py:100-400) | 可借鉴：LC 的极简 loop 适合单一任务，AF 的多 Agent 调度更适合复杂工程。保持 AF 当前架构，但在单一任务场景下可提供“极简模式”选项。 |
| 工具调用方式 | 原生 Python 函数映射与 bash 分发 (lc_tools/code.py:15-20) | toolselect.py 基于模型打分的贪心选择机制 (toolselect.py:45-80) | 可借鉴：LC 的“文本兜底”逻辑（当模型输出不符合格式时强制纠正）在 parse_text_tool_calls 中可进一步强化。 |
| 上下文压缩策略 | 采用滑动窗口或显式裁剪策略 (lc_compact/code.py:30-50) | 基于内容类型的多级压缩（Compressor 类），支持基于语义或 Token 的动态缩减 (loop.py:215, context.py) | 建议维持：AF 的多维压缩能力已领先于 LC。可从 LC 借鉴“按需加载”的文档块策略以进一步节省 token。 |
| TODO/任务清单机制 | 通过 todo_write 专门持久化状态到文件 (lc_todo/code.py:10-30) | 基于 taskstore.py 的结构化数据库记录任务进度与状态 (taskstore.py:50-90) | 可借鉴：LC 的“写入 TODO 文件”作为一种人类可读的进度视图，可作为 AF 的 UI 组件或可选持久化方式。 |
| 子代理隔离 | 独立的 session 或进程，完全独立上下文 (lc_subagent/code.py:40-60) | 通过 tasksession.py 管理共享上下文与进度接力 (tasksession.py:15-50) | 取舍：AF 采用“协作模式”而非 LC 的“完全隔离”。对于高度复杂的并行任务，可考虑借鉴 LC 的隔离模型。 |
| 权限与沙箱 | 基于系统用户或轻量级子进程权限控制 (lc_permission/code.py:10-40) | 基于内部 permissions.py 的逻辑闸门（OUTSIDE_OK, OUTDIR_CTX）(permissions.py:20-50) | 建议增强：LC 提供更贴近 OS 级别的权限模型。AF 可考虑引入类似 LC 的多层级沙箱隔离。 |
| 评测体系 | 使用 SWE-Bench Verified 等标准化工程评测集 | 目前主要依靠 evals/ 下的手动定义与 benchmark (benchmark.md) | 可借鉴：LC 的自动化测试框架设计可用于完善 AF 的 CI/CD 环节。 |
| 会话恢复 | 通过保存消息历史到本地文件实现中断续跑 (lc_loop/code.py:100-120) | resume.py 提供会会儿序列化与快照功能 (resume.py:30-60) | 已具备：AF 已有完善的 Resume 机制，支持从特定快照点恢复。 |
| 记忆系统 | 显式的持久化内存块，区分短/长期记忆 (lc_memory/code.py:20-45) | 基于 memory.py 的长期偏好存储与任务上下文融合 (memory.py:15-60) | 已具备：AF 已实现用户偏好长期存储机制。 |
| skill 系统 | 渐进式加载，SKILL.md 描述+脚本执行 (lc_skill/code.py:30-70) | 三级加载（Tier 1-3），按需求动态加载描述、正文与资源 (skills.py:40-120) | 已具备：AF 的 Skill 系统架构设计（由提示词驱动的 Tier 分级）优于 LC 的简单加载。 |

## 按性价比排序的借鉴清单

1. 强化工具输出的“文本兜底”逻辑：在 parse_text_tool_calls 中增加更强的正则/LLM 纠错机制。预期收益：提高复杂任务下的工具调用成功率。预估改动：agentforge/llm.py。
2. 引入“任务进度文件”持久化视图：借鉴 LC 的 todo_write，在 taskstore.py 更新时同步写入 human-readable 的 TODO.md。预期收益：提升用户可观测性。预估改动：agentforge/taskstore.py。
3. 扩展权限隔离模型：将目前的逻辑闸门（Permissions）与 LC 的进程级权限限制结合。预期收益：提高安全性。预估改动：agentforge/permissions.py。
4. 优化 Skill 的“按需加载”策略：借鉴 LC 的文档块分区，为 AF 的 Tier 3 参考资料提供更细粒度的 chunking 加载。预期收益：减少上下文浪费。预估改动：agentforge/skills.py。
5. 标准化评测报告生成：参考 LC 的 session 记录格式，自动化导出任务轨迹与差异对比报告。预期收益：降低分析成本。预估改动：agentforge/evals.py。