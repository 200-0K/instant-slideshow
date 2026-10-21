@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" web_server.py %*
) else (
    python.exe web_server.py %*
)

echo.
echo Web server stopped.
pause
