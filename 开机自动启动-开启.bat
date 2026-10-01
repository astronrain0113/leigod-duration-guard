@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   开机自动启动 - 开启
echo   效果：每次登录 Windows 后自动
echo     1. 以管理员身份启动本程序（不弹 UAC）
echo     2. 自动拉起雷神并绑定保护
echo ============================================================
echo.

net session >nul 2>&1
if errorlevel 1 (
  echo [需要管理员] 请右键本文件，选择"以管理员身份运行"
  echo.
  pause
  exit /b 1
)

set "EXE=%~dp0LeigodGuard.exe"
if not exist "%EXE%" (
  echo [错误] 没找到 LeigodGuard.exe
  echo   请把本文件放在 LeigodGuard.exe 同一个文件夹里
  pause
  exit /b 1
)

schtasks /create /tn "LeigodGuard" /tr "\"%EXE%\" --launcher" /sc onlogon /rl highest /f
if errorlevel 1 (
  echo.
  echo [失败] 创建计划任务失败，请把上面的报错截图
  pause
  exit /b 1
)

echo.
echo   设置成功。
echo   下次开机登录后会自动完成：启动本程序 - 拉起雷神 - 进入保护。
echo   本程序只保留一个实例，重复启动不会出问题。
echo.
echo   若想开机后不显示面板（只在托盘）：
echo     编辑计划任务，把参数 --launcher 改成 --launcher --minimized
echo     （任务计划程序里找 LeigodGuard，或再运行一次本文件重设）
echo.
pause
