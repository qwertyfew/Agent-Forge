---
name: remote-ops
description: 云服务器远程运维与模型部署指南。当任务涉及 SSH 连接服务器、远程执行命令、上传文件、在云服务器上安装/部署 Ollama 模型时，按本技能操作。
keywords:
  - 服务器
  - 云服务器
  - 远程
  - SSH
  - 部署
  - 上传
  - 云端
  - ECS
  - 轻量服务器
  - 运维
  - deploy
  - server
---

# Remote Ops（远程服务器与模型部署）

## 前提

用户已配置 SSH 免密（公钥在服务器 `~/.ssh/authorized_keys`）。host 参数可以是
`user@ip`、IP，或 `~/.ssh/config` 里的 Host 别名。

## 标准操作流程

1. **先摸底再动手**：远程操作前先 `ssh_info`，拿到系统版本、内存/磁盘、GPU、
   Ollama 状态——它决定了能部署多大的模型（无 GPU 的 2C4G 小机只适合 1~4B 模型）。

2. **执行远程命令**：`ssh_run`。注意：
   - 每次调用是独立会话，`cd` 不会延续，需要时写成 `cd /path && cmd`
   - 长任务（编译、大文件下载）调高 timeout，上限 1800s
   - 会改动系统状态的操作（apt 安装、写配置）提前向用户说明要做什么

3. **上传文件**：`ssh_upload` 只支持单文件，目录先 `ssh_run` 里 tar 打包或逐个传。

4. **部署模型**：优先用 `ollama_deploy`（自动装 Ollama + 拉模型 + 验证）。
   拉大模型（>4GB）把 timeout 调到 1800。完成后建议：
   - `ssh_run` 执行 `curl 127.0.0.1:11434/api/tags` 确认服务正常

## 把远程模型暴露给外网（危险，需谨慎）

默认 Ollama 只监听 127.0.0.1。若要让其他机器访问：

```bash
sudo systemctl edit ollama   # 写入 Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama
```

**必须同时**：云安全组只放行给可信 IP；Ollama 原生无鉴权，裸暴露 11434 等于
把免费算力送人。更稳妥的方案是用 nginx 加 basic auth 或保持内网。

## 常见失败处理

- `Permission denied` → 免密没配好，按工具返回的提示引导用户配公钥
- `Connection timed out` → 云安全组未放行 22 端口，或 IP 写错
- 国内服务器拉不动 ollama.com → 工具已内置 gitee 镜像源兜底；仍失败就
  本地下载 install.sh 后 `ssh_upload` 上传执行
- 磁盘不足装不下模型 → 先 `ssh_run` 执行 `df -h` 看空间，清理或换小模型
