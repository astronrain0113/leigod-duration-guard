@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   开机自动启动 - 关闭
echo ============================================================
echo.
net session >nul 2>&1
if errorlevel 1 (
  echo [需要管理员] 请右键本文件，选择"以管理员身份运行"
  echo.
  pause
  exit /b 1
)
schtasks /delete /tn "LeigodGuard" /f
if errorlevel 1 (
  echo   没有找到名为 LeigodGuard 的计划任务（可能本来就没开启）
) else (
  echo   已关闭开机自动启动。
)
echo.
pause
