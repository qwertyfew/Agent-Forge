#!/usr/bin/env bash
# 常驻服务（Linux / macOS）：让 http://127.0.0.1:8787 一直开着。
# 等价于 Windows 侧的 start-service.bat —— 起一个后台的 service.py 当保姆：
# 端口没人服务就补位拉起（只补位不抢占、不杀进程），顺带守 Ollama。
#
#   ./start-service.sh              # 后台起，日志 .run/service.log
#   ./start-service.sh --status     # 看一眼现在什么状态
#   ./start-service.sh --stop       # 停掉常驻服务与 Web 服务
set -e
cd "$(dirname "$0")"
mkdir -p .run

# Windows 上的 POSIX 层（Git Bash / MSYS / Cygwin）不是本脚本的目标：那边用
# start-service.bat。这些环境下 pwd 返回 /d/... 这类 MSYS 路径，写进启动项会失效。
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "[AgentForge] 检测到 Windows 的 POSIX 层，请改用 start-service.bat（双击即可）。" >&2
    exit 1;;
esac

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "[AgentForge] 还没有虚拟环境。先跑一次 ./start-web.sh 把依赖装上。" >&2
  exit 1
fi

if [ "${1:-}" = "--status" ] || [ "${1:-}" = "--stop" ]; then
  exec "$PY" service.py "$@"            # 这两个子命令前台跑，直接看输出
fi

nohup "$PY" service.py "$@" >>.run/service.boot.log 2>&1 &
echo "[AgentForge] 常驻服务已在后台运行（PID $!）。"
echo "[AgentForge] 页面：http://127.0.0.1:8787   日志：.run/service.log"
