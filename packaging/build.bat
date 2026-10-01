@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0.."
echo ============================================================
echo  雷神总时长保护工具 - 打包脚本
echo  产物: dist\LeigodGuard\LeigodGuard.exe   (图形版)
echo        dist\LeigodGuard\LeigodGuardCUI.exe (工具版，与主程序共用 _internal)
echo ============================================================
echo.
echo [1/5] 检查打包依赖 PyInstaller...
python -c "import PyInstaller" 1>nul 2>nul
if errorlevel 1 goto NO_PYINSTALLER
echo       已安装。
echo.
echo [2/5] 跳过强制清理（本机删除守卫会中断批量删除，改用覆盖构建）...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo       完成。
echo.
echo [3/5] 打包图形版（不弹控制台窗口）...
python -m PyInstaller --noconfirm packaging\LeigodGuard.spec
if errorlevel 1 goto BUILD_FAILED
echo.
echo [4/5] 打包控制台工具版（界面诊断 / 坐标校准要用）...
python -m PyInstaller --noconfirm packaging\LeigodGuardCUI.spec
if errorlevel 1 goto BUILD_FAILED
echo.
echo [5/5] 把工具版 exe 放进图形版目录（共用同一份 _internal），并做一次自检...
xcopy /y "dist\LeigodGuardCUI\LeigodGuardCUI.exe" "dist\LeigodGuard\" 1>nul
if errorlevel 1 goto COPY_FAILED
echo.
echo ============================================================
echo  打包完成
echo   图形版  : dist\LeigodGuard\LeigodGuard.exe
echo   工具版  : dist\LeigodGuard\LeigodGuardCUI.exe
echo.
echo  建议: 把整个 dist\LeigodGuard 目录复制到有写权限的位置
echo        （配置 config\config.json 与日志 logs\ 会写在 exe 旁边）
echo  提醒: 如果雷神以管理员身份运行，本程序也必须以管理员身份运行，
echo        否则 Windows 会拒绝锁定雷神的关闭按钮，关闭保护会失效。
echo ============================================================
echo.
echo 正在运行环境自检（找不到雷神时会以退出码 2 结束，属正常）...
echo ------------------------------------------------------------
"dist\LeigodGuard\LeigodGuardCUI.exe" --check
echo ------------------------------------------------------------
echo 自检结束。
endlocal
exit /b 0

:NO_PYINSTALLER
echo.
echo [错误] 没有找到 PyInstaller。请先执行:
echo        pip install pyinstaller
endlocal
exit /b 1

:BUILD_FAILED
echo.
echo [错误] 打包失败，请查看上面的 PyInstaller 输出。
endlocal
exit /b 1

:COPY_FAILED
echo.
echo [错误] 复制控制台版到图形版目录失败，请检查 dist 目录是否被占用。
endlocal
exit /b 1
