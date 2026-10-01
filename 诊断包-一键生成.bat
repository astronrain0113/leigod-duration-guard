@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   生成诊断包【会弹一次 UAC，请点「是」】
echo ============================================================
echo.
set "EXE=%~dp0LeigodGuardCUI.exe"
if not exist "%EXE%" goto NOEXE
set "STAMP=%date:~0,4%%date:~5,2%%date:~8,2%-%time:~0,2%%time:~3,2%%time:~6,2%"
set "STAMP=%STAMP: =0%"
set "OUT=%~dp0logs\inspector-%STAMP%"
echo   产物目录： logs\inspector-%STAMP%\
echo   请稍等十几秒，跑完会告诉你结果。
echo.
start "" /wait "%EXE%" --inspector --out "%OUT%"
echo.
if exist "%OUT%\inspector.json" goto DONE
echo   [注意] 没有生成诊断包，常见原因：
echo          1、UAC 那一步点了「否」；
echo          2、雷神客户端没在运行。
echo          目录：%OUT%
echo.
pause
exit /b 1

:DONE
echo   [完成] 诊断包已生成。提 Issue 时把整个目录拖进去即可。
start "" "%OUT%"
echo.
pause
exit /b 0

:NOEXE
echo.
echo   [错误] 没找到 LeigodGuardCUI.exe，它应当与本脚本放在同一个目录里。
echo.
pause
exit /b 1
