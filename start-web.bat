@echo off
rem Start AgentForge Web UI on port 8787 (detached, log to .run\web.log)
cd /d "%~dp0"
if not exist .run mkdir .run
start "AgentForge" /min .venv\Scripts\python.exe run.py -c config.lanrun.yaml --web --port 8787
