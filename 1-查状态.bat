@echo off
setlocal
cd /d "%~dp0"
echo ======================================================
echo  雷神关闭保护 - 第1步：查当前状态
echo  会弹一次 UAC，请点「是」
echo ======================================================
echo.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY for /f "delims=" %%i in ("where python 2^>nul") do (set "PY=%%i"&goto :found)
:found
if not defined PY (
  echo [错误] 找不到 Python 解释器，请先安装 Python。
  pause
  exit /b 1
)
echo 使用解释器：%PY%
echo.
"%PY%" "tests\run_elevated.py" --console --timeout 180 --wait "real_state\inspector.json" -- "tests\get_real_state.py" --out real_state
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo 完成。结果在 real_state\inspector.json 里。) else (echo [注意] 退出码 %RC% 非 0：可能被拒绝或出错，请看上面的报错。)
echo.
pause
exit /b %RC%
