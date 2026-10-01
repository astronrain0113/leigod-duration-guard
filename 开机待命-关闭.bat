@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   开机待命 - 关闭
echo ============================================================
echo.
net session >nul 2>&1
if errorlevel 1 (
  echo [需要管理员] 请右键本文件，选择"以管理员身份运行"
  echo.
  pause
  exit /b 1
)
schtasks /delete /tn "LeigodGuardStandby" /f >nul 2>&1
if errorlevel 1 (
  echo   没找到待命任务（可能本来就没开启，或已经关掉了）
) else (
  echo   已删除计划任务：下次登录不会再自动待命。
)
echo.
echo   注意：这只取消了「以后开机自动待命」。
echo   当前正在跑的那个待命进程要另外停：
echo     右键屏幕右下角托盘里的蓝色小方块图标 → "退出待命"
echo.
pause
