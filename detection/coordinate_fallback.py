"""Windows 坐标 / 截图 / 点击底层能力（第三优先级方案的执行层）。

这里集中所有会直接触碰系统 API 的代码，便于单独校验：
  - DPI 感知声明（高缩放屏下坐标必须是物理像素）
  - 窗口激活（SetForegroundWindow 会被前台限制挡掉，需 AttachThreadInput 借权）
  - 鼠标点击（按下→保持→抬起；自定义绘制的界面收不到瞬时点击）
  - 两种截图：PrintWindow（不受遮挡）与屏幕截取（需要前台）
  - 相对坐标计算：只用「相对窗口的比例/偏移」，绝不保存绝对屏幕坐标
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    dwmapi = ctypes.windll.dwmapi
except OSError:      # 理论上不会发生（Vista+ 都有）
    dwmapi = None

user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]


def _declare_prototypes() -> None:
    """显式声明「返回句柄」的 API 原型。

    不声明时 ctypes 默认按 c_int(32 位) 解释返回值，64 位句柄会被**截断**，
    拿到的是个无效句柄。实测后果：SetWindowsHookExW 直接报 126
    (ERROR_MOD_NOT_FOUND)，因为传进去的 hMod 是被截断的模块句柄。
    这类 bug 在句柄数值小的机器上不一定暴露，非常阴险，必须一次性声明清楚。
    """
    k32 = ctypes.windll.kernel32
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    k32.GetCurrentThreadId.restype = wt.DWORD

    u = user32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetParent.restype = ctypes.c_void_p
    u.GetParent.argtypes = [wt.HWND]
    u.GetAncestor.restype = ctypes.c_void_p
    u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    u.WindowFromPoint.restype = ctypes.c_void_p
    u.WindowFromPoint.argtypes = [wt.POINT]
    u.GetSystemMenu.restype = ctypes.c_void_p
    u.GetSystemMenu.argtypes = [wt.HWND, wt.BOOL]
    u.GetMenuState.restype = wt.UINT
    u.GetMenuState.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
    u.EnableMenuItem.restype = wt.UINT
    u.EnableMenuItem.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
    u.DrawMenuBar.restype = wt.BOOL
    u.DrawMenuBar.argtypes = [wt.HWND]
    u.MonitorFromWindow.restype = ctypes.c_void_p
    u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    u.GetMonitorInfoW.restype = wt.BOOL
    u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.IsWindow.restype = wt.BOOL
    u.IsWindow.argtypes = [wt.HWND]
    u.GetWindowThreadProcessId.restype = wt.DWORD
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    u.AttachThreadInput.restype = wt.BOOL
    u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    u.SetForegroundWindow.restype = wt.BOOL
    u.SetForegroundWindow.argtypes = [wt.HWND]
    u.IsIconic.restype = wt.BOOL
    u.IsIconic.argtypes = [wt.HWND]
    u.PrintWindow.restype = wt.BOOL
    u.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    u.ClientToScreen.restype = wt.BOOL
    u.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    u.ScreenToClient.restype = wt.BOOL
    u.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    u.GetDpiForWindow.restype = wt.UINT
    u.GetDpiForWindow.argtypes = [wt.HWND]
    u.GetSystemMetricsForDpi.restype = ctypes.c_int
    u.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wt.UINT]


_declare_prototypes()

DWMWA_EXTENDED_FRAME_BOUNDS = 9
WM_NCHITTEST = 0x0084
HTCLOSE = 20
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
SC_RESTORE = 0xF120
SW_SHOWNORMAL = 1

_dpi_done = False


def set_dpi_aware() -> None:
    """声明 PER_MONITOR_AWARE_V2。必须在任何坐标 API 之前调用一次。

    否则在 125%/150% 缩放屏上 GetWindowRect 与鼠标坐标都是逻辑坐标，
    与实际物理像素差一个比例，点击位置会整体偏移——这正是
    「校准工具能找到位置、主程序却点歪」的根因之一。
    """
    global _dpi_done
    if _dpi_done:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    _dpi_done = True


def get_window_rect(hwnd) -> tuple:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def get_client_rect_screen(hwnd) -> tuple:
    """客户区在屏幕坐标系下的矩形（不含标题栏/边框）。"""
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, pt.x + (r.right - r.left), pt.y + (r.bottom - r.top))


def get_frame_bounds(hwnd) -> tuple:
    """DWM 扩展边框（屏幕上真正可见的窗口范围）。失败时退化为 GetWindowRect。"""
    if dwmapi is not None:
        r = wt.RECT()
        try:
            hr = dwmapi.DwmGetWindowAttribute(wt.HWND(int(hwnd)), ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
                                              ctypes.byref(r), ctypes.sizeof(r))
            if hr == 0 and (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
                return (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
    return get_window_rect(hwnd)


def get_dpi(hwnd) -> int:
    try:
        return int(user32.GetDpiForWindow(wt.HWND(int(hwnd))))
    except Exception:
        return 96


def get_monitor(hwnd) -> dict:
    """返回窗口所在显示器的信息（多显示器适配用）。"""
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                    ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
    try:
        hmon = user32.MonitorFromWindow(wt.HWND(int(hwnd)), 2)  # NEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return {"handle": hmon,
                    "monitor": (mi.rcMonitor.left, mi.rcMonitor.top,
                                mi.rcMonitor.right, mi.rcMonitor.bottom),
                    "work": (mi.rcWork.left, mi.rcWork.top,
                             mi.rcWork.right, mi.rcWork.bottom),
                    "primary": bool(mi.dwFlags & 1)}
    except Exception:
        pass
    return {"handle": 0, "monitor": None, "work": None, "primary": True}


def is_minimized_offscreen(hwnd) -> bool:
    """雷神（Electron）最小化到托盘时会把窗口挪到屏幕外(-25600)，
    但 IsIconic 仍返回 False，必须靠坐标判断。"""
    l, t, _, _ = get_window_rect(hwnd)
    return bool(user32.IsIconic(wt.HWND(int(hwnd)))) or l < -20000 or t < -20000


def ensure_visible(hwnd) -> tuple:
    """把窗口恢复到屏幕可见状态并返回恢复后的矩形。

    这里原先**无条件** `SetForegroundWindow` + `sleep(0.2)`：窗口本来就在前台
    也照睡 200ms。而「确保暂停」一次会调它 1~3 次，光这一项就白送 0.2~0.6 秒，
    正好落在用户抱怨的「点完 ✕ 要等半天」这条路上。现在只在真需要时才动并等待。
    """
    if is_minimized_offscreen(hwnd):
        user32.SendMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)
        time.sleep(0.5)
    try:
        if user32.GetForegroundWindow() != hwnd:
            user32.SetForegroundWindow(wt.HWND(int(hwnd)))
            # 只在前台**刚被改过**时才等它生效；已经在前台就不必等。
            time.sleep(0.12)
    except Exception:
        pass
    return get_window_rect(hwnd)


def activate_window(hwnd) -> bool:
    """让目标窗口成为真正的前景窗口。

    只调 SetForegroundWindow 常被系统前台限制静默拒绝，
    此时用 AttachThreadInput 借用前台线程的输入权限再抢焦点。
    """
    try:
        user32.ShowWindow(wt.HWND(int(hwnd)), SW_SHOWNORMAL)
    except Exception:
        pass
    if user32.GetForegroundWindow() == hwnd:
        return True
    try:
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(wt.HWND(int(fg)), None)
        cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(fg_tid, cur_tid, True)
        user32.SetForegroundWindow(wt.HWND(int(hwnd)))
        user32.BringWindowToTop(wt.HWND(int(hwnd)))
        user32.AttachThreadInput(fg_tid, cur_tid, False)
    except Exception:
        try:
            user32.SetForegroundWindow(wt.HWND(int(hwnd)))
        except Exception:
            pass
    time.sleep(0.2)
    return user32.GetForegroundWindow() == hwnd


def press_click(x: int, y: int, hold_ms: int = 120) -> None:
    """按下 → 保持 → 抬起。保持时长可控，避免瞬时点击被自定义绘制界面丢掉。"""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.SetCursorPos(int(x), int(y))     # 二次移动，确保产生响应式移动消息
    time.sleep(0.05)
    user32.mouse_event(0x0002, int(x), int(y), 0, 0)
    time.sleep(max(0.03, hold_ms / 1000.0))
    user32.mouse_event(0x0004, int(x), int(y), 0, 0)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("mi", _MOUSEINPUT)]


MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
INPUT_MOUSE = 0


def send_left_click(x: int, y: int, marker: int = 0,
                    hold_ms: int = 60, settle_ms: int = 45) -> tuple:
    """用 SendInput 合成一次左键点击，返回 `(是否成功, 说明)`。

    与 `press_click`（用旧的 `mouse_event`）的区别只有一个，但很关键：
    这里把 `marker` 写进 `dwExtraInfo`。关闭保护会**吞掉** ✕ 热区的点击，
    确认已暂停后再把这次点击**重放**出去；重放事件必须能被自己的低层钩子
    认出来并直接放行，否则会形成「吞掉自己重放的点击 → 再重放 → 无限循环」。
    `dwExtraInfo` 是系统原样透传给低层钩子的字段，正是干这个用的。

    UIPI 限制：本进程权限低于目标窗口时，SendInput 会被静默丢弃
    （函数仍返回 1）。因此关闭保护必须提权运行，且调用方要能接受
    「重放可能无效」这一事实——重放失败并不意味着点击丢失，
    只是用户的这一次点击被吞掉了。
    """
    user32.SetCursorPos(int(x), int(y))
    time.sleep(max(0.0, settle_ms / 1000.0))
    user32.SetCursorPos(int(x), int(y))

    def _mk(flag):
        inp = _INPUT(type=INPUT_MOUSE,
                     mi=_MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag,
                                    time=0, dwExtraInfo=ctypes.c_void_p(marker or None)))
        return inp

    down, up = _mk(MOUSEEVENTF_LEFTDOWN), _mk(MOUSEEVENTF_LEFTUP)
    try:
        user32.SendInput.restype = wt.UINT
        user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
    except Exception as e:
        return False, f"SendInput 原型声明失败：{e}"

    # 分两次送：按下 → 保持 hold_ms → 抬起。
    # 一次送两个事件也能形成点击，但保持时长为 0 时，部分自绘界面
    # （含 Chromium 渲染的自定义按钮）会把这次点击判为「误触」而忽略。
    n_down = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_INPUT))
    time.sleep(max(0.02, hold_ms / 1000.0))
    n_up = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUT))
    if n_down != 1 or n_up != 1:
        return False, (f"SendInput 结果 down={n_down} up={n_up}（各期望 1）；"
                       f"错误码 {ctypes.GetLastError()}，通常是权限不足（UIPI）")
    return True, "已重放点击"


def compute_click_point(rect: tuple, ratio=None, pos=None) -> tuple:
    """把「相对窗口的坐标」换算成屏幕坐标。

    ratio=（相对宽高比例，优先，窗口缩放后仍对准）
    pos  =（相对窗口左上角的像素偏移）
    绝不接受绝对屏幕坐标：雷神一移动就失效。
    """
    left, top, right, bottom = rect
    w, h = max(1, right - left), max(1, bottom - top)
    if ratio:
        return int(left + w * ratio[0]), int(top + h * ratio[1])
    if pos:
        return int(left + pos[0]), int(top + pos[1])
    raise ValueError("未提供相对坐标（ratio/pos 至少一个）")


def hit_test(hwnd, x: int, y: int) -> int:
    """对目标窗口发 WM_NCHITTEST。

    实测（tests/probe_feasibility.py [D]）：lParam 必须用**屏幕坐标**，
    用客户区坐标会一律得到 HTNOWHERE(0)。返回 20 即 HTCLOSE。
    """
    lp = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
    return int(user32.SendMessageW(wt.HWND(int(hwnd)), WM_NCHITTEST, 0, lp))


def window_from_point(x: int, y: int):
    pt = wt.POINT(int(x), int(y))
    return user32.WindowFromPoint(pt)


def is_point_over_window(hwnd, x: int, y: int) -> bool:
    """该屏幕点是否落在目标窗口（或其子窗口）上——用于判断截图未被遮挡。"""
    h = window_from_point(x, y)
    if not h:
        return False
    root = user32.GetAncestor(wt.HWND(int(h)), 2)   # GA_ROOT
    return int(h) == int(hwnd) or int(root) == int(hwnd)


def capture_window_printwindow(hwnd):
    """用 PrintWindow 抓窗口自身画面（不被遮挡），返回 RGB numpy 数组或 None。

    PW_RENDERFULLCONTENT=2 用于抓 GPU 渲染窗口（Chromium/Electron 必须）。
    若目标进程权限高于本进程（雷神提权而本程序没有），
    该调用会被 UIPI 拦截并返回黑图，调用方务必用
    image_detection.looks_like_real_capture() 校验后再用。
    """
    import numpy as np
    import win32gui
    import win32ui

    left, top, right, bottom = get_window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        if not user32.PrintWindow(wt.HWND(int(hwnd)), save_dc.GetSafeHdc(), 2):
            return None
        info = bmp.GetInfo()
        buf = bmp.GetBitmapBits(True)
        img = np.frombuffer(buf, dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
        return img[:, :, :3][:, :, ::-1].copy()          # BGRA -> RGB
    except Exception:
        return None
    finally:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def capture_region_screen(rect: tuple):
    """屏幕截取指定区域，返回 RGB numpy 数组或 None（会被上层窗口遮挡）。"""
    import numpy as np
    from PIL import ImageGrab
    try:
        l, t, r, b = [int(v) for v in rect]
        if r <= l or b <= t:
            return None
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
        return np.asarray(img)[:, :, ::-1].copy()
    except Exception:
        return None
