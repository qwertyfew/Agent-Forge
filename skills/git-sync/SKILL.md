---
name: git-sync
description: 本项目（AgentForge）的代码同步规范。把改动提交并推送到 GitHub 仓库 qwertyfew/Agent-Forge 时加载；涉及 commit、push、分支、远程、git 报错处理时也用本 skill。
keywords:
  - 提交
  - 推送
  - 同步
  - git
  - commit
  - push
  - 远程
  - 仓库
  - 分支
  - 提交代码
  - 同步到git
  - origin
  - 版本控制
---

# Git 同步（AgentForge）

## 仓库信息

- 远程：`origin` → `git@github.com:qwertyfew/Agent-Forge.git`（SSH，走 `~/.ssh/config` 里
  映射的 `ssh.github.com:443`；HTTPS 走 `https://github.com/` 时本机 gitconfig 已配 ghproxy 代理）
- 主分支：`main`（已 `-u` 跟踪 origin/main）
- 提交身份（仓库本地配置）：`qwertyfew <gjiafuq82@163.com>`
- 当前仓库根 = 工作区根 = 这个项目目录本身

## 标准流程

```bash
git status -sb            # 先看改了什么
git diff --stat           # 需要时看改动规模
git add -A
git commit -m "..."       # 见下方提交信息规范
git push                  # 已配 upstream，直接推 origin main
```

- 非交互环境务必加 `GIT_TERMINAL_PROMPT=0`，避免 git 弹凭据输入把任务挂住。
- 推送前先 `git pull --rebase` 只有在远程可能领先时才需要（单人仓库一般不需要）。
- 若 push 被拒（non-fast-forward）：先 `git pull --rebase origin main`，解决冲突后再推；
  千万不要用 `git push --force`。

## 提交信息规范

中文，采用 `类型: 简述` 的约定式前缀，正文用短横线列出要点：

- `feat:` 新功能  `fix:` 修复缺陷  `refactor:` 重构  `docs:` 文档
- `style:` 纯样式调整（前端微调属于这类）  `chore:` 配置/杂务  `perf:` 性能

示例：

```
style: 侧栏栏目互斥淡显

- 切换栏目时「新建任务」跟着变淡，悬停恢复
- 清理无用的计数角标样式
```

## 红线（必须遵守）

1. **绝不提交密钥与机器相关状态**：`models.json`（含 API Key）、`.tasks.json`、`.prefs.json`、
   `.agentforge/`、`.sessions/`、`.run/`、`.venv/` 都已在 `.gitignore` 里。
   `git add -A` 之后必须用
   `git diff --cached --name-only | grep -Ei "models\.json|tasks\.json|prefs\.json|\.venv|\.run/|sessions|\.env"`
   自查一遍，命中就 `git reset` 掉再处理。
2. 不提交二进制/大文件（模型权重、截图、日志）。
3. 不 `git push --force`、不 `git reset --hard` 丢弃他人提交。
4. 提交前确保代码能跑（至少 `python -c "import ast; ast.parse(open('webui.py',encoding='utf-8').read())"`
   与前端 `<script>` 语法校验通过）。

## 相关工具

本仓库的插件里已有 `git_status` / `git_diff` / `git_add` / `git_commit` / `git_push` / `git_log` 工具，
优先用它们（写操作会走审批）；需要更细的命令时才用 `run_command` 调 git。
