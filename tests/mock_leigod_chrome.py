"""结构仿真靶机 v2 —— 复刻真实雷神（Electron）的**关闭相关结构**。

## 为什么需要它（这是本项目最重要的一次教训）

v1 靶机 `mock_leigod.py` 是「普通 Win32 窗口 + 系统标题栏」：
`WS_SYSMENU` 在、`WM_NCHITTEST` 对 ✕ 返回 `HTCLOSE(20)`、点 ✕ 直接 `WM_CLOSE`。
所有关闭保护测试在它上面全绿，**但在真机上完全失效**。

真机（Leigod v11.3.2.9，证据见 `tests/out/real_close_caps_admin.txt`）：

| 项 | 真机 | v1 靶机 |
| --- | --- | --- |
| 窗口样式 | `0x14C20000`（**无** `WS_SYSMENU`） | `0x00CF0000`（有） |
| 非客户区 | **没有**（`GetWindowRect == GetClientRect`） | 有（系统标题栏） |
| ✕ 归属 | 应用自绘，`WM_NCHITTEST` 全窗 `HTCLIENT(1)` | 系统自绘，返回 `HTCLOSE` |
| 点 ✕ 的后果 | 弹**应用内确认框**，窗口不关 | 直接 `WM_CLOSE` → 销毁 |
| 确认框 | 「最小化到托盘」/「真的退出」 | 无 |

**靶机结构与真机不一致，自动化结论就没有意义。** 本文件按上表右侧改为左侧。

## 与真机的对应关系

- `WS_CAPTION|WS_BORDER|WS_DLGFRAME|WS_MINIMIZEBOX`，**去掉** `WS_SYSMENU`；
- 处理 `WM_NCCALCSIZE` 返回 0 → 非客户区为零（Chromium 的标准做法）；
- `WM_NCHITTEST` 一律返回 `HTCLIENT(1)`；
- 客户区左上角自绘标题栏，右侧自绘 `≡  —  ✕`；✕ 中心固定在
  **距右边 27px、距顶边 14px**（与真机实测的 27px / 18px 同量级，
  刻意**不**去读关闭保护的热区配置，避免测试变成自证）；
- 点 ✕ → 打开确认框；选「真的退出」才销毁窗口；选「最小化到托盘」则把窗口
  挪到 `(-25600,-25600)`（真机 Electron 的最小化到托盘行为）。

## 用法

    python tests/mock_leigod_chrome.py [--state RUNNING|PAUSED] [--title 雷神加速器]
报警/事件：`tests/chrome_events.log`        状态：`tests/chrome_state.json`
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time
import traceback

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

# 句柄类返回值必须显式声明成 64 位，否则 ctypes 按 c_int 截断，
# 拿到的是无效句柄（本项目在 SetWindowsHookExW 上已经踩过一次）。
# 只声明 restype 与少数必须的 argtypes，避免依赖 wintypes 里并不存在的别名
# （如 HMENU / HINSTANCE 在不同 Python 版本上并不一致）。
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterClassExW.restype = wt.WORD
user32.SendMessageW.restype = ctypes.c_longlong
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadCursorW.restype = wt.HANDLE

HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_LOG = os.path.join(HERE, "chrome_events.log")
STATE_FILE = os.path.join(HERE, "chrome_state.json")
ERR_LOG = os.path.join(HERE, "chrome_error.log")

# ---- 消息常量 ----
WM_CREATE, WM_DESTROY, WM_CLOSE, WM_PAINT = 0x0001, 0x0002, 0x0010, 0x000F
WM_NCCALCSIZE, WM_NCHITTEST, WM_LBUTTONDOWN = 0x0083, 0x0084, 0x0201
WM_DRAWITEM, WM_COMMAND, WM_SYSCOMMAND = 0x002B, 0x0111, 0x0112
WM_ERASEBKGND, WM_SIZE = 0x0014, 0x0005
SC_CLOSE, SC_MINIMIZE = 0xF060, 0xF020
HTCLIENT = 1

WS_CHILD, WS_VISIBLE, BS_OWNERDRAW = 0x40000000, 0x10000000, 0x0B
WS_POPUP, WS_CAPTION, WS_BORDER, WS_DLGFRAME, WS_MINIMIZEBOX, WS_CLIPSIBLINGS = (
    0x80000000, 0x00C00000, 0x00800000, 0x00400000, 0x00020000, 0x04000000)
#: 真机实测样式（无 WS_SYSMENU / WS_THICKFRAME / WS_MAXIMIZEBOX）
CHROME_STYLE = WS_VISIBLE | WS_CLIPSIBLINGS | WS_CAPTION | WS_BORDER | WS_DLGFRAME | WS_MINIMIZEBOX

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [("CtlType", wt.UINT), ("CtlID", wt.UINT), ("itemID", wt.UINT),
                ("itemAction", wt.UINT), ("itemState", wt.UINT), ("hwndItem", wt.HWND),
                ("hDC", wt.HDC), ("rcItem", wt.RECT), ("itemData", ctypes.c_void_p)]


CLS_MAIN = "LeigodChromeWnd"
CLS_DIALOG = "LeigodChromeDialog"
DEFAULT_TITLE = "雷神加速器"

BTN_ID = 1001                 # 暂停/开启按钮（供检测与暂停链路使用）
DLG_TRAY_ID, DLG_EXIT_ID = 2001, 2002

BAR_H = 30                    # 自绘标题栏高度
#: ✕ 中心距右边 / 距顶边（物理像素）——与真机实测同量级，**不引用关闭保护的配置**
CLOSE_DX, CLOSE_DY = 27, 14
BTN_W, BTN_H, GLYPH_GAP = 34, 22, 34

TRAY_X, TRAY_Y = -25600, -25600

logf = None


def ev(name, **kw):
    line = json.dumps({"t": round(time.time(), 3), "ev": name, **kw}, ensure_ascii=False)
    print(line, flush=True)
    if logf:
        logf.write(line + "\n")
        logf.flush()


def publish(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"state": state, "pid": os.getpid(), "ts": time.time()}, f)
    except OSError:
        pass


def _mk_proc(fn):
    return WNDPROC(fn)


class ChromeMock:
    """无系统标题栏 + 自绘 ✕ + 应用内确认框的仿真客户端。"""

    def __init__(self, state="RUNNING", title=DEFAULT_TITLE):
        self.state = state
        self.title = title
        self.hwnd = None
        self.dialog = None
        self.btn = None
        self.dlg_btns = {}
        self.hinst = kernel32.GetModuleHandleW(None)
        self._proc = _mk_proc(self._wndproc)
        self._dlg_proc = _mk_proc(self._dlg_wndproc)
        self.reg_main = False
        self.reg_dlg = False

    # ------------------------------------------------------------- 几何
    def client(self, hwnd) -> tuple:
        rc = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        return rc.left, rc.top, rc.right, rc.bottom

    def close_rect(self, hwnd) -> tuple:
        """自绘 ✕ 的点击热区（客户区坐标）。"""
        l, t, r, b = self.client(hwnd)
        cx, cy = r - CLOSE_DX, t + CLOSE_DY
        return (cx - 16, cy - 14, cx + 16, cy + 14)

    def min_rect(self, hwnd) -> tuple:
        l, t, r, b = self.client(hwnd)
        cx, cy = r - CLOSE_DX - GLYPH_GAP, t + CLOSE_DY
        return (cx - 16, cy - 14, cx + 16, cy + 14)

    def menu_rect(self, hwnd) -> tuple:
        l, t, r, b = self.client(hwnd)
        cx, cy = r - CLOSE_DX - GLYPH_GAP * 2, t + CLOSE_DY
        return (cx - 16, cy - 14, cx + 16, cy + 14)

    # ------------------------------------------------------------- 绘制
    def _paint_bar(self, hwnd, hdc):
        l, t, r, b = self.client(hwnd)
        bar = gdi32.CreateSolidBrush(0x00201E1E)
        rc = wt.RECT(l, t, r, t + BAR_H)
        user32.FillRect(hdc, ctypes.byref(rc), bar)
        gdi32.DeleteObject(bar)
        gdi32.SetBkMode(hdc, 1)

        font = gdi32.CreateFontW(15, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, "Microsoft YaHei")
        old = gdi32.SelectObject(hdc, font)
        gdi32.SetTextColor(hdc, 0x00D0D0D0)
        user32.DrawTextW(hdc, "≡", -1, ctypes.byref(self._rc(self.menu_rect(hwnd))), 0x0001 | 0x0004)
        user32.DrawTextW(hdc, "—", -1, ctypes.byref(self._rc(self.min_rect(hwnd))), 0x0001 | 0x0004)
        gdi32.SetTextColor(hdc, 0x00FFFFFF)
        user32.DrawTextW(hdc, "✕", -1, ctypes.byref(self._rc(self.close_rect(hwnd))), 0x0001 | 0x0004)
        gdi32.TextOutW(hdc, l + 16, t + 7, "雷神加速器  自绘标题栏（仿真）", 18)
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(font)

    @staticmethod
    def _rc(rect):
        return wt.RECT(*rect)

    def _draw_ownerdraw(self, dis):
        rc = dis.rcItem
        if self.state == "RUNNING":
            bg, txt = 0x00706FD6, 0x00FFFFFF
        else:
            bg, txt = 0x00FFFFFF, 0x00202020
        brush = gdi32.CreateSolidBrush(bg)
        user32.FillRect(dis.hDC, ctypes.byref(rc), brush)
        gdi32.DeleteObject(brush)
        pen = gdi32.CreatePen(0, 1, 0x00A0A0A0)
        old = gdi32.SelectObject(dis.hDC, pen)
        gdi32.Rectangle(dis.hDC, rc.left, rc.top, rc.right, rc.bottom)
        gdi32.SelectObject(dis.hDC, old)
        gdi32.DeleteObject(pen)
        gdi32.SetBkMode(dis.hDC, 1)
        gdi32.SetTextColor(dis.hDC, txt)
        font = gdi32.CreateFontW(17, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, "Microsoft YaHei")
        of = gdi32.SelectObject(dis.hDC, font)
        r = wt.RECT(rc.left, rc.top + 5, rc.right, rc.bottom)
        user32.DrawTextW(dis.hDC, self._label(), -1, ctypes.byref(r), 0x0001 | 0x0004)
        gdi32.SelectObject(dis.hDC, of)
        gdi32.DeleteObject(font)

    def _label(self):
        return "暂停时长" if self.state == "RUNNING" else "开启时长"

    # ------------------------------------------------------------- 主窗口
    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            return self._dispatch(hwnd, msg, wp, lp)
        except Exception:
            with open(ERR_LOG, "a", encoding="utf-8") as f:
                f.write(f"--- msg={msg} wp={wp} lp={lp}\n{traceback.format_exc()}\n")
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _dispatch(self, hwnd, msg, wp, lp):
        if msg == WM_NCCALCSIZE:
            # Chromium 的标准做法：让非客户区为零（保留 WS_CAPTION 但不画系统标题栏）。
            # 真机 `GetWindowRect == GetClientRect` 就是这么来的。
            if wp:
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        if msg == WM_NCHITTEST:
            # 真机实测：整个窗口一律 HTCLIENT（✕ 由渲染进程处理，系统不知道有 ✕）
            return HTCLIENT
        if msg == WM_CREATE:
            l, t, r, b = self.client(hwnd)
            self.btn = user32.CreateWindowExW(
                0, "BUTTON", self._label(), WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                int(r * 0.70), 46, 104, 30, hwnd, BTN_ID, None, None)
            ev("chrome_started", hwnd=hwnd, style=hex(CHROME_STYLE),
               title=self.title, state=self.state)
            publish(self.state)
            return 0
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            rc = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            bg = gdi32.CreateSolidBrush(0x00302C2C)
            user32.FillRect(hdc, ctypes.byref(rc), bg)
            gdi32.DeleteObject(bg)
            self._paint_bar(hwnd, hdc)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_DRAWITEM:
            self._draw_ownerdraw(DRAWITEMSTRUCT.from_address(lp))
            return 1
        if msg == WM_COMMAND:
            if (wp & 0xFFFF) == BTN_ID:
                ev("chrome_button_clicked", before=self.state)
                self.state = "PAUSED" if self.state == "RUNNING" else "RUNNING"
                user32.SetWindowTextW(self.btn, self._label())
                user32.InvalidateRect(self.btn, None, True)
                publish(self.state)
                ev("chrome_state_changed", state=self.state)
            return 0
        if msg == WM_LBUTTONDOWN:
            x = ctypes.c_short(lp & 0xFFFF).value
            y = ctypes.c_short((lp >> 16) & 0xFFFF).value
            return self._on_click(x, y)
        if msg == WM_SYSCOMMAND:
            cmd = wp & 0xFFF0
            ev("chrome_syscommand", cmd=hex(cmd))
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        if msg == WM_CLOSE:
            # 程序化关闭/Alt+F4（放行后确实会走到这里）
            ev("chrome_wm_close")
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            ev("chrome_destroyed")
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _on_click(self, x, y):
        cx = self.close_rect(self.hwnd)
        mn = self.min_rect(self.hwnd)
        me = self.menu_rect(self.hwnd)
        if cx[0] <= x <= cx[2] and cx[1] <= y <= cx[3]:
            ev("chrome_close_clicked", x=x, y=y)
            self.open_confirm()
            return 0
        if mn[0] <= x <= mn[2] and mn[1] <= y <= mn[3]:
            ev("chrome_minimize_clicked")
            user32.ShowWindow(self.hwnd, 6)     # SW_MINIMIZE
            return 0
        if me[0] <= x <= me[2] and me[1] <= y <= me[3]:
            ev("chrome_menu_clicked")
            return 0
        return user32.DefWindowProcW(self.hwnd, WM_LBUTTONDOWN, 0, (y << 16) | x)

    # ------------------------------------------------------------- 确认框
    def open_confirm(self):
        if self.dialog:
            return
        self._register_dlg()
        # 确认框也做成无系统标题栏的自绘窗口（与 Electron 自绘弹层一致）
        self.dialog = user32.CreateWindowExW(
            0x00000008,                  # WS_EX_TOPMOST
            CLS_DIALOG, "提示", WS_POPUP | WS_VISIBLE | WS_BORDER,
            0, 0, 380, 170, self.hwnd, None, None, None)
        if not self.dialog:
            ev("chrome_dialog_error", err=ctypes.GetLastError())
            return
        mr = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(mr))
        x = mr.left + ((mr.right - mr.left) - 380) // 2
        y = mr.top + ((mr.bottom - mr.top) - 170) // 2
        user32.SetWindowPos(self.dialog, None, x, y, 380, 170, 0x0040)   # SWP_SHOWWINDOW
        user32.UpdateWindow(self.dialog)
        ev("chrome_dialog_opened", dialog=self.dialog, rect=[x, y, x + 380, y + 170])

    def _register_dlg(self):
        if self.reg_dlg:
            return
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0x0003
        wc.lpfnWndProc = self._dlg_proc
        wc.hInstance = self.hinst
        wc.hCursor = user32.LoadCursorW(None, 32512)
        wc.hbrBackground = gdi32.CreateSolidBrush(0x00F0F0F0)
        wc.lpszClassName = CLS_DIALOG
        user32.RegisterClassExW(ctypes.byref(wc))
        self.reg_dlg = True

    def _dlg_wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_CREATE:
                self.dlg_btns["tray"] = user32.CreateWindowExW(
                    0, "BUTTON", "最小化到托盘", WS_CHILD | WS_VISIBLE,
                    40, 100, 130, 34, hwnd, DLG_TRAY_ID, None, None)
                self.dlg_btns["exit"] = user32.CreateWindowExW(
                    0, "BUTTON", "真的退出", WS_CHILD | WS_VISIBLE,
                    210, 100, 130, 34, hwnd, DLG_EXIT_ID, None, None)
                return 0
            if msg == WM_COMMAND:
                cid = wp & 0xFFFF
                if cid == DLG_TRAY_ID:
                    ev("chrome_choose_tray")
                    self._close_confirm()
                    self._to_tray()
                    return 0
                if cid == DLG_EXIT_ID:
                    ev("chrome_choose_exit")
                    self._close_confirm()
                    user32.DestroyWindow(self.hwnd)
                    return 0
            if msg == WM_CLOSE:
                ev("chrome_dialog_dismissed")
                self._close_confirm()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        except Exception:
            with open(ERR_LOG, "a", encoding="utf-8") as f:
                f.write(f"--- dialog msg={msg}\n{traceback.format_exc()}\n")
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _close_confirm(self):
        if self.dialog:
            d, self.dialog = self.dialog, None
            self.dlg_btns = {}
            user32.DestroyWindow(d)

    def _to_tray(self):
        # 真机 Electron 最小化到托盘：窗口被挪到屏幕外，IsIconic 仍为 False
        user32.SetWindowPos(self.hwnd, None, TRAY_X, TRAY_Y, 0, 0,
                            0x0001 | 0x0004 | 0x0010 | 0x0040)
        ev("chrome_tray", rect=[TRAY_X, TRAY_Y])
        publish("TRAY")

    # ------------------------------------------------------------- 运行
    def run(self):
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0x0003
        wc.lpfnWndProc = self._proc
        wc.hInstance = self.hinst
        wc.hCursor = user32.LoadCursorW(None, 32512)
        wc.hbrBackground = gdi32.CreateSolidBrush(0x00302C2C)
        wc.lpszClassName = CLS_MAIN
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError()
        self.hwnd = user32.CreateWindowExW(
            0, CLS_MAIN, self.title, CHROME_STYLE & ~WS_VISIBLE, 240, 160, 1080, 700,
            None, None, None, None)
        if not self.hwnd:
            raise ctypes.WinError()
        # 关键一步，也是 Chromium 的真实做法：
        # WM_NCCALCSIZE 只在 **SetWindowPos(SWP_FRAMECHANGED)** 之后才会以 wParam=TRUE
        # 发来（创建/显示时只收到 FALSE 通知）。少了这一句，窗口会残留系统客户区
        # （实测 1080x700 的窗口客户区只有 1062x653），与真机
        # 「GetWindowRect == GetClientRect」不符，仿真就失真了。
        # 顺序也很重要：**先** FRAMECHANGED 再显示，否则测试可能在窗口刚可见、
        # 客户区还没算好的那一瞬间去测量，读到 1062x653 而误判。
        user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                            0x0027)          # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
        user32.ShowWindow(self.hwnd, 5)      # SW_SHOW
        user32.UpdateWindow(self.hwnd)
        ev("window_created", hwnd=self.hwnd)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return 0


def main():
    global logf
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="RUNNING", choices=["RUNNING", "PAUSED"])
    ap.add_argument("--title", default=DEFAULT_TITLE)
    a = ap.parse_args()
    logf = open(EVENT_LOG, "w", encoding="utf-8", buffering=1)
    try:
        return ChromeMock(a.state, a.title).run()
    finally:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
