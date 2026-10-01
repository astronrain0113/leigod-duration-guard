"""调试：为什么合成的 ✕ 点击没有触发关闭。"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
user32 = ctypes.windll.user32
EV = os.path.join(HERE, "mock_events.log")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong


def events():
    try:
        return [json.loads(l) for l in open(EV, encoding="utf-8") if l.strip()]
    except Exception:
        return []


def find(timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        hits = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(h, _):
            b = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, b, 256)
            if b.value == "LeigodMockWnd":
                hits.append(h)
            return True

        user32.EnumWindows(cb, 0)
        if hits:
            return hits[0]
        time.sleep(0.3)
    return None


if os.path.exists(EV):
    os.remove(EV)
p = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_leigod.py"), "--state", "PAUSED"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
hwnd = find()
if not hwnd:
    print("mock 未出现")
    sys.exit(1)
time.sleep(1)
r = wt.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(r))
dpi = user32.GetDpiForWindow(hwnd)
cap = user32.GetSystemMetricsForDpi(4, dpi)
fr = user32.GetSystemMetricsForDpi(33, dpi)
bw = user32.GetSystemMetricsForDpi(30, dpi)
x, y = r.right - fr - bw // 2, r.top + fr + cap // 2
print("hwnd", hex(hwnd), "rect", (r.left, r.top, r.right, r.bottom), "dpi", dpi, "x,y", x, y)

user32.ShowWindow(hwnd, 5)
fg = user32.GetForegroundWindow()
cur = ctypes.windll.kernel32.GetCurrentThreadId()
tgt = user32.GetWindowThreadProcessId(fg, None)
user32.AttachThreadInput(tgt, cur, True)
user32.SetForegroundWindow(hwnd)
user32.BringWindowToTop(hwnd)
user32.AttachThreadInput(tgt, cur, False)
time.sleep(0.5)
print("foreground==mock ?", user32.GetForegroundWindow() == hwnd)
under = user32.WindowFromPoint(wt.POINT(x, y))
print("WindowFromPoint", hex(under), "root", hex(user32.GetAncestor(under, 2)), "mock", hex(hwnd))
lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
print("WM_NCHITTEST ->", user32.SendMessageW(hwnd, 0x84, 0, lp))

n0 = len(events())
for i in range(3):
    user32.SetCursorPos(x, y)
    time.sleep(0.25)
    user32.mouse_event(0x0002, x, y, 0, 0)
    time.sleep(0.15)
    user32.mouse_event(0x0004, x, y, 0, 0)
    time.sleep(0.6)
    print(f"click{i} alive={bool(user32.IsWindow(hwnd))} new={[e['ev'] for e in events()[n0:]]}")

for label, msg, wp in [("SC_CLOSE", 0x112, 0xF060),
                       ("WM_CLOSE", 0x10, 0),
                       ("NCLBUTTONDOWN/HTCLOSE", 0xA1, 20),
                       ("NCLBUTTONDOWN/HTCAPTION", 0xA1, 2)]:
    if not user32.IsWindow(hwnd):
        print(f"{label}: 窗口已关闭，跳过")
        continue
    n0 = len(events())
    user32.PostMessageW(hwnd, msg, wp, lp)
    time.sleep(0.8)
    print(f"{label} alive={bool(user32.IsWindow(hwnd))} new={[e['ev'] for e in events()[n0:]]}")

p.terminate()
