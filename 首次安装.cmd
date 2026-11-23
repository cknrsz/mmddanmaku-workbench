@echo off
setlocal
cd /d "%~dp0"
echo Checking for Python 3.12 or newer...
py -3 -c "import sys; sys.exit(sys.version_info ^< (3, 12))" >nul 2>&1
if errorlevel 1 (
    echo Python 3.12 or newer was not found. Install it from https://www.python.org/downloads/windows/ and run this script again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info ^< (3, 12))"
if errorlevel 1 (
    echo The existing .venv requires Python 3.12 or newer. Remove or replace it and retry.
    goto failed
)
echo Installing application dependencies from PyPI...
".venv\Scripts\python.exe" -m pip install --index-url https://pypi.org/simple "./backend[bilibili]"
if errorlevel 1 (
    echo PyPI installation failed. Retrying with the Tsinghua mirror...
    ".venv\Scripts\python.exe" -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple "./backend[bilibili]"
    if errorlevel 1 goto failed
)
echo Installation complete. Run launch-windows.cmd to start.
pause
exit /b 0
:failed
echo Installation failed. Check the error above and try again.
pause
exit /b 1
