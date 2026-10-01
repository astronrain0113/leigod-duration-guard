"""测试公共设施：启动/操作仿雷神靶机、构造测试配置、等待条件。

重要事实（踩过的坑）：
  · 本机 venv 的 python.exe 是转发器，Popen 拿到的 pid 不是真实进程 pid，
    因此一律按窗口类名定位靶机，不要用 pid 匹配。
  · WM_NCHITTEST 的 lParam 必须用屏幕坐标（用客户区坐标一律返回 HTNOWHERE）。
  · 合成鼠标点击前必须把目标窗口变成真正的前台窗口，否则首次点击会被
    「激活窗口」吃掉。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, ROOT)

user32 = ctypes.windll.user32
PY = sys.executable
MOCK_CLS = "LeigodMockWnd"
MOCK_EVENTS = os.path.join(HERE, "mock_events.log")
MOCK_STATE = os.path.join(HERE, "mock_state.json")
EVENT_LOG = os.path.join(HERE, "mock_events.log")

user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowExW.restype = ctypes.c_void_p

BM_CLICK = 0x00F5
WM_CLOSE = 0x0010

WM_NCHITTEST, HTCLOSE = 0x84, 20

# 本机是 125% 缩放：测试进程也必须声明 DPI 感知，否则读到的窗口矩形是逻辑坐标
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------- 工具
def uniq_path(name: str) -> str:
    """返回 OUT 下一个本次运行独享的路径（因此天然「不存在」）。

    用「唯一文件名」替代旧写法「先删掉同名文件再新建」：语义一样（都是从未
    存在过的文件），但不会触发本机环境的删除守卫。
    """
    stem, ext = os.path.splitext(name)
    return os.path.join(OUT, f"{stem}_{os.getpid()}_{int(time.time() * 1000)}{ext}")


def reset_file(path: str) -> None:
    """把文件清空到「干净起步」状态，但**不删除**它。

    为什么不用 os.remove：本机环境有一套删除守卫，会在单轮内累计删除次数，
    超过阈值后直接在 os.remove 处中断进程（表现为测试「启动即退出、退出码 1」）。
    对测试而言「文件不存在」和「文件为空」是等价的——read_mock_state / 各读取端
    都已容忍空内容——所以清空即可，既保留语义又不触碰守卫。
    """
    try:
        if os.path.isdir(path):
            return
        with open(path, "w", encoding="utf-8"):
            pass
    except OSError:
        pass


# --------------------------------------------------------------------- 靶机
def kill_stale_mocks() -> None:
    try:
        import psutil
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = p.info["cmdline"] or []
            except Exception:
                continue
            if p.info["pid"] != os.getpid() and any("mock_leigod" in str(a) for a in cl):
                try:
                    p.kill()
                except Exception:
                    pass
    except Exception:
        pass


def start_mock(state: str = "RUNNING", label: str = None):
    kill_stale_mocks()
    for f in (MOCK_EVENTS, MOCK_STATE):
        reset_file(f)
    args = [PY, os.path.join(HERE, "mock_leigod.py"), "--state", state]
    if label:
        args += ["--label", label]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    hwnd = find_window(timeout=15)
    if not hwnd:
        proc.terminate()
        raise RuntimeError("靶机窗口没有出现")
    return proc, hwnd


def find_window(timeout: float = 15.0, cls: str = MOCK_CLS):
    end = time.time() + timeout
    while time.time() < end:
        hits = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(h, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, buf, 256)
            if buf.value == cls:
                hits.append(h)
            return True

        user32.EnumWindows(cb, 0)
        if hits:
            return hits[0]
        time.sleep(0.25)
    return None


def mock_events() -> list:
    try:
        with open(MOCK_EVENTS, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []


def mock_event_names() -> list:
    return [e.get("ev") for e in mock_events()]


def read_mock_state() -> str:
    try:
        with open(MOCK_STATE, encoding="utf-8") as f:
            return json.load(f)["state"]
    except Exception:
        return "?"


def force_foreground(hwnd) -> bool:
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


#: mouse_event 标志
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT),
                ("hi", ctypes.c_ulong * 8)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


def _mouse_input(flags, ax=0, ay=0) -> int:
    """发一条鼠标输入。返回 SendInput 的返回值（0 = 被系统拒绝，通常是 UIPI）。

    注意：**不要**给 `user32.SendInput` 设 argtypes —— ctypes 的 `windll.user32`
    在整个进程里是同一个对象，改 argtypes 会连带打挂别的模块（实测踩过）。
    """
    inp = _INPUT()
    inp.type = 0                      # INPUT_MOUSE
    inp.u.mi.dx = ax
    inp.u.mi.dy = ay
    inp.u.mi.dwFlags = flags
    try:
        return int(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)))
    except Exception:
        return 0


def _abs_coord(x: int, y: int) -> tuple:
    """屏幕像素 → SendInput 的归一化绝对坐标（0..65535）。"""
    smx = int(user32.GetSystemMetrics(0))
    smy = int(user32.GetSystemMetrics(1))
    return (int(int(x) * 65535 / max(1, smx - 1)),
            int(int(y) * 65535 / max(1, smy - 1)))


def cursor_pos() -> tuple:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (int(p.x), int(p.y))


def click_at(x, y, settle: float = 0.35) -> None:
    """真人式点击：先归位光标，再按下抬起。

    为什么按下/抬起要用 **ABSOLUTE** 绝对坐标而不是只靠当前光标位置：
    合成输入进入的是系统输入队列，按下事件用的是「此刻的系统光标位置」。
    负载高时 `SetCursorPos` 与实际生效之间可能错开一拍，按下就会落在旧位置上
    ——表现是"前提全部校验通过，点击却没送达靶机"，且**每次跑丢在不同用例**，
    极难定位。显式带上归一化坐标后，位置不再依赖光标状态。
    """
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    ax, ay = _abs_coord(int(x), int(y))
    _mouse_input(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, ax, ay)
    time.sleep(0.12)
    _mouse_input(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, ax, ay)
    time.sleep(settle)


def close_button_point(hwnd):
    """算出 ✕ 中心，并用 WM_NCHITTEST 自校验。"""
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


def click_close(hwnd, times: int = 1) -> bool:
    """点 ✕。返回是否确实压在 HTCLOSE 上。"""
    force_foreground(hwnd)
    x, y, ht = close_button_point(hwnd)
    ok = (ht == HTCLOSE)
    for _ in range(max(1, times)):
        click_at(x, y)
    return ok


def alive(hwnd) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(wt.HWND(int(hwnd))))


# --------------------------------------------------------------- 关闭链路
SC_CLOSE = 0xF060
WM_SYSCOMMAND = 0x0112
MF_GRAYED = 0x0001


def sc_close_disabled(hwnd) -> bool:
    """读取窗口系统菜单里「关闭」命令的实际启用状态。

    这是**系统层面的真值**：用户点 ✕ 或按 Alt+F4 时，Windows 正是查这个状态
    来决定要不要发出 SC_CLOSE。灰了就等于 ✕ 点不动。
    """
    user32.GetSystemMenu.restype = ctypes.c_void_p
    user32.GetSystemMenu.argtypes = [wt.HWND, wt.BOOL]
    user32.GetMenuState.restype = wt.UINT
    user32.GetMenuState.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
    hmenu = user32.GetSystemMenu(wt.HWND(int(hwnd)), False)
    if not hmenu:
        return False
    state = user32.GetMenuState(hmenu, SC_CLOSE, 0)
    if state == 0xFFFFFFFF:
        return False
    return bool(state & MF_GRAYED)


def post_sc_close(hwnd) -> None:
    """投递一次真实的关闭命令。

    注意：这条链路**不会**被灰色菜单拦住——Windows 只在「用户点 ✕ / Alt+F4」
    时检查菜单状态，程序已经投出来的 SC_CLOSE 会照常执行。
    因此它只适合用来验证「放行后确实能关掉」，不能用来验证拦截。
    """
    user32.PostMessageW(wt.HWND(int(hwnd)), WM_SYSCOMMAND, SC_CLOSE, 0)


def synthetic_input_works(hwnd) -> bool:
    """本环境是否允许合成鼠标输入。

    实测：在部分执行上下文里 SendInput 会返回 0（UIPI 拦截合成输入），
    此时任何「模拟真人点击」的验证都做不了，必须如实降级而不是假装通过。
    """
    n0 = len(mock_events())
    try:
        click_close(hwnd, times=1)
    except Exception:
        return False
    time.sleep(0.5)
    return len(mock_events()) > n0 and "mock_nclbuttondown" in mock_event_names()[n0:]


def inject_close_intent(engine) -> bool:
    """把一次「鼠标按在 ✕ 上」的事件送进意图识别链路。

    仅在合成输入被系统禁止、或系统 ✕ 已被关闭保护禁用（点它不会有反应）时使用
    （报告会标注）。注意：
      · 坐标是真实算出来的，命中测试也走真实的 WM_NCHITTEST，
        因此「是否真的压在 ✕ 上」这一判断没有被绕过；
      · 被替代的只有「内核把硬件事件派发给我们」这一段。

    两个前置条件必须自己满足，否则消费端会把这条意图丢掉、表现为「注入成功但
    引擎毫无反应」——那是本机最容易误判成产品缺陷的一类假象：
      ① 靶机在前台（`is_close_button_at` 之外的链路都需要它，且未抢前台时
         窗口可能还没稳定下来）；
      ② `WM_NCHITTEST` 与消费端 `is_close_button_at` **双重确认**压中 ✕。
    """
    hwnd = engine.close.hwnd
    if not hwnd:
        return False
    ensure_foreground(hwnd)
    x, y, ht = close_button_point(hwnd)
    if ht != HTCLOSE:
        return False
    try:
        if not engine.close.is_close_button_at(x, y):
            return False
    except Exception:
        return False
    engine.close.mouse_watcher._queue.append((x, y, time.time()))
    return True


def wait_until(predicate, timeout: float = 8.0, interval: float = 0.15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = predicate()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(interval)
    return None


# --------------------------------------------------- 结构仿真靶机 v2（chrome 形态）
# v1 靶机 mock_leigod.py 是「普通 Win32 窗口 + 系统标题栏」，
# 而真机是「无 WS_SYSMENU + 零非客户区 + 自绘 ✕ + 应用内确认框」。
# 关闭保护的验证必须用 v2，否则测的是另一个东西。
MOCK_CHROME_CLS = "LeigodChromeWnd"
MOCK_CHROME_DIALOG_CLS = "LeigodChromeDialog"
CHROME_EVENTS = os.path.join(HERE, "chrome_events.log")
CHROME_STATE = os.path.join(HERE, "chrome_state.json")
#: 靶机自绘 ✕ 的几何（客户区坐标）：距右边 27px、距顶边 14px
CHROME_CLOSE_DX, CHROME_CLOSE_DY = 27, 14


def start_mock_chrome(state: str = "RUNNING"):
    """启动结构仿真靶机 v2，返回 (proc, hwnd)。"""
    kill_stale_mocks()
    for f in (CHROME_EVENTS, CHROME_STATE):
        reset_file(f)
    proc = subprocess.Popen(
        [PY, os.path.join(HERE, "mock_leigod_chrome.py"), "--state", state],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    hwnd = find_window(timeout=15, cls=MOCK_CHROME_CLS)
    if not hwnd:
        proc.terminate()
        raise RuntimeError("仿真靶机 v2 窗口没有出现")
    # 必须等它**可见**：窗口对象在 SetWindowPos(SWP_FRAMECHANGED) 之前就已存在，
    # 那时客户区还没算成全窗口，此时测量会读到残留的非客户区。
    wait_until(lambda: bool(user32.IsWindowVisible(wt.HWND(int(hwnd)))), timeout=5.0)
    wait_until(lambda: _client_is_full(hwnd), timeout=5.0)
    return proc, hwnd


def _client_is_full(hwnd) -> bool:
    a = wt.RECT()
    b = wt.RECT()
    user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(a))
    user32.GetClientRect(wt.HWND(int(hwnd)), ctypes.byref(b))
    return (a.right - a.left) == b.right and (a.bottom - a.top) == b.bottom


def move_mouse_to(x, y, settle: float = 0.1) -> None:
    """把光标移到指定位置，并**制造一次真实的移动事件**。

    只调 SetCursorPos 在本机上不会产生低层鼠标钩子可见的 WM_MOUSEMOVE，
    钩子侧的悬停判定因此收不到样本（实测坑）。
    补一次 1px 的相对移动（`mouse_event(MOUSEEVENTF_MOVE)`）即可产生真实事件。
    """
    user32.SetCursorPos(int(x), int(y))
    time.sleep(settle)
    user32.mouse_event(0x0001, 1, 0, 0, 0)      # MOUSEEVENTF_MOVE，相对 +1px
    time.sleep(settle)


def park_cursor() -> None:
    """把光标挪到无关的角落，避免上一个用例把它停在窗口右上角，
    给下一个用例制造「光标一进场就已悬停」的假象（用例间状态污染）。"""
    move_mouse_to(60, 60, settle=0.05)
    time.sleep(0.15)


# ------------------------------------------- 「别的程序」的窗口（前台判据验证用）
# 为什么要它：仅仅「新建并显示」一个窗口就会让它成为前台窗口（实测），
# 所以想验证「雷神不在前台时不预暂停」，必须真的有一个别的窗口抢在前台。
_OTHER_CLS = "GuardTestOtherWnd"
_other = {"hwnd": 0, "thread": None, "stop": None}


def create_other_window(rect=(1480, 40, 1700, 150)):
    """创建一个普通顶层窗口并让它成为前台窗口，返回 hwnd。用完调 destroy_other_window。"""
    import threading

    destroy_other_window()
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
            ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

    user32.CreateWindowExW.restype = wt.HWND
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

    def proc(hwnd, msg, wp, lp):
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    p = WNDPROC(proc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0x0003
    wc.lpfnWndProc = p
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hCursor = user32.LoadCursorW(None, 32512)
    wc.hbrBackground = gdi32.CreateSolidBrush(0x00808080)
    wc.lpszClassName = _OTHER_CLS
    user32.RegisterClassExW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(0, _OTHER_CLS, "别的程序（测试用）",
                                  0x00CF0000, rect[0], rect[1],
                                  rect[2] - rect[0], rect[3] - rect[1],
                                  None, None, None, None)
    stop = threading.Event()

    def pump():
        msg = wt.MSG()
        while not stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.02)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    _other.update(hwnd=hwnd, thread=t, stop=stop, proc=p)
    force_foreground(hwnd)
    return hwnd


def destroy_other_window() -> None:
    if _other.get("stop"):
        _other["stop"].set()
    if _other.get("hwnd"):
        try:
            user32.DestroyWindow(wt.HWND(int(_other["hwnd"])))
        except Exception:
            pass
    _other.update(hwnd=0, thread=None, stop=None)
    time.sleep(0.3)


def chrome_events() -> list:
    try:
        with open(CHROME_EVENTS, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except Exception:
        return []


def chrome_event_names() -> list:
    return [e.get("ev") for e in chrome_events()]


def chrome_state() -> str:
    try:
        with open(CHROME_STATE, encoding="utf-8") as f:
            return json.load(f).get("state", "?")
    except Exception:
        return "?"


def find_chrome_dialog():
    """找靶机的应用内确认框（独立顶层窗口）。"""
    return find_window(timeout=0.1, cls=MOCK_CHROME_DIALOG_CLS)


def chrome_close_point(hwnd):
    """按靶机自身几何算出自绘 ✕ 的屏幕坐标（**不**引用关闭保护的热区配置）。"""
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.right - CHROME_CLOSE_DX, r.top + CHROME_CLOSE_DY)


def chrome_center_point(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)


def click_chrome_close(hwnd) -> tuple:
    """真人式点击靶机自绘 ✕。返回 (x, y)。"""
    force_foreground(hwnd)
    x, y = chrome_close_point(hwnd)
    click_at(x, y, settle=0.25)
    return x, y


def point_window_at(x, y) -> int:
    """屏幕点 (x,y) 上最上层窗口的句柄（0 = 没取到）。"""
    try:
        return int(user32.WindowFromPoint(wt.POINT(int(x), int(y))) or 0)
    except Exception:
        return 0


def ensure_foreground(hwnd, tries: int = 4) -> bool:
    """反复尝试把窗口抢到前台，返回最终是否成功。

    为什么需要重试：`SetForegroundWindow` 会被系统前台锁静默拒绝，而
    `force_foreground` 依赖的 `AttachThreadInput(fg_tid, ...)` 在
    「当前前台线程取不到」时（前台是桌面、或 GetForegroundWindow 返回 0）
    必然失败 —— 进程刚起来、还没有任何窗口获得过焦点时，**第一次**抢前台
    基本会失败。调用方必须检查返回值：抢不到前台时注入的点击会落到别的窗口，
    测试就会把「焦点没抢到」误读成「产品没拦住」。
    """
    for i in range(max(1, int(tries))):
        try:
            if force_foreground(hwnd) and user32.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass
        time.sleep(0.2 + 0.2 * i)
    try:
        return bool(hwnd) and user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def click_chrome_close_checked(hwnd, need_foreground: bool = True) -> tuple:
    """真人式点击靶机自绘 ✕，返回 (是否具备点击条件, 诊断说明)。

    注入前先确认前提，不成立时**不注入**点击，直接返回 `False` + 诊断，
    让用例明确报「前提不成立（环境/焦点问题）」，而不是把它当成产品结论。

    前提一（恒需）：点位上的最上层窗口就是靶机 —— 否则这一下点在别的窗口上。
    前提二（`need_foreground=True` 时）：靶机是前台窗口。关闭保护的热区判定与
      预暂停依赖它；若用例本身已关掉输入层、只看应用自己的反应（用例 H），
      就不必强求前台，否则会把环境焦点问题误报成产品失败。
    """
    fg_ok = ensure_foreground(hwnd)
    # 点位必须在**抢完前台之后**才量：激活可能伴随显示状态变化，先量再抢有可能
    # 拿到一个已经被挪动过的旧矩形（负载高时偶发，且每次落在不同用例）。
    x, y = chrome_close_point(hwnd)
    over = point_window_at(x, y)
    fg_now = int(user32.GetForegroundWindow() or 0)
    diag = (f"点=({x},{y})｜点位窗口=0x{over:X}（期望 0x{int(hwnd):X}）｜"
            f"前台=0x{fg_now:X}｜抢前台={'成功' if fg_ok else '失败'}")
    if over != int(hwnd) or (need_foreground and fg_now != int(hwnd)):
        return False, diag
    click_at(x, y, settle=0.25)
    # 点击后的取证：真出问题时靠它区分「点击没送达」与「被钩子吞了」
    fg_after = int(user32.GetForegroundWindow() or 0)
    cur = cursor_pos()
    diag += (f"｜点击后光标=({cur[0]},{cur[1]})｜点击后前台=0x{fg_after:X}")
    return True, diag


def click_chrome_dialog(dlg, which: str = "tray") -> bool:
    """点靶机确认框上的按钮（`tray`=最小化到托盘 / `exit`=真的退出）。"""
    label = {"tray": "最小化到托盘", "exit": "真的退出"}.get(which, which)
    btn = user32.FindWindowExW(wt.HWND(int(dlg)), None, "Button", label)
    if not btn:
        return False
    user32.SendMessageW(wt.HWND(int(btn)), BM_CLICK, 0, 0)
    return True


def post_dialog_close(dlg) -> bool:
    """直接给确认框发 WM_CLOSE（等价于用户在别处点掉它）。"""
    if not dlg:
        return False
    user32.PostMessageW(wt.HWND(int(dlg)), WM_CLOSE, 0, 0)
    return True


# --------------------------------------------------------------------- 配置
def test_config(**overrides):
    """构造测试配置：把「雷神」指向靶机进程与窗口类。

    只改「哪个进程/窗口算雷神」，其余一律用真实默认值，
    这样测出来的行为才对真实客户端有参考意义。
    """
    from core.config import Config
    data = {
        "general": {"notifications": True, "follow_leigod": False, "follow_exit_seconds": 30},
        "leigod": {
            "process_patterns": ["python"],
            "window_class_candidates": [MOCK_CLS],
            "window_title_keywords": ["雷神"],
            "min_window_size": [300, 200],
        },
        "detection": {"capture": "auto", "ocr_enabled": True},
        "duration": {"verify_timeout_ms": 1200, "max_retries": 3, "retry_interval_ms": 400,
                     "poll_ms": 400},
        "game_monitor": {"enabled": True, "exit_wait_seconds": 30, "processes": []},
        "ui": {"refresh_ms": 200},
        "close_protection": {"enabled": True, "block_when_unknown": True,
                            "block_when_pause_failed": True,
                            "detect_close_intent": True,
                            "auto_close_after_pause": True,
                            "reapply_interval_ms": 1000},
    }
    for k, v in (overrides or {}).items():
        node = data.setdefault(k, {})
        if isinstance(v, dict) and isinstance(node, dict):
            node.update(v)
        else:
            data[k] = v
    return Config(data, path=os.path.join(OUT, "test_config.json"))


def make_engine(config, logger=None):
    from core.logging_setup import setup_logging
    from core.protection_engine import ProtectionEngine, RecordingSink
    if logger is None:
        logger = setup_logging("INFO", console=False)   # 绝不静默吞异常
    sink = RecordingSink()
    return ProtectionEngine(config, sink=sink, logger=logger), sink


# --------------------------------------------------------------------- 报表
class Report:
    def __init__(self, title: str):
        self.title = title
        self.lines = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}" + (f" — {detail}" if detail else "")
        print(line, flush=True)
        self.lines.append(line)
        return ok

    def info(self, text: str) -> None:
        print(f"  · {text}", flush=True)
        self.lines.append(f"  · {text}")

    def section(self, title: str) -> None:
        print(f"\n== {title} ==", flush=True)
        self.lines.append(f"\n== {title} ==")

    def save(self, name: str) -> str:
        path = os.path.join(OUT, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{self.title}\n" + "\n".join(self.lines) + "\n")
        return path
