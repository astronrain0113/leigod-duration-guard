@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   开机待命 - 开启
echo   效果：本工具安静待在托盘；你一打开雷神，它立刻弹出并接管
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
schtasks /create /tn "LeigodGuardStandby" /tr "\"%EXE%\" --watcher" /sc onlogon /rl highest /f
if errorlevel 1 (
  echo.
  echo [失败] 创建计划任务失败，请把上面的报错截图
  pause
  exit /b 1
)
echo   已设置：以后每次登录 Windows 自动进入待命。
echo.
echo   注意：计划任务的 onlogon 触发器只在**下次登录**时才生效，
echo         所以现在立刻手动把待命启动起来（不等下次开机）：
echo.
schtasks /run /tn "LeigodGuardStandby"
if errorlevel 1 (
  echo   [提示] 立刻启动失败，请手动双击一次"立刻待命.bat"
) else (
  echo   已启动。
)
echo.
echo   怎么确认它在跑：
echo     看屏幕右下角托盘，会出现一个**蓝色小方块**图标，
echo     鼠标移上去显示「雷神总时长保护 —— 待命中」。
echo     如果被 Windows 收起来了，点托盘左边的 ^ 就能翻出来。
echo.
echo   以后你双击打开雷神加速器：
echo     保护工具会自动弹出，吸附在雷神窗口旁边开始保护。
echo     你关掉雷神时它跟着退，下次再开雷神又会自己回来。
echo.
echo   取消待命：先右键托盘图标选"退出待命"，再运行"开机待命-关闭.bat"
echo.
pause
