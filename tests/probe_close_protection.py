"""关闭保护机制验证：三种候选方案谁真能拦住「点 ✕」。

背景：拦截别的进程的关闭窗口，在「不注入 DLL、不改二进制」的约束下只有
少数几条路可走。本脚本逐一实测，用真实鼠标点击 ✕ 判定成败。

方案 1  禁用系统菜单项：GetSystemMenu + EnableMenuItem(SC_CLOSE, MF_GRAYED)
方案 2  去掉窗口 WS_SYSMENU 样式：SetWindowLongW + SWP_FRAMECHANGED
方案 3  对照组：不做任何处理（必须能关掉，否则说明测试无效）

判定标准（全部用真实鼠标点击，并对照 mock_events.log）：
  拦截成功 = 点击 ✕ 后窗口仍存活，且靶机没有收到 WM_CLOSE
  拦截失败 = 窗口消失，或靶机收到 WM_CLOSE
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
user32 = ctypes.windll.user32
PY = sys.executable
MOCK = os.path.join(HERE, "mock_leigod.py")
EVENT_LOG = os.path.join(HERE, "mock_events.log")

user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW = user32.SetWindowLongPtrW
user32.SetWindowLongPtrW.restype = ctypes.c_longlong

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

WM_NCHITTEST, HTCLOSE = 0x84, 20
WM_SYSCOMMAND, SC_CLOSE = 0x112, 0xF060
GWL_STYLE, WS_SYSMENU = -16, 0x00080000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x20
MF_BYCOMMAND, MF_GRAYED, MF_ENABLED = 0x0, 0x1, 0x0

report = []


def say(s):
    print(s, flush=True)
    report.append(s)


def kill_stale_mocks():
    try:
        import psutil
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = p.info["cmdline"] or []
            except Exception:
                continue
            if p.info["pid"] != os.getpid() and any("mock_leigod" in str(a) for a in cl):
                p.kill()
    except Exception:
        pass


def find_mock(timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        hits = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(h, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, buf, 256)
            if buf.value == "LeigodMockWnd":
                hits.append(h)
            return True

        user32.EnumWindows(cb, 0)
        if hits:
            return hits[0]
        time.sleep(0.2)
    return None


def events():
    try:
        with open(EVENT_LOG, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []


def saw(event_name, since_index):
    return any(e["ev"] == event_name for e in events()[since_index:])


def force_foreground(hwnd):
    user32.ShowWindow(hwnd, 5)
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    tgt = user32.GetWindowThreadProcessId(fg, None)
    user32.AttachThreadInput(tgt, cur, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(tgt, cur, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def click_at(x, y):
    user32.SetCursorPos(x, y)
    time.sleep(0.15)
    user32.mouse_event(0x0002, x, y, 0, 0)
    time.sleep(0.12)
    user32.mouse_event(0x0004, x, y, 0, 0)
    time.sleep(0.5)


def close_button_point(hwnd):
    """按 DWM/系统度量算 ✕ 中心，并用 WM_NCHITTEST 自校验（必须是 HTCLOSE）。"""
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    dpi = user32.GetDpiForWindow(hwnd)
    cap = user32.GetSystemMetricsForDpi(4, dpi)
    fr = user32.GetSystemMetricsForDpi(33, dpi)
    bw = user32.GetSystemMetricsForDpi(30, dpi)
    x, y = r.right - fr - bw // 2, r.top + fr + cap // 2
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    ht = user32.SendMessageW(hwnd, WM_NCHITTEST, 0, lp)
    return x, y, ht


def trial(title, setup):
    kill_stale_mocks()
    if os.path.exists(EVENT_LOG):
        os.remove(EVENT_LOG)
    proc = subprocess.Popen([PY, MOCK, "--state", "PAUSED"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        hwnd = find_mock()
        if not hwnd:
            say(f"[{title}] 靶机未出现，跳过")
            return
        time.sleep(0.8)
        idx = len(events())
        setup(hwnd)
        time.sleep(0.4)
        force_foreground(hwnd)
        x, y, ht = close_button_point(hwnd)
        say(f"[{title}] 点击 ✕ 于 ({x},{y}) 命中码={ht}"
            f"{'（HTCLOSE ✓）' if ht == HTCLOSE else '（非 HTCLOSE，坐标算法需修正）'}")
        click_at(x, y)
        time.sleep(0.6)
        alive = bool(user32.IsWindow(hwnd))
        got_close = saw("mock_wm_close", idx)
        got_destroy = saw("mock_destroyed", idx)
        say(f"[{title}] 结果: 窗口存活={alive} 收到WM_CLOSE={got_close} 已销毁={got_destroy}"
            f"  → {'拦截成功' if alive and not got_close else '拦截失败'}")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        time.sleep(0.5)


def setup_none(hwnd):
    pass


def setup_grayed(hwnd):
    hmenu = user32.GetSystemMenu(hwnd, False)
    prev = user32.EnableMenuItem(hmenu, SC_CLOSE, MF_BYCOMMAND | MF_GRAYED)
    say(f"[灰色菜单] GetSystemMenu=0x{hmenu:X} 原启用状态={prev}")
    user32.DrawMenuBar(hwnd)
    user32.InvalidateRect(hwnd, None, True)


def setup_no_sysmenu(hwnd):
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    new = style & ~WS_SYSMENU
    ok = user32.SetWindowLongPtrW(hwnd, GWL_STYLE, new)
    say(f"[去掉SYSMENU] style 0x{style & 0xFFFFFFFF:X} -> 0x{new & 0xFFFFFFFF:X} "
        f"旧值=0x{ok & 0xFFFFFFFF:X} 调用成功={bool(ok)}")
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
    user32.InvalidateRect(hwnd, None, True)


def main():
    say("=" * 72)
    say("关闭保护机制验证（真实鼠标点击 ✕）")
    say("=" * 72)
    trial("方案3-对照组", setup_none)
    trial("方案1-灰色SC_CLOSE", setup_grayed)
    trial("方案2-去掉SYSMENU", setup_no_sysmenu)
    with open(os.path.join(OUT, "close_protection_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
