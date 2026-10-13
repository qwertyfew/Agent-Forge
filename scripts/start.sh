#!/usr/bin/env bash
# ===========================================================================
# AgentForge 唯一入口（Linux / macOS）—— 等价于 Windows 侧根目录的 start.bat。
#
#   ./scripts/start.sh                  首次会建 .venv 装依赖、登记开机自启，
#                                       然后把常驻服务起起来（幂等，可重复跑）
#   ./scripts/start.sh --status         看现在什么状态
#   ./scripts/start.sh --stop           停掉常驻服务与 Web 服务
#   ./scripts/start.sh --no-autostart   这次不碰自启条目
#   ./scripts/start.sh --remove-autostart  卸载自启条目
#
# 常驻服务本身也是无窗口后台进程（nohup），关掉终端不会停，日志在 .run/service.log。
# ===========================================================================
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

UNAME="$(uname -s 2>/dev/null || echo unknown)"

# Windows 的 POSIX 层（Git Bash / MSYS / Cygwin）不是本脚本的目标：那边双击 start.bat。
# 这些环境里 pwd 返回 /d/... 这类 MSYS 路径，写进启动项在真实登录流程里解析不了。
case "$UNAME" in
  MINGW*|MSYS*|CYGWIN*)
    echo "[AgentForge] 检测到 Windows 的 POSIX 层，请改用项目根目录的 start.bat（双击即可）。" >&2
    exit 1;;
esac

MODE=start
AUTOSTART=yes
for arg in "$@"; do
  case "$arg" in
    --status|status)          MODE=status ;;
    --stop|stop)              MODE=stop ;;
    --remove-autostart)       MODE=remove-autostart ;;
    --no-autostart)           AUTOSTART=no ;;
    -h|--help)                MODE=help ;;
    *) echo "[AgentForge] 未知参数：$arg（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

ENTRY_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
ENTRY="$ENTRY_DIR/agentforge.desktop"

install_autostart() {
  if [ "$UNAME" = "Darwin" ]; then
    echo "[AgentForge] macOS 不读 XDG autostart，无法自动登记开机自启。"
    echo "[AgentForge] 需要的话请自行配置 launchd，或每次登录跑一次 ./scripts/start.sh。"
    return 0
  fi
  mkdir -p "$ENTRY_DIR"
  cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=AgentForge
Comment=Keep the AgentForge web UI on http://127.0.0.1:8787 alive
Exec=/bin/bash -lc "cd '$ROOT' && ./scripts/start.sh --no-autostart >>.run/service.boot.log 2>&1"
Icon=utilities-terminal
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
  echo "[AgentForge] 已登记开机自启：$ENTRY"
}

remove_autostart() {
  if [ -f "$ENTRY" ]; then
    rm -f "$ENTRY"
    echo "[AgentForge] 已卸载开机自启：$ENTRY"
  else
    echo "[AgentForge] 本来就没有自启条目。"
  fi
  echo "[AgentForge] 正在跑的常驻服务不受影响，要停用 ./scripts/start.sh --stop。"
}

usage() {
  sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
}

PY=.venv/bin/python
mkdir -p .run

case "$MODE" in
  help)
    usage
    exit 0;;
  remove-autostart)
    remove_autostart
    exit 0;;
  status)
    [ -x "$PY" ] || { echo "[AgentForge] 还没有虚拟环境，先跑一次 ./scripts/start.sh。" >&2; exit 1; }
    exec "$PY" service.py --status;;
  stop)
    [ -x "$PY" ] || { echo "[AgentForge] 还没有虚拟环境，没有东西可停。" >&2; exit 1; }
    exec "$PY" service.py --stop;;
esac

# ---- 1) 虚拟环境 ----------------------------------------------------------
if [ ! -x "$PY" ]; then
  echo "[AgentForge] 首次运行：创建虚拟环境..."
  python3 -m venv .venv
  echo "[AgentForge] 安装依赖..."
  "$PY" -m pip install --upgrade pip -q
  "$PY" -m pip install -r requirements.txt -q
fi

# ---- 2) 开机自启 ----------------------------------------------------------
if [ "$AUTOSTART" = "yes" ]; then
  install_autostart
fi

# ---- 3) 常驻服务（后台无窗口） --------------------------------------------
# service.py 自己会判断「已经在跑就不重复起」，所以这里不必先查状态。
nohup "$PY" service.py >>.run/service.boot.log 2>&1 &
sleep 1

echo
echo "[AgentForge] 好了。Web UI：http://127.0.0.1:8787"
echo "[AgentForge] 已交给后台常驻服务托管：关掉终端也不会停，服务掉了会自动补位。"
echo "[AgentForge]   状态：./scripts/start.sh --status"
echo "[AgentForge]   停止：./scripts/start.sh --stop"
echo "[AgentForge]   日志：.run/service.log"
