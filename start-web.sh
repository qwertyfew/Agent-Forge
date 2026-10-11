#!/usr/bin/env bash
# 一键启动 AgentForge Web UI（端口 8787，日志写 .run/web.log）
# 换台电脑 clone 下来直接跑这个脚本即可：缺虚拟环境会自动创建并装依赖。
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[AgentForge] 首次运行：创建虚拟环境..."
  python3 -m venv .venv
  echo "[AgentForge] 安装依赖..."
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -r requirements.txt -q
fi

mkdir -p .run
exec .venv/bin/python run.py -c config.lanrun.yaml --web --port 8787
