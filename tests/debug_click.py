"""诊断：合成点击为何到不了目标窗口 / 低层钩子为何不回调。"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
import harness as H                                       # noqa: E402
from core.logging_setup import setup_logging              # noqa: E402
from detection import coordinate_fallback as cf           # noqa: E402
from leigod.close_protection import MouseClickWatcher     # noqa: E402

log = setup_logging("INFO", console=False)
user32 = ctypes.windll.user32


def button_center(hwnd):
    import uiautomation as auto
    win = auto.ControlFromHandle(hwnd)
    for c, _ in auto.WalkControl(win, maxDepth=6):
        if getattr(c, "Name", "") in ("暂停时长", "开启时长"):
            r = c.BoundingRectangle
            return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    return None


def probe(hwnd, x, y):
    return {
        "fg": cf.user32.GetForegroundWindow(),
        "wfp": cf.window_from_point(x, y),
        "over": cf.is_point_over_window(hwnd, x, y),
    }


def variant(name, force, hook):
    proc, hwnd = H.start_mock("RUNNING")
    time.sleep(0.8)
    m = None
    if hook:
        m = MouseClickWatcher(log)
        print(f"  [{name}] hook={m.start()} {m.error}", flush=True)
    pt = button_center(hwnd)
    if force:
        ok = H.force_foreground(hwnd)
        print(f"  [{name}] force_foreground={ok}", flush=True)
    p = probe(hwnd, *pt)
    print(f"  [{name}] target={pt} fg={hex(p['fg'] or 0)} wfp={hex(p['wfp'] or 0)} "
          f"mock={hex(hwnd)} over={p['over']}", flush=True)
    H.click_at(*pt)
    time.sleep(0.9)
    print(f"  [{name}] events={H.mock_event_names()} state={H.read_mock_state()}", flush=True)
    if m:
        print(f"  [{name}] hook queue={m.pop()}", flush=True)
        m.stop()
    proc.terminate()
    time.sleep(0.6)


print("变体1：不置前台、无钩子", flush=True)
variant("V1", force=False, hook=False)
print("变体2：置前台、无钩子", flush=True)
variant("V2", force=True, hook=False)
print("变体3：置前台、有钩子", flush=True)
variant("V3", force=True, hook=True)
