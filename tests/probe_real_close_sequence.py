"""真机只读观测：点 ✕ 之后，雷神到底创建了什么？（关闭保护 v2 层级3 的设计输入）

## 为什么需要它

用户实测已确认：点 ✕ 不关窗，而是弹出雷神自己的确认框（「最小化到托盘」/「真的退出」）。
但**确认框是什么形态**还不知道，而这决定了「层级3 确认框监测」怎么实现：

  · 若它是**独立顶层窗口**（有 HWND、属 leigod pid）→ 可以用 `SetWinEventHook`
    实时发现，甚至可能用 UIA 找到「真的退出」按钮；
  · 若它是 **Electron 页内绘制**（同一个 HWND 内画出来的） → 只能退化为
    截图 + OCR 检测按钮文案。

## 合规性（规格书 §四）

`SetWinEventHook` 是 Windows 官方提供的**跨进程旁观**接口，由系统把事件回调到本进程，
**不注入 DLL、不修改目标进程、不发任何网络请求**。本探针**只读**：
不点击、不发送任何会改变雷神状态的消息；仅 `activate_window` 把窗口置前，便于真人点击。

## 安全前置

点 ✕ 本身不会关闭雷神（只弹框），但仍要求「总时长已暂停」才继续：
若当前不是 PAUSED，需要显式加 `--allow-running`，并在弹框里选「最小化到托盘」而**不要**
选「真的退出」，否则会损失时长。

## 用法（需管理员权限）

    python tests/run_elevated.py --wait tests/out/real_close_sequence.json -- \
        tests/probe_real_close_sequence.py --wait 120

看到提示后：切到雷神窗口 → 点一次右上角 ✕ → 等弹框出现 →
（建议）在弹框里选「最小化到托盘」。
结果：`tests/out/real_close_sequence.json` 与 `.txt`
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

user32 = ctypes.windll.user32

# ---- WinEvent 常量 ----
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0

EVENT_NAMES = {
    EVENT_SYSTEM_FOREGROUND: "FG",
    EVENT_OBJECT_CREATE: "CREATE",
    EVENT_OBJECT_DESTROY: "DESTROY",
    EVENT_OBJECT_SHOW: "SHOW",
    EVENT_OBJECT_HIDE: "HIDE",
    EVENT_OBJECT_LOCATIONCHANGE: "MOVE",
    EVENT_OBJECT_NAMECHANGE: "NAME",
}

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

user32.SetWinEventHook.restype = ctypes.c_void_p
user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                   WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
user32.UnhookWinEvent.restype = wt.BOOL
user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.IsWindow.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]

RESULT = os.path.join(HERE, "out", "real_close_sequence.json")


class EventSniffer:
    """在一个自带消息循环的线程里装 SetWinEventHook，回调只做入队。"""

    def __init__(self, pid_filter: int, logger=None):
        self.pid_filter = int(pid_filter)
        self.log = logger
        self.events = []
        self._lock = threading.Lock()
        self._hooks = []
        self._thread = None
        self._stop = threading.Event()
        self._proc = WINEVENTPROC(self._callback)
        self.installed = False
        self.error = ""

    def _callback(self, hhook, event, hwnd, id_object, id_child, tid, ts):
        if id_object != OBJID_WINDOW or id_child != CHILDID_SELF:
            return                      # 只看窗口级事件，过滤掉菜单/光标等噪声
        with self._lock:
            if len(self.events) < 4000:
                self.events.append({
                    "t": round(time.time(), 3),
                    "ev": EVENT_NAMES.get(event, hex(event)),
                    "raw": event, "hwnd": int(hwnd or 0),
                    "tid": int(tid), "ms": int(ts),
                })

    def pop(self) -> list:
        with self._lock:
            out = list(self.events)
            self.events.clear()
        return out

    def _run(self):
        min_ev, max_ev = EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_NAMECHANGE
        for _ in range(1):
            h = user32.SetWinEventHook(min_ev, max_ev, None, self._proc,
                                       self.pid_filter, 0,
                                       WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            if h:
                self._hooks.append(h)
        # 全局前台事件（idProcess=0 才收得到别的进程抢前台）
        h2 = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                                    None, self._proc, 0, 0,
                                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        if h2:
            self._hooks.append(h2)
        if not self._hooks:
            self.error = f"SetWinEventHook 失败，错误码 {ctypes.GetLastError()}"
            return
        self.installed = True
        msg = wt.MSG()
        while not self._stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)
        for h in self._hooks:
            user32.UnhookWinEvent(h)
        self._hooks = []
        self.installed = False

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="winevent", daemon=True)
        self._thread.start()
        for _ in range(30):
            if self.installed or self.error:
                break
            time.sleep(0.1)
        return self.installed

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None


def _tk(hwnd, fn, n, default=""):
    buf = ctypes.create_unicode_buffer(n)
    fn(wt.HWND(int(hwnd)), buf, n)
    return buf.value or default


def window_info(hwnd) -> dict:
    r = wt.RECT()
    c = wt.RECT()
    user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r))
    user32.GetClientRect(wt.HWND(int(hwnd)), ctypes.byref(c))
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(int(hwnd)), ctypes.byref(pid))
    return {
        "hwnd": int(hwnd), "hwnd_hex": hex(int(hwnd)),
        "class": _tk(hwnd, user32.GetClassNameW, 256),
        "title": _tk(hwnd, user32.GetWindowTextW, 512),
        "pid": int(pid.value),
        "rect": [r.left, r.top, r.right, r.bottom],
        "size": [r.right - r.left, r.bottom - r.top],
        "client_size": [c.right, c.bottom],
        "style": hex(user32.GetWindowLongW(wt.HWND(int(hwnd)), -16) & 0xFFFFFFFF),
        "exstyle": hex(user32.GetWindowLongW(wt.HWND(int(hwnd)), -20) & 0xFFFFFFFF),
        "visible": bool(user32.IsWindowVisible(wt.HWND(int(hwnd)))),
        "owner": int(user32.GetWindow(wt.HWND(int(hwnd)), 4) or 0),   # GW_OWNER
        "offscreen": (r.left < -20000 or r.top < -20000),
    }


def top_level_of_pid(pid: int, include_hidden: bool = True) -> list:
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        try:
            p = wt.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value != pid:
                return True
            if not include_hidden and not user32.IsWindowVisible(h):
                return True
            if user32.GetParent(h):
                return True
            out.append(int(h))
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return out


def uia_names(hwnd, limit: int = 60) -> list:
    """尝试用 UIA 枚举该窗口的控件名（判断确认框按钮可能不可用 UIA 找到）。"""
    try:
        from detection import ui_automation as uia
        if not uia.available():
            return ["<UIA 不可用>"]
        return [f"{i.control_type}|{i.name!r}|{i.class_name}"
                for i in uia.list_controls(hwnd, max_depth=8, limit=limit)][:limit]
    except Exception as e:
        return [f"<UIA 枚举失败：{type(e).__name__}: {e}>"]


def grab(hwnd, out_png: str) -> bool:
    try:
        from detection import coordinate_fallback as cf
        arr = cf.capture_window_printwindow(hwnd)
        if arr is None:
            return False
        from PIL import Image
        Image.fromarray(arr).save(out_png)
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=120.0)
    ap.add_argument("--allow-running", action="store_true",
                    help="当前不是 PAUSED 时也继续（请在弹框里选「最小化到托盘」）")
    ap.add_argument("--out", default=RESULT)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    rep = {
        "ts": time.time(), "elevated": False, "pid": 0,
        "main_before": None, "hook": {}, "timeline": [],
        "new_windows": [], "main_after": None, "notes": [],
    }

    def flush():
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        os.replace(tmp, args.out)

    def say(msg):
        print(msg, flush=True)
        rep["notes"].append(msg)

    rep["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    say(f"本进程提权 = {rep['elevated']}")
    if not rep["elevated"]:
        say("⚠️ 未提权：对提权窗口只能看到部分信息（UIPI）。建议用 run_elevated.py 运行。")

    from core.config import load_config
    from core.state_machine import DurationState
    from detection import coordinate_fallback as cf
    from leigod import window as win_mod
    from leigod.duration_detector import DurationDetector

    cfg = load_config()
    win = win_mod.find_main_window(cfg)
    if not win:
        say("✗ 未找到雷神主窗口，请先启动雷神客户端")
        flush()
        return 3
    rep["main_before"] = win.as_dict()
    pid = win.pid
    rep["pid"] = pid
    say(f"雷神主窗口 HWND=0x{win.hwnd:X} class={win.class_name} pid={pid} rect={win.rect}")
    say(f"启动时该进程的顶层窗口 = {[hex(h) for h in top_level_of_pid(pid)]}")

    # 状态前置检查
    cf.ensure_visible(win.hwnd)
    cf.activate_window(win.hwnd)
    time.sleep(0.6)
    det = DurationDetector(cfg)
    reading = det.detect(win, allow_ocr=True)
    rep["state_before"] = reading.state.value
    rep["state_detail"] = reading.summary()
    say(f"当前时长状态 = {reading.state.value}（{reading.summary()}）")
    if reading.state is not DurationState.PAUSED and not args.allow_running:
        say("✗ 当前不是 PAUSED。点 ✕ 本身不会关闭雷神，但为避免任何损失，"
            "默认拒绝继续。确认无妨后加 --allow-running 重跑。")
        flush()
        return 5

    sniffer = EventSniffer(pid)
    ok = sniffer.start()
    rep["hook"] = {"installed": ok, "error": sniffer.error}
    say(f"SetWinEventHook 安装 = {ok} {sniffer.error}")

    # 同时记录真实鼠标点击：没有点击证据时，「没发现新窗口」只能说明「没人点」，
    # 不能拿来断定确认框是页内绘制的（这是首版真机测试翻车的同一类错误）。
    from leigod.close_protection import MouseClickWatcher
    clicks = MouseClickWatcher()
    clicks_ok = clicks.start()
    say(f"鼠标点击记录钩子安装 = {clicks_ok}")
    r0 = window_info(win.hwnd)["rect"]
    rep["window_rect_before"] = r0

    known = set(top_level_of_pid(pid))
    say("=" * 68)
    say("请现在切到雷神窗口，点一次右上角的 ✕。")
    say("弹框出现后，建议选「最小化到托盘」（本探针只做观测）。")
    say("=" * 68)

    t0 = time.time()
    raw_events = []
    dialog_seen_at = None
    n_click_total = n_click_in_win = n_click_close = 0
    hot = None
    try:
        from leigod import close_intent as ci
        hot = ci.zone_rect(tuple(r0))
        say(f"（按 v2 默认热区统计 ✕ 点击：{hot}）")
    except Exception:
        pass
    while time.time() - t0 < args.wait:
        raw_events.extend(sniffer.pop())
        for (x, y, _ts) in clicks.pop():
            n_click_total += 1
            if r0[0] <= x <= r0[2] and r0[1] <= y <= r0[3]:
                n_click_in_win += 1
            if hot and hot[0] <= x <= hot[2] and hot[1] <= y <= hot[3]:
                n_click_close += 1
                say(f"  检测到落在 ✕ 热区内的点击 (x={x}, y={y})")
        alive = bool(user32.IsWindow(wt.HWND(int(win.hwnd))))
        now = top_level_of_pid(pid)
        for h in now:
            if h in known:
                continue
            known.add(h)
            info = window_info(h)
            info["uia"] = uia_names(h)
            png = os.path.join(os.path.dirname(args.out),
                               f"real_close_newwin_{h & 0xFFFFFF:X}.png")
            info["screenshot"] = png if grab(h, png) else None
            rep["new_windows"].append(info)
            if dialog_seen_at is None:
                dialog_seen_at = time.time()
            say(f"★ 新顶层窗口：class={info['class']} title={info['title']!r} "
                f"rect={info['rect']} owner=0x{info['owner']:X}")
            say(f"   UIA 可见控件 = {info['uia'][:12]}")
        rep["main_alive"] = alive
        rep["main_now"] = window_info(win.hwnd) if alive else None
        if dialog_seen_at and time.time() - dialog_seen_at > 5.0:
            break
        if not alive:
            break
        flush()
        time.sleep(0.25)

    raw_events.extend(sniffer.pop())
    for (x, y, _ts) in clicks.pop():
        n_click_total += 1
        if r0[0] <= x <= r0[2] and r0[1] <= y <= r0[3]:
            n_click_in_win += 1
        if hot and hot[0] <= x <= hot[2] and hot[1] <= y <= hot[3]:
            n_click_close += 1
    clicks.stop()
    sniffer.stop()
    rep["clicks"] = {"total": n_click_total, "in_window": n_click_in_win,
                     "on_close": n_click_close}
    rep["timeline"] = raw_events
    rep["state_after"] = det.detect(win, allow_ocr=True).state.value if user32.IsWindow(
        wt.HWND(int(win.hwnd))) else "WINDOW_GONE"
    rep["main_after"] = window_info(win.hwnd) if user32.IsWindow(wt.HWND(int(win.hwnd))) else None

    say("=" * 68)
    say(f"事件序列（{len(raw_events)} 条）：")
    for e in raw_events[:120]:
        say(f"  +{e['t'] - t0:7.2f}s  {e['ev']:8s} hwnd=0x{e['hwnd']:X}")
    say(f"新增顶层窗口 = {[w['hwnd_hex'] + ' ' + w['class'] for w in rep['new_windows']]}")
    say(f"点击统计 = {rep['clicks']}")
    say(f"主窗口存活 = {user32.IsWindow(wt.HWND(int(win.hwnd)))}"
        f"  事后状态 = {rep['state_after']}")
    if n_click_close == 0:
        say("⚠️ 全程**没有**检测到落在 ✕ 热区内的点击 → 本次观测无效，"
            "不能据此判断确认框形态。请重跑并真的点一次 ✕。")
    elif rep["new_windows"]:
        say("→ 结论：确认框是【独立顶层窗口】→ 层级3 可用 SetWinEventHook 实时发现，"
            "并可用 UIA 找「真的退出」按钮。")
    else:
        say("→ 结论：确实点了 ✕ 但没有出现新顶层窗口 → 确认框是【Electron 页内绘制】，"
            "层级3 必须退化为截图 + OCR 检测按钮文案。")
    flush()
    print(f"\n报告 -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
