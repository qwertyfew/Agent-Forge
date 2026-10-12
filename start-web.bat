@echo off
rem One-click launcher for the AgentForge Web UI (port 8787, log: .run\web.log).
rem On a fresh clone just double-click this file: a missing virtualenv is
rem created and dependencies are installed automatically.
rem
rem KEEP THIS FILE ASCII-ONLY WITH CRLF LINE ENDINGS. Do not add non-ASCII text
rem and do not convert it to UTF-8:
rem   * cmd.exe tracks byte offsets by CRLF, so a bare-LF file desyncs and
rem     starts executing from the middle of a line;
rem   * on a 936 (GBK) console, UTF-8 CJK bytes make the GBK decoder swallow
rem     the trailing CR, which desyncs the file just the same.
rem Either way cmd runs half-sentences as commands and the script dies.
rem See docs/BATCH_D_REPORT.md section 6 for the full analysis.
cd /d "%~dp0"

if not exist .venv (
  echo [AgentForge] First run: creating the virtualenv...
  python -m venv .venv
  if errorlevel 1 (
    echo [AgentForge] Failed. Install Python 3.10+ and make sure it is on PATH.
    pause
    exit /b 1
  )
  echo [AgentForge] Installing dependencies...
  .venv\Scripts\python.exe -m pip install --upgrade pip -q
  .venv\Scripts\python.exe -m pip install -r requirements.txt -q
  if errorlevel 1 (
    echo [AgentForge] Dependency install failed. Check your network.
    pause
    exit /b 1
  )
)

if not exist .run mkdir .run
rem Prefer the machine-local config; fall back to the generic template.
set CFG=config.yaml
if exist config.lanrun.yaml set CFG=config.lanrun.yaml
rem Run through a child shell so stdout/stderr land in .run\web.log.
rem -u keeps Python unbuffered, otherwise the log stays empty until exit.
if exist .run\web.log del .run\web.log
start "AgentForge" /min cmd /c ".venv\Scripts\python.exe -u run.py -c %CFG% --web --port 8787 > .run\web.log 2>&1"
echo [AgentForge] Started with config %CFG% - open http://127.0.0.1:8787 in a few seconds
