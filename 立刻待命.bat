@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   立刻待命（只影响本次开机，不改任何开机设置）
echo ============================================================
echo.
set "EXE=%~dp0LeigodGuard.exe"
if not exist "%EXE%" (
  echo [错误] 没找到 LeigodGuard.exe
  pause
  exit /b 1
)
start "" "%EXE%" --watcher
echo   已启动待命守望。
echo.
echo   去屏幕右下角托盘找**蓝色小方块**图标（可能在 ^ 里面）。
echo   鼠标移上去会显示「雷神总时长保护 —— 待命中」。
echo.
echo   之后你打开雷神加速器，保护工具会自己弹出来。
echo.
timeout /t 3 >nul
