@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY="%~dp0python\python.exe"
if not exist %PY% (
    echo [ERROR] Embedded Python not found at %PY%
    echo Please run setup_env.bat first.
    pause
    exit /b 1
)
%PY% "%~dp0file_sync_monitor.py"
echo.
echo [Program exited] Press any key to close.
pause >nul
