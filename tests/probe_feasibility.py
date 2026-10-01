"""技术可行性验证（诊断脚本，可重复运行）。

验证「不注入、不改二进制、不用 API」前提下关闭保护能否成立：

  A. 定位真实主窗口（HWND / ClassName / Rect / DPI / 多窗口择主）
  B. UI Automation 取「开启时长 / 暂停时长」按钮并直接 Invoke
  C. 按窗口相对坐标点击按钮并改变状态
  D. WM_NCHITTEST 精确判定「鼠标悬停在关闭按钮上」（HTCLOSE）
  E. 禁用系统菜单 SC_CLOSE 后，程序化 SC_CLOSE 与真实点击 ✕ 是否都被吞掉
  F. 恢复 SC_CLOSE 后关闭恢复（证明 E 的拦截真实生效，而非窗口本来关不掉）

全部针对仿雷神靶机（tests/mock_leigod.py）运行，使用与真实客户端完全相同的 API。
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
gdi32 = ctypes.windll.gdi32
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

PY = sys.executable
MOCK = os.path.join(HERE, "mock_leigod.py")
STATE_FILE = os.path.join(HERE, "mock_state.json")
EVENT_LOG = os.path.join(HERE, "mock_events.log")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

WM_NCHITTEST, HTCLOSE, HTCAPTION, HTNOWHERE = 0x84, 20, 2, 0
WM_SYSCOMMAND, SC_CLOSE = 0x112, 0xF060
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
    except Exception as e:
        say(f"[warn] 清理遗留靶机失败: {e}")


def find_mock_window(timeout=15.0, cls="LeigodMockWnd"):
    """按窗口类名定位靶机窗口。

    注意：本机 venv 的 python.exe 是转发器，Popen 拿到的 pid 并非真实进程 pid
    （实测 pid=37600，窗口属主却是 36612），因此不能用 pid 匹配窗口。
    对真实雷神同理：leigod_launcher.exe 会另起 leigod.exe。
    """
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
        time.sleep(0.2)
    return None


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
    time.sleep(0.4)


def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)["state"]
    except Exception:
        return "?"


def hit_test(hwnd, x, y, screen_coords):
    """对窗口发 WM_NCHITTEST，返回命中码。"""
    if screen_coords:
        lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    else:
        pt = wt.POINT(x, y)
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    return user32.SendMessageW(hwnd, WM_NCHITTEST, 0, lp)


def main():
    kill_stale_mocks()
    for f in (STATE_FILE, EVENT_LOG):
        if os.path.exists(f):
            os.remove(f)
    proc = subprocess.Popen([PY, MOCK, "--state", "RUNNING"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    hwnd = find_mock_window()
    if not hwnd:
        say("[FATAL] 靶机窗口未出现")
        proc.terminate()
        return 1
    try:
        # -------- A --------
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        dpi = user32.GetDpiForWindow(hwnd) if hasattr(user32, "GetDpiForWindow") else 0
        say(f"[A] HWND=0x{hwnd:X} PID={proc.pid} Class={buf.value} Title={title.value!r}")
        say(f"[A] Rect=({r.left},{r.top},{r.right},{r.bottom}) "
            f"Size={r.right-r.left}x{r.bottom-r.top} DPI={dpi}")
        say(f"[A] SM_CYCAPTION={user32.GetSystemMetrics(4)} SM_CXSIZE={user32.GetSystemMetrics(30)} "
            f"SM_CYFRAME={user32.GetSystemMetrics(33)}")

        # -------- B --------
        import uiautomation as auto
        btn_rect = None
        win = auto.ControlFromHandle(hwnd)
        rows = []
        for c, _ in auto.WalkControl(win, maxDepth=8):
            try:
                if c.ControlTypeName.endswith("Control") and c.Name:
                    rows.append((c.ControlTypeName, c.Name, getattr(c, "AutomationId", ""),
                                 c.BoundingRectangle, c.IsEnabled))
            except Exception:
                pass
        for t, n, aid, br, en in rows:
            say(f"[B] {t:16s} name={n!r} id={aid!r} "
                f"rect=({br.left},{br.top},{br.right},{br.bottom}) enabled={en}")
        for t, n, aid, br, en in rows:
            if t == "ButtonControl" and n in ("暂停时长", "开启时长"):
                btn_rect = (br.left, br.top, br.right, br.bottom)
        say(f"[B] 按精确文本命中目标按钮: {btn_rect}")

        # -------- B2: UIA Invoke --------
        if btn_rect:
            for c, _ in auto.WalkControl(win, maxDepth=8):
                if getattr(c, "Name", "") == "暂停时长":
                    c.Click(simulateMove=False)
                    break
            time.sleep(0.7)
            say(f"[B2] UIA.Click 后状态={read_state()}（期望 PAUSED）")

        # -------- C: 相对坐标点击 --------
        if btn_rect:
            bx = btn_rect[0] + (btn_rect[2] - btn_rect[0]) // 2
            by = btn_rect[1] + (btn_rect[3] - btn_rect[1]) // 2
            say(f"[C] 按钮中心屏幕坐标=({bx},{by})  相对窗口=({bx-r.left},{by-r.top}) "
                f"比例=({(bx-r.left)/(r.right-r.left):.4f},{(by-r.top)/(r.bottom-r.top):.4f})")
            force_foreground(hwnd)
            before = read_state()
            click_at(bx, by)
            say(f"[C] 相对坐标点击: {before} -> {read_state()}（期望翻转）")

        # -------- D: 关闭按钮命中测试 --------
        force_foreground(hwnd)
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cap, fr = user32.GetSystemMetrics(4), user32.GetSystemMetrics(33)
        bw = user32.GetSystemMetrics(30)
        # 逐个扫描顶栏右侧区域，找出 HTCLOSE 的实际范围
        hits = {}
        for dx in range(2, 90, 3):
            for dy in range(2, cap + 2 * fr, 3):
                x, y = r.right - dx, r.top + dy
                ht = hit_test(hwnd, x, y, screen_coords=False)
                hits.setdefault(ht, []).append((dx, dy))
        for ht, pts in sorted(hits.items()):
            if ht in (HTCLOSE, HTCAPTION, HTNOWHERE):
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                name = {HTCLOSE: "HTCLOSE", HTCAPTION: "HTCAPTION",
                        HTNOWHERE: "HTNOWHERE"}.get(ht, str(ht))
                say(f"[D] 客户坐标命中 {name}: dx={min(xs)}..{max(xs)} dy={min(ys)}..{max(ys)} n={len(pts)}")
            else:
                say(f"[D] 客户坐标命中 {ht}: n={len(pts)}")
        ht_screen = hit_test(hwnd, r.right - 20, r.top + fr + cap // 2, screen_coords=True)
        say(f"[D] 屏幕坐标解释下同一点命中={ht_screen}（若为 {HTCLOSE} 说明 lParam 用屏幕坐标）")

        # -------- E: 禁用 SC_CLOSE --------
        hmenu = user32.GetSystemMenu(hwnd, False)
        prev = user32.EnableMenuItem(hmenu, SC_CLOSE, MF_BYCOMMAND | MF_GRAYED)
        say(f"[E] GetSystemMenu=0x{hmenu:X} EnableMenuItem(GRAYED) 旧状态={prev}（-1=失败）")
        user32.DrawMenuBar(hwnd)
        user32.InvalidateRect(hwnd, None, True)
        time.sleep(0.5)
        user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
        time.sleep(0.9)
        alive1 = bool(user32.IsWindow(hwnd))
        say(f"[E1] 程序化 SC_CLOSE 后窗口存活={alive1}")
        if alive1:
            cx = r.right - fr - bw // 2
            cy = r.top + fr + cap // 2
            say(f"[E2] 真实点击 ✕ 于 ({cx},{cy})；命中码={hit_test(hwnd, cx, cy, False)}")
            force_foreground(hwnd)
            click_at(cx, cy)
            time.sleep(0.7)
        say(f"[E2] 真实点击 ✕ 后窗口存活={bool(user32.IsWindow(hwnd))}")

        # -------- F: 恢复 --------
        if user32.IsWindow(hwnd):
            user32.EnableMenuItem(hmenu, SC_CLOSE, MF_BYCOMMAND | MF_ENABLED)
            user32.DrawMenuBar(hwnd)
            user32.InvalidateRect(hwnd, None, True)
            time.sleep(0.5)
            user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
            time.sleep(1.0)
            say(f"[F] 恢复 SC_CLOSE 后窗口存活={bool(user32.IsWindow(hwnd))}（期望 False=正常关闭）")
        else:
            say("[F] 跳过：窗口已关闭，E 的拦截未生效")

        with open(os.path.join(OUT, "feasibility_report.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(report))
        return 0
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
