"""以管理员身份重跑关闭保护前提探针（会弹一次 UAC）。

为什么必须提权：
  · 雷神以 requireAdministrator 运行，永远处于高完整性级别；
  · 未提权进程对它的窗口做 `SendMessage(WM_NCHITTEST)` / `EnableMenuItem`
    会被 UIPI **静默拦截**，表现为「返回 0 / 返回 0xFFFFFFFF」——
    与「窗口真的这么回答」无法区分。
  · 因此非提权跑出来的结论不可用（见 docs/真机发现-关闭保护架构问题.md）。

本脚本只做一件事：用 runas 拉起 tests/probe_real_close_caps.py（无控制台），
等待它写出报告后打印出来。探针本身是只读的（唯一写操作「禁用 SC_CLOSE→立即还原」）。

用法：
    python tests/run_probe_elevated.py
"""
import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROBE = os.path.join(HERE, "probe_real_close_caps.py")
OUT = os.path.join(HERE, "out", "real_close_caps_admin.txt")

# 用与项目同源的隔离 venv 里的 pythonw.exe（无控制台，避免弹黑框）
PYW = os.path.join(os.environ.get("WORKBUDDY_PYTHON_DIR",
                                 os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                                  ".workbuddy", "binaries", "python",
                                  "envs", "default")),
                  "Scripts", "pythonw.exe")


def before_mtime():
    try:
        return os.path.getmtime(OUT)
    except OSError:
        return 0.0


def main() -> int:
    if not os.path.exists(PYW):
        print(f"找不到 pythonw.exe：{PYW}\n请设置 WORKBUDDY_PYTHON_DIR 环境变量指向 venv 根目录")
        return 2
    if not os.path.exists(PROBE):
        print(f"找不到探针：{PROBE}")
        return 2

    t0 = before_mtime()
    print("正在请求管理员权限（屏幕上会弹出 UAC，请点「是」）…", flush=True)

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_int]
    rc = shell32.ShellExecuteW(None, "runas", PYW, f'"{PROBE}"', HERE, 0)
    code = int(rc) if rc else 0
    print(f"ShellExecuteW(runas) -> {code}", flush=True)
    if code <= 32:
        print("启动失败或被拒绝（常见：5=拒绝/取消 UAC）。未取得提权结论。", flush=True)
        return 3

    deadline = time.time() + 90
    while time.time() < deadline:
        if before_mtime() > t0 + 0.001:
            break
        time.sleep(1.0)
    else:
        print("等待超时：报告未更新（UAC 未通过？）。", flush=True)
        return 4

    print("\n" + "=" * 78, flush=True)
    with open(OUT, encoding="utf-8") as f:
        print(f.read(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
