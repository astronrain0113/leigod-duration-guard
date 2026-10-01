@echo off
setlocal
cd /d "%~dp0"
echo ======================================================
echo  雷神关闭保护 - 真机闭环验证（全自动，你不需要点任何东西）
echo  会弹一次 UAC，请点「是」
echo ------------------------------------------------------
echo  前置条件：雷神已经打开（不用管它在计时还是已暂停）
echo  过程约 30-60 秒，脚本会自己点 X 自己采集证据，
echo  结束后会弹一个结论框。全程不用看控制台、不用切窗口。
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
"%PY%" "tests\run_elevated.py" --console --timeout 300 --wait "tests\out\verify_auto.json" -- "tests\verify_close_loop_auto.py"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo 完成。结论看刚才弹出的窗口，或 tests\out\verify_auto.txt。) else (echo [注意] 退出码 %RC% 非 0：可能 UAC 被取消或出错，请看上面的报错。)
echo 报告位置：tests\out\verify_auto.txt 与 .json 与 .console.log
echo.
pause
exit /b %RC%
