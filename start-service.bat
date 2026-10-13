@echo off
rem AgentForge resident service: keeps http://127.0.0.1:8787 alive.
rem Double-click this file once. It runs service.py with pythonw (no window);
rem logs go to .run\service.log, and the web log to .run\web.log.
rem
rem Status:  .venv\Scripts\python.exe service.py --status
rem Stop:    .venv\Scripts\python.exe service.py --stop
rem
rem KEEP THIS FILE ASCII-ONLY WITH CRLF LINE ENDINGS. Same reason as
rem start-web.bat: cmd.exe tracks byte offsets by CRLF, so a bare-LF file or
rem non-ASCII text on a 936 (GBK) console desyncs and starts running
rem half-sentences as commands.
cd /d "%~dp0"

if not exist .venv\Scripts\pythonw.exe (
  echo [AgentForge] No .venv found. Run start-web.bat once first to create it.
  pause
  exit /b 1
)

if not exist .run mkdir .run
start "AgentForge Service" /min ".venv\Scripts\pythonw.exe" service.py
echo [AgentForge] Resident service started - http://127.0.0.1:8787
echo [AgentForge] It restarts the web UI automatically if it ever drops.
