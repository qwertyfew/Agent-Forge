#!/usr/bin/env bash
# 开机自启（Linux / macOS）：写一份 XDG autostart 条目，登录时自动起常驻服务。
#
#   ./install-autostart.sh            # 安装 / 刷新
#   ./install-autostart.sh --status   # 看是否已安装
#   ./install-autostart.sh --remove   # 卸载
#
# 和 Windows 侧的 install-autostart.bat 是同一件事：条目里必须写死本机的
# 绝对路径，所以它不能进仓库，只能由脚本在每台机器上生成。
# macOS 不认 XDG autostart，用 launchd（见 README「换台电脑」一节）。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
ENTRY="$DIR/agentforge.desktop"
UNAME="$(uname -s 2>/dev/null || echo unknown)"

case "${1:-install}" in
  --remove|remove|uninstall)
    rm -f "$ENTRY"
    echo "[AgentForge] 已移除 $ENTRY"
    echo "[AgentForge] 正在跑的常驻服务不受影响，要停用 ./start-service.sh --stop"
    exit 0
    ;;
  --status|status)
    if [ -f "$ENTRY" ]; then
      echo "[AgentForge] 已安装：$ENTRY"
      echo
      cat "$ENTRY"
    else
      echo "[AgentForge] 未安装。跑 ./install-autostart.sh 安装。"
    fi
    exit 0
    ;;
esac

if [ ! -x "$HERE/start-service.sh" ]; then
  echo "[AgentForge] 找不到可执行的 start-service.sh，请在项目目录里运行本脚本。" >&2
  exit 1
fi

# 这条启动项必须写【原生路径】。Windows 的 POSIX 层（Git Bash / MSYS / Cygwin）里 pwd 返回
# /d/... 这类 MSYS 路径，写进去在真实登录流程里根本解析不了，所以在这层直接叫停。
case "$UNAME" in
  MINGW*|MSYS*|CYGWIN*)
    echo "[AgentForge] 检测到 Windows 的 POSIX 层（$UNAME）。" >&2
    echo "[AgentForge] Windows 上请改用：install-autostart.bat（双击即可）。" >&2
    exit 1;;
  Darwin)
    # macOS 不读 XDG autostart，写了也是白写，不如直说。
    echo "[AgentForge] macOS 不使用 XDG autostart，本脚本无法为它注册自启。" >&2
    echo "[AgentForge] 请自行配置 launchd，或每次登录手动跑 ./start-service.sh。" >&2
    exit 1;;
esac

mkdir -p "$DIR"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=AgentForge
Comment=Keep the AgentForge web UI on http://127.0.0.1:8787 alive
Exec=/bin/bash -lc "cd '$HERE' && ./start-service.sh >>.run/service.boot.log 2>&1"
Icon=utilities-terminal
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo "[AgentForge] 已安装开机自启：$ENTRY"
echo "[AgentForge] 下次登录生效。现在就想用，跑 ./start-service.sh 即可。"
