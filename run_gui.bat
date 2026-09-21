@echo off
REM ============================================================
REM  Course Grabber Launcher  (ASCII only on purpose: cmd.exe
REM  reads .bat as GBK, so non-ASCII text here would break it)
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================
echo    Course Grabber
echo ============================================
echo.

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
    echo [ERROR] Python not found. Install Python 3 and tick "Add to PATH".
    echo [ERROR] Python not found> run_log.txt
    echo.
    pause
    exit /b 1
)

echo Python:
"%PY%" --version
echo.

echo Checking selenium ...
"%PY%" -c "import selenium" >nul 2>nul
if errorlevel 1 (
    "%PY%" -c "import sys;sys.path.insert(0,'libs');import selenium" >nul 2>nul
)
if errorlevel 1 (
    echo selenium missing, installing into .\libs ...
    "%PY%" -m pip install --target libs --no-cache-dir selenium
    echo.
)

echo Launching GUI ...
echo --------------------------------------------
"%PY%" gui.py
set "RC=%ERRORLEVEL%"
echo --------------------------------------------

if not "%RC%"=="0" (
    echo.
    echo [ERROR] gui.py exited with code %RC%
    echo [ERROR] exit %RC% at %DATE% %TIME%> run_log.txt
    echo.
    pause
    exit /b %RC%
)

endlocal
