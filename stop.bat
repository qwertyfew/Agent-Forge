@echo off
rem ===========================================================================
rem AgentForge - stop button. Lives next to start.bat on purpose.
rem
rem Double-click to stop the resident service (and the web server it keeps up).
rem
rem This file holds NO logic of its own: it forwards to `start.bat --stop`.
rem That matters - there is still exactly one place that knows how to stop
rem things (service.py --stop), so this cannot drift out of sync with
rem start.bat. It exists only because Windows cannot double-click a batch file
rem "with an argument": `start.bat --stop` is not a file, it is start.bat plus
rem a flag, and using it means opening a terminal and typing the flag out.
rem
rem KEEP THIS FILE ASCII-ONLY WITH CRLF LINE ENDINGS - same rule as start.bat.
rem cmd.exe finds commands by byte offset, so a bare-LF file or non-ASCII text
rem makes it execute half-sentences. See README: "Cross-platform script line
rem endings".
rem ===========================================================================
setlocal EnableExtensions

call "%~dp0start.bat" --stop

rem Below: hold the window open, but only when the file was double-clicked.
rem Explorer launches a batch file as `cmd /c ""<path>" "`, and the shell puts
rem its own command line into %cmdcmdline% - so the file name shows up there.
rem A command typed at a prompt does not mention the file, so a terminal is left
rem alone. The quotes around %cmdcmdline% keep a "&" in the path from being read
rem as a command separator.
rem
rem find.exe is called by full path on purpose: Git for Windows ships a GNU
rem find.exe, and a bare `find` resolved to it once cmd inherited a Git Bash
rem PATH - it then died on /i ("No such file or directory"). Naming the system
rem one makes the check PATH-independent.
echo "%cmdcmdline%" | "%SystemRoot%\System32\find.exe" /i "%~nx0" >nul
if not errorlevel 1 pause
