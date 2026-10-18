@echo off
chcp 936 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

set PYTHONIOENCODING=gbk
set PYTHONUTF8=0

set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   没找到 Python。请先装 Python 3.9 以上版本，装的时候记得勾选 Add to PATH。
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

set "PORT=8799"
"%PY%" tools\port_check.py %PORT%
set "RC=!errorlevel!"

if "!RC!"=="0" (
    echo.
    echo   服务已经在运行了，直接打开界面：
    echo   http://127.0.0.1:%PORT%/
    echo.
    start "" "http://127.0.0.1:%PORT%/"
    timeout /t 2 >nul
    exit /b 0
)

if "!RC!"=="2" (
    echo   端口 %PORT% 被别的程序占用了，换 8800 试试。
    set "PORT=8800"
)

echo.
echo   ==========================================
echo     YouTube 视频下载器
echo     浏览器会自动打开，关掉这个窗口就停止服务
echo   ==========================================
echo.

"%PY%" app.py --port %PORT%
if errorlevel 1 (
    echo.
    echo   启动失败了。上面有报错信息，复制给我看看。
    pause
)
