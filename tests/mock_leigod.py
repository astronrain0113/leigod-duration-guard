"""仿雷神客户端（测试靶机）。

在真实雷神客户端未运行时提供结构与行为等价的测试目标，用于自动化验证：
窗口发现 / UIA 取控件 / 相对坐标点击 / 截图 OCR / 关闭保护 / 关闭意图识别。

与真实客户端的对应关系（依据实测截图 logs/thunder_window.png）：
  顶栏：[logo … 剩余时长] [开启时长|暂停时长] [充值] [≡] [−] [✕]
  RUNNING = 按钮文本「暂停时长」，红底白字
  PAUSED  = 按钮文本「开启时长」，白底深字

运行：python mock_leigod.py [--state RUNNING|PAUSED]
事件记录：mock_events.log     状态发布：mock_state.json
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

# 必须先声明 DPI 感知：否则窗口/坐标被系统按 125% 缩放，与真实客户端（Electron，DPI 感知）不一致
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]

HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_LOG = os.path.join(HERE, "mock_events.log")
STATE_FILE = os.path.join(HERE, "mock_state.json")
ERR_LOG = os.path.join(HERE, "mock_error.log")

WM_CREATE, WM_DESTROY, WM_CLOSE, WM_PAINT = 1, 2, 0x10, 0x0F
WM_DRAWITEM, WM_COMMAND, WM_SYSCOMMAND = 0x2B, 0x111, 0x112
WM_NCLBUTTONDOWN, WM_ERASEBKGND = 0xA1, 0x14
SC_CLOSE = 0xF060

# WM_CREATE 期间 DefWindowProc 需要完整处理，回调签名用 64 位返回
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


CLS = "LeigodMockWnd"
TITLE = "雷神加速器"
BTN_ID = 1001
BTN_LEFT_RATIO = 0.700      # 顶栏按钮左边界（相对客户区宽度）
BTN_TOP, BTN_W, BTN_H = 16, 104, 30

WS_CHILD, WS_VISIBLE, BS_OWNERDRAW = 0x40000000, 0x10000000, 0x0B

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


class MockLeigod:
    def __init__(self, state="RUNNING", fixed_label=None):
        self.state = state
        self.fixed_label = fixed_label     # 非空则按钮文案固定，点击也不改状态（模拟无法识别的界面）
        self.hwnd = None
        self.btn = None
        self.hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)

    def _label(self) -> str:
        if self.fixed_label:
            return self.fixed_label
        return "暂停时长" if self.state == "RUNNING" else "开启时长"

    # ---------------- 绘制 ----------------
    def _draw_btn(self, dis):
        rc = dis.rcItem
        if self.state == "RUNNING":
            bg, txt = 0x00706FD6, 0x00FFFFFF       # RGB(214,111,112) 红底白字
        else:
            bg, txt = 0x00FFFFFF, 0x00202020       # 白底深字
        label = self._label()
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
        oldf = gdi32.SelectObject(dis.hDC, font)
        r = wt.RECT(rc.left, rc.top + 5, rc.right, rc.bottom)
        user32.DrawTextW(dis.hDC, label, -1, ctypes.byref(r), 0x0001 | 0x0004)
        gdi32.SelectObject(dis.hDC, oldf)
        gdi32.DeleteObject(font)

    def _paint(self, hwnd):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rc = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        bar = gdi32.CreateSolidBrush(0x00201E1E)
        user32.FillRect(hdc, ctypes.byref(rc), bar)
        gdi32.DeleteObject(bar)
        gdi32.SetBkMode(hdc, 1)
        font = gdi32.CreateFontW(20, 0, 0, 0, 600, 0, 0, 0, 0, 0, 0, 0, 0, "Microsoft YaHei")
        oldf = gdi32.SelectObject(hdc, font)
        gdi32.SetTextColor(hdc, 0x0000A5FF)
        r = wt.RECT(rc.right - 470, BTN_TOP + 6, rc.right - 290, BTN_TOP + 30)
        user32.DrawTextW(hdc, "1318 时 08 分", -1, ctypes.byref(r), 0x0001)
        gdi32.SetTextColor(hdc, 0x00D0D0D0)
        r2 = wt.RECT(20, BTN_TOP + 6, 320, BTN_TOP + 30)
        user32.DrawTextW(hdc, "雷神加速器  顶级全球游戏加速技术", -1, ctypes.byref(r2), 0x0000)
        gdi32.SelectObject(hdc, oldf)
        gdi32.DeleteObject(font)
        user32.EndPaint(hwnd, ctypes.byref(ps))

    # ---------------- 消息 ----------------
    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            return self._dispatch(hwnd, msg, wp, lp)
        except Exception:
            with open(ERR_LOG, "a", encoding="utf-8") as f:
                f.write(f"--- msg={msg} wp={wp} lp={lp}\n{traceback.format_exc()}\n")
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _dispatch(self, hwnd, msg, wp, lp):
        if msg == WM_CREATE:
            rc = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rc))
            x = int(rc.right * BTN_LEFT_RATIO)
            txt = self._label()
            self.btn = user32.CreateWindowExW(
                0, "BUTTON", txt, WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
                x, BTN_TOP, BTN_W, BTN_H, hwnd, BTN_ID, None, None)
            ev("mock_started", hwnd=hwnd, btn=self.btn, state=self.state,
               title=TITLE, label=txt)
            publish(self.state)
            return 0   # WM_CREATE 必须返回 0，返回 -1 会让系统销毁窗口
        if msg == WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == WM_DRAWITEM:
            self._draw_btn(DRAWITEMSTRUCT.from_address(lp))
            return 1
        if msg == WM_COMMAND:
            if (wp & 0xFFFF) == BTN_ID:
                ev("mock_button_clicked", before=self.state)
                if self.fixed_label:
                    # 文案固定模式：点击不改状态（模拟「点了但状态不变」的客户端）
                    return 0
                self.state = "PAUSED" if self.state == "RUNNING" else "RUNNING"
                user32.SetWindowTextW(self.btn, self._label())
                user32.InvalidateRect(self.btn, None, True)
                publish(self.state)
                ev("mock_state_changed", state=self.state)
            return 0
        if msg == WM_NCLBUTTONDOWN:
            # 只记录命中码，随后必须交给 DefWindowProc：
            # 真实的关闭按钮点击链是 DefWindowProc 收到 HTCLOSE 后才发出 SC_CLOSE，
            # 自己 return 0 会把关闭链掐断，测试就观察不到拦截是否生效。
            ev("mock_nclbuttondown", hit=wp,
               what="HTCLOSE" if wp == 20 else ("HTCAPTION" if wp == 2 else str(wp)))
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        if msg == WM_SYSCOMMAND:
            cmd = wp & 0xFFF0
            ev("mock_syscommand", cmd=hex(cmd))
            # 与真实应用（Electron/Chromium、Qt、tkinter 等）一致：
            # SC_CLOSE 交给 DefWindowProc 处理，由系统决定是否转成 WM_CLOSE。
            # 自实现 DestroyWindow 会绕过系统菜单的启用状态检查，测试就失去意义。
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        if msg == WM_CLOSE:
            ev("mock_wm_close")
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            ev("mock_destroyed")
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0x0003
        wc.lpfnWndProc = self._proc
        wc.hInstance = self.hinst
        wc.hCursor = user32.LoadCursorW(None, 32512)
        wc.hbrBackground = gdi32.CreateSolidBrush(0x00201E1E)
        wc.lpszClassName = CLS
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError()
        self.hwnd = user32.CreateWindowExW(
            0, CLS, TITLE, 0x00CF0000 | 0x10000000,
            120, 90, 1080, 700, None, None, None, None)
        if not self.hwnd:
            raise ctypes.WinError()
        user32.ShowWindow(self.hwnd, 5)
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
    ap.add_argument("--label", default=None,
                    help="强制固定按钮文案（模拟无法识别的界面，如客户端改版）")
    a = ap.parse_args()
    logf = open(EVENT_LOG, "w", encoding="utf-8", buffering=1)
    try:
        return MockLeigod(a.state, fixed_label=a.label).run()
    finally:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
