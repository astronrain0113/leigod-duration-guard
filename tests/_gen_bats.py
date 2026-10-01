# -*- coding: utf-8 -*-
"""生成 GBK+CRLF 的批处理启动器（本机控制台代码页 936，禁止 UTF-8+LF）。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENV = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                    ".workbuddy", "binaries", "python", "envs", "default",
                    "Scripts", "python.exe")


def emit(name, lines):
    blob = []
    for ln in lines:
        ln.encode("cp936")                       # 预检：GBK 编不了就当场失败
        blob.append(ln + "\r\n")
    path = os.path.join(ROOT, name)
    with open(path, "w", encoding="cp936", newline="") as f:
        f.write("".join(blob))
    print("已生成", path)


COMMON_HEAD = [
    "@echo off",
    "setlocal",
    'cd /d "%~dp0"',
]
PY_PROBE = [
    'set "PY="',
    'if exist "%~dp0.venv\\Scripts\\python.exe" set "PY=%~dp0.venv\\Scripts\\python.exe"',
    'if not defined PY if exist "{VENV}" set "PY={VENV}"'.format(VENV=VENV),
    'if not defined PY for /f "delims=" %%i in ("where python 2^>nul") do (set "PY=%%i"&goto :found)',
    ":found",
    "if not defined PY (",
    "  echo [错误] 找不到 Python 解释器，请先安装 Python。",
    "  pause",
    "  exit /b 1",
    ")",
    "echo 使用解释器：%PY%",
    "echo.",
]

emit("1-查状态.bat", COMMON_HEAD + [
    "echo ======================================================",
    "echo  雷神关闭保护 - 第1步：查当前状态",
    "echo  会弹一次 UAC，请点「是」",
    "echo ======================================================",
    "echo.",
] + PY_PROBE + [
    '"%PY%" "tests\\run_elevated.py" --console --timeout 180 '
    '--wait "real_state\\inspector.json" -- "tests\\get_real_state.py" --out real_state',
    'set "RC=%ERRORLEVEL%"',
    "echo.",
    'if "%RC%"=="0" (echo 完成。结果在 real_state\\inspector.json 里。) '
    'else (echo [注意] 退出码 %RC% 非 0：可能被拒绝或出错，请看上面的报错。)',
    "echo.",
    "pause",
    "exit /b %RC%",
])

emit("2-真机闭环.bat", COMMON_HEAD + [
    "echo ======================================================",
    "echo  雷神关闭保护 - 真机闭环验证（全自动，你不需要点任何东西）",
    "echo  会弹一次 UAC，请点「是」",
    "echo ------------------------------------------------------",
    "echo  前置条件：雷神已经打开（不用管它在计时还是已暂停）",
    "echo  过程约 30-60 秒，脚本会自己点 X 自己采集证据，",
    "echo  结束后会弹一个结论框。全程不用看控制台、不用切窗口。",
    "echo ======================================================",
    "echo.",
] + PY_PROBE + [
    '"%PY%" "tests\\run_elevated.py" --console --timeout 300 '
    '--wait "tests\\out\\verify_auto.json" -- "tests\\verify_close_loop_auto.py"',
    'set "RC=%ERRORLEVEL%"',
    "echo.",
    'if "%RC%"=="0" (echo 完成。结论看刚才弹出的窗口，或 tests\\out\\verify_auto.txt。) '
    'else (echo [注意] 退出码 %RC% 非 0：可能 UAC 被取消或出错，请看上面的报错。)',
    "echo 报告位置：tests\\out\\verify_auto.txt 与 .json 与 .console.log",
    "echo.",
    "pause",
    "exit /b %RC%",
])
