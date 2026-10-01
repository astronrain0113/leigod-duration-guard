"""真机终局验证：**禁用 SC_CLOSE 到底能不能拦住真实雷神的自绘 ✕**。

背景（见 docs/真机发现-关闭保护架构问题.md）：
  · 已确认：真实窗口 ✕ 由应用自绘（整窗 `WM_NCHITTEST` 一律返回 `HTCLIENT`）
    → 依赖 `HTCLOSE` 的「关闭意图识别」在真机不成立。
  · 已确认：提权后 `EnableMenuItem(SC_CLOSE, MF_GRAYED)` 在**系统层**确实生效
    （`GetMenuState` 变灰），但**自绘 ✕ 不会因此变灰**（它不是系统标题栏按钮，
    外观由网页绘制），所以「看起来没锁」是预期现象。
  · **未知**：自绘 ✕ 的点击是否走 `SC_CLOSE` 通路。Windows 只对
    「系统标题栏按钮 / Alt+F4」检查菜单项状态，故**只能靠真人点一次 ✕** 判定。

⚠️ 第一版测试的教训（务必保留）：
    第一版只盯「窗口是否消失」，**没有验证用户到底点没点**，
    于是把「95 秒后窗口还活着」直接写成 verdict=「拦截生效」——**这是谎报**。
    没有点击证据的存活 = **无法判定**，不是成功。本版用底层鼠标钩子记录真实点击。

安全前置：若总时长不是 PAUSED，一律中止（RUNNING 时若被关掉，正是本项目要防的损失）。

用法：
    python tests/run_elevated.py --wait real_state/close_test.json -- \
        tests/real_close_test.py --mode block --wait 120
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

user32 = ctypes.windll.user32

SC_CLOSE = 0xF060
MF_BYCOMMAND, MF_GRAYED, MF_ENABLED = 0x0, 0x1, 0x0

#: ✕ 热区（相对窗口 rect，物理像素；本机 DPI=120 实测 ✕ 中心距右边约 40px、距顶约 18px）
HOT_W, HOT_H = 90, 60

user32.GetSystemMenu.argtypes = [wt.HWND, wt.BOOL]
user32.GetSystemMenu.restype = ctypes.c_void_p
user32.GetMenuState.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
user32.GetMenuState.restype = wt.UINT
user32.EnableMenuItem.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
user32.EnableMenuItem.restype = wt.UINT
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]

RESULT = os.path.join(ROOT, "real_state", "close_test.json")
_state = {
    "ts_start": time.time(), "phase": "init", "mode": None, "elevated": None,
    "hwnd": 0, "armed": None, "survived": None, "finished": False,
    "elapsed": 0.0,
    "clicks_total": 0, "clicks_in_window": 0, "clicks_on_close": 0,
    "close_click_points": [], "verdict": "", "note": "",
}


def flush():
    try:
        os.makedirs(os.path.dirname(RESULT), exist_ok=True)
        tmp = RESULT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RESULT)
    except OSError:
        pass


def menu_disabled(hwnd):
    hm = user32.GetSystemMenu(wt.HWND(int(hwnd)), False)
    if not hm:
        return None
    st = user32.GetMenuState(ctypes.c_void_p(hm), SC_CLOSE, MF_BYCOMMAND)
    return None if st == 0xFFFFFFFF else bool(st & MF_GRAYED)


def set_menu(hwnd, disabled):
    hm = user32.GetSystemMenu(wt.HWND(int(hwnd)), False)
    if not hm:
        return False, "无系统菜单"
    prev = user32.EnableMenuItem(ctypes.c_void_p(hm), SC_CLOSE,
                                 MF_BYCOMMAND | (MF_GRAYED if disabled else MF_ENABLED))
    now = menu_disabled(hwnd)
    return (prev != 0xFFFFFFFF) and (now == disabled), \
        f"EnableMenuItem 返回=0x{prev:08X} 现在禁用={now}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="block", choices=["block", "none"])
    ap.add_argument("--wait", type=float, default=120.0)
    ap.add_argument("--settle", type=float, default=5.0,
                    help="检测到 ✕ 点击后再观察多久（秒），用于确认窗口是否随后关闭")
    args = ap.parse_args()

    _state["mode"] = args.mode
    _state["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    flush()
    if not _state["elevated"]:
        _state.update(phase="abort", finished=True, verdict="无效",
                      note="未提权，对提权窗口的结论无效（UIPI）")
        flush()
        return 2

    from core.config import load_config
    from core.state_machine import DurationState
    from detection import coordinate_fallback as cf
    from leigod import window as win_mod
    from leigod.close_protection import MouseClickWatcher
    from leigod.duration_detector import DurationDetector

    cfg = load_config()
    win = win_mod.find_main_window(cfg)
    if not win:
        _state.update(phase="abort", finished=True, verdict="无效", note="未找到雷神主窗口")
        flush()
        return 3

    hwnd = win.hwnd
    r = win.rect
    hot = (r[2] - HOT_W, r[0], r[1], r[1] + HOT_H)     # (x_lo, x_hi, y_lo, y_hi)
    _state.update(hwnd=hwnd, hwnd_hex=hex(hwnd), rect=list(r),
                  close_zone=[r[2] - HOT_W, r[1], r[2], r[1] + HOT_H], phase="found")
    flush()

    # ---- 安全前置：必须 PAUSED ----
    det = DurationDetector(cfg)
    # 先置前，让截图护栏放行（elevated 时 PrintWindow 通常已可用，置前是双保险）
    cf.ensure_visible(hwnd)
    cf.activate_window(hwnd)
    time.sleep(0.6)
    reading = det.detect(win, allow_ocr=True)
    _state["precheck_state"] = reading.state.value
    _state["precheck_detail"] = reading.summary()
    flush()
    if reading.state is not DurationState.PAUSED:
        _state.update(phase="abort", finished=True, verdict="中止",
                      note=f"当前为 {reading.state.value}，非 PAUSED，拒绝执行"
                           f"（避免关闭时损失时长）：{reading.summary()}")
        flush()
        return 5

    # ---- 装钩子：记录真实鼠标点击 ----
    watcher = MouseClickWatcher()
    hooked = watcher.start()
    _state["mouse_hook_installed"] = bool(hooked)
    flush()

    if args.mode == "block":
        ok, why = set_menu(hwnd, True)
        _state["armed"] = ok
        _state["arm_detail"] = why
        if not ok:
            watcher.stop()
            _state.update(phase="abort", finished=True, verdict="无效",
                          note=f"未能锁定系统菜单项：{why}")
            flush()
            return 4
    else:
        _state.update(armed=None, arm_detail="对照组：不修改系统菜单")
    _state.update(phase="armed_waiting_click",
                  note="已就绪，等待用户点击雷神右上角的 ✕")
    flush()

    cf.activate_window(hwnd)          # 再置前一次，确保 ✕ 可见可点
    t0 = time.time()
    last_reapply = 0.0
    close_click_at = None
    while time.time() - t0 < args.wait:
        alive = bool(user32.IsWindow(wt.HWND(int(hwnd))))
        _state["elapsed"] = round(time.time() - t0, 1)

        for (x, y, _ts) in watcher.pop():
            _state["clicks_total"] += 1
            if r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                _state["clicks_in_window"] += 1
            if hot[0] <= x <= hot[1] and hot[2] <= y <= hot[3]:
                _state["clicks_on_close"] += 1
                _state["close_click_points"].append([x, y])
                if close_click_at is None:
                    close_click_at = time.time()

        if not alive:
            break
        # 已检测到 ✕ 点击 → 再观察 settle 秒，确认窗口是否随后关闭
        if close_click_at and time.time() - close_click_at > args.settle:
            break
        if args.mode == "block" and time.time() - last_reapply > 2.0:
            set_menu(hwnd, True)
            last_reapply = time.time()
        _state["menu_disabled_now"] = menu_disabled(hwnd)
        flush()
        time.sleep(0.4)

    alive = bool(user32.IsWindow(wt.HWND(int(hwnd))))
    _state["survived"] = alive
    _state["elapsed"] = round(time.time() - t0, 1)

    released = None
    if alive:
        okr, whyr = set_menu(hwnd, False)
        released = okr
        _state["release_detail"] = whyr
    watcher.stop()

    # ---- 结论：绝不在没有点击证据时说「成功」 ----
    if args.mode == "none":
        verdict = ("对照组：未拦截 → 窗口%s" % ("关闭了（符合预期）" if not alive else "仍在（未点到？）"))
    elif _state["clicks_on_close"] == 0:
        verdict = "无法判定：全程未检测到落在 ✕ 上的点击（用户可能没点，或被别的东西挡住）"
    elif alive:
        verdict = "拦截生效：确实点到了 ✕，窗口仍存活"
    else:
        verdict = "拦截无效：确实点到了 ✕，窗口被关闭"
    _state.update(phase="done", finished=True, released=released, verdict=verdict)
    flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        _state.update(phase="error", finished=True, note=f"{type(e).__name__}: {e}")
        flush()
        raise
