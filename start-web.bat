@echo off
rem 一键启动 AgentForge Web UI（端口 8787，日志写 .run\web.log）
rem 换台电脑 clone 下来直接双击这个脚本即可：缺虚拟环境会自动创建并装依赖。
cd /d "%~dp0"

if not exist .venv (
  echo [AgentForge] 首次运行：创建虚拟环境...
  python -m venv .venv
  if errorlevel 1 (
    echo [AgentForge] 创建失败：请先安装 Python 3.10+ 并确保它在 PATH 里。
    pause
    exit /b 1
  )
  echo [AgentForge] 安装依赖...
  .venv\Scripts\python.exe -m pip install --upgrade pip -q
  .venv\Scripts\python.exe -m pip install -r requirements.txt -q
  if errorlevel 1 (
    echo [AgentForge] 依赖安装失败，请检查网络。
    pause
    exit /b 1
  )
)

if not exist .run mkdir .run
start "AgentForge" /min .venv\Scripts\python.exe run.py -c config.lanrun.yaml --web --port 8787
echo [AgentForge] 已启动，稍候几秒后打开 http://127.0.0.1:8787
