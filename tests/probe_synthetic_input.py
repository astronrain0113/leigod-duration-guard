"""探针：本机到底能不能合成鼠标点击（决定校准验证走真点击还是替身）。

背景：此前观察到 SendInput/mouse_event 被拦（返回 0），若真如此，
任何「模拟真人点击」的验证都做不了，必须如实降级并在报告里标注，
而不是假装点过了。

结论会打印成一行 [结论]，并写入 tests/out/synthetic_input_report.txt。
运行：python tests/probe_synthetic_input.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import harness as H  # noqa: E402

user32 = ctypes.windll.user32


def child_windows(hwnd) -> list:
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(h, buf, 128)
        r = wt.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        out.append((int(h), buf.value, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumChildWindows(wt.HWND(int(hwnd)), cb, 0)
    return out


def main() -> int:
    lines = []
    def say(t):
        print(t, flush=True)
        lines.append(t)

    proc, hwnd = H.start_mock("RUNNING")
    say(f"靶机 HWND={hex(hwnd)} 初始状态={H.read_mock_state()}")
    fg = H.force_foreground(hwnd)
    say(f"force_foreground → {fg}（前台窗口={hex(user32.GetForegroundWindow() or 0)}）")

    kids = child_windows(hwnd)
    say(f"子窗口: {kids}")
    btn = next((k for k in kids if k[1].lower() == "button"), None)
    if not btn:
        say("[结论] 没有找到按钮子窗口，无法继续")
        proc.terminate()
        return 2
    r = btn[2]
    cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
    say(f"按钮 HWND={hex(btn[0])} rect={r} 中心=({cx},{cy})")

    # 1) SetCursorPos 是否成功
    ok_cursor = bool(user32.SetCursorPos(int(cx), int(cy)))
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    say(f"SetCursorPos → {ok_cursor}，实际光标=({p.x},{p.y})")

    # 2) mouse_event 点击
    n0 = len(H.mock_events())
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.12)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.6)
    evs = [e.get("ev") for e in H.mock_events()[n0:]]
    state = H.read_mock_state()
    say(f"mouse_event 点击后：新增事件={evs}  靶机状态={state}")
    mouse_work = "mock_button_clicked" in evs

    # 3) SendMessage(BM_CLICK) 到按钮（替身路径，供校准测试使用）
    if not mouse_work:
        btn_hwnd = btn[0]
        user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        n1 = len(H.mock_events())
        user32.SendMessageW(wt.HWND(btn_hwnd), 0x00F5, 0, 0)   # BM_CLICK
        time.sleep(0.4)
        evs2 = [e.get("ev") for e in H.mock_events()[n1:]]
        say(f"BM_CLICK 后：新增事件={evs2}  靶机状态={H.read_mock_state()}")
        say(f"（BM_CLICK 是否生效: {'mock_button_clicked' in evs2}）")

    if mouse_work:
        say("[结论] 本机合成鼠标点击**可用** → 校准验证可使用真实点击")
    else:
        say("[结论] 本机合成鼠标点击**被系统拦截** → 自动化验证必须用替身注入，"
            "并在报告中如实标注「内核派发环节被替代」")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        pass

    out = os.path.join(H.OUT, "synthetic_input_report.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n报告: {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
