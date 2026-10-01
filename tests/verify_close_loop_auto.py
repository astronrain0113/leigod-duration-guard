"""真机闭环验证 · 自动版（推荐）：**用户零操作**，脚本自己点 ✕ 并把结论弹给你。

## 为什么要有这个脚本（取代人工版 `verify_close_loop_real.py`）

人工版要求真人：先切到雷神点「开启时长」→ 等 → 点 ✕ → 处理弹框 → 再点 ✕，
全程还要**盯控制台**且**不许切窗口**。这三个要求互相打架：

  1. 雷神必须恒在前台（否则 OCR 拿不到画面 → UNKNOWN），但提示只打在控制台
     —— 用户被迫在两个窗口之间来回切，而切换本身就在破坏前提；
  2. 为了测「拦截」必须先让雷神进入**计时中**，也就是**先消耗用户的总时长**；
     而这个工具存在的唯一理由就是防止总时长被无意义消耗 ——
     **为了验证而制造它要防的损失，直接违背项目目的**；
  3. 阶段 2（第二次点 ✕）只为判定确认框形态，产品价值极低，却要用户再配合一次。

自动版把这三件事全部收进脚本：自己把雷神切到计时中（几秒）、自己合成点击 ✕、
自己采集证据、收尾自动恢复暂停，最后弹一个结论框。**用户全程不需要看控制台。**

## 它遵守的纪律（与人工版一致）

- §二/§四：不碰雷神 API、不注入、不改内存、不发网络请求；
- §六：UNKNOWN 绝不当 PAUSED；读不到就停在那里如实报；
- §十四：状态未知时**不点任何东西**；
- §十六/§三十四：只有**真的重新读到 PAUSED** 才重放点击（Fail Safe）；
- §三十三：不许假装成功 —— 每一步的证据都落盘，结论由证据推出，推不出就报"未决"。

## 用法（需管理员，雷神需已打开）

    python tests\\run_elevated.py --console --timeout 180 --wait tests\\out\\verify_auto.json -- \
        tests\\verify_close_loop_auto.py

产物：`tests/out/verify_auto.json` / `.txt` / `.console.log`。
"""
from __future__ import annotations

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
OUT_DIR = os.path.join(HERE, "out")
DEFAULT_OUT = os.path.join(OUT_DIR, "verify_auto.json")

GA_ROOT = 2
#: 运行期消耗时长的上限（秒）。工具的目的是省时长，验证也不该挥霍。
MAX_RUNNING_BUDGET_S = 90.0


# --------------------------------------------------------------- 取证基建
class _Tee:
    """同时写 stdout 与转录文件 —— 控制台被关掉也留得下全程。"""

    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8", errors="replace")
        self.stdout = sys.stdout

    def write(self, s):
        try:
            self.stdout.write(s)
            self.stdout.flush()
        except Exception:
            pass
        try:
            self.f.write(s)
            self.f.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.f.flush()
        except Exception:
            pass


def call_with_timeout(fn, timeout: float = 10.0, name: str = ""):
    """在独立线程里跑一次调用，超时即弃（返回 None）。

    观测循环里任何一次「卡住」都会让整轮验证**冻死** —— 不报错、不写日志，
    只是再也不刷新。真机上发生过（并发 OCR 把状态读取卡死 100+ 秒）。
    """
    box = {}

    def _w():
        try:
            box["v"] = ("ok", fn())
        except BaseException as e:                       # noqa: BLE001
            box["v"] = ("err", f"{type(e).__name__}: {e}")

    t = threading.Thread(target=_w, name=name or "probe", daemon=True)
    t.start()
    t.join(timeout)
    return box.get("v")


class _UnknownReading:
    """§六：UNKNOWN 绝不当 PAUSED。读不到就是读不到。"""

    def __init__(self, detail: str = "状态读取失败，按 UNKNOWN 处理"):
        from core.state_machine import DurationState
        self.state = DurationState.UNKNOWN
        self._detail = detail

    def summary(self) -> str:
        return self._detail


def window_at(x: int, y: int) -> dict:
    """记录点击归属窗口（区分「没收到点击」与「点到了别的窗口」）。

    ⚠️ 绝不给 `ctypes.windll.user32` 的 API 赋 argtypes：它与
    `detection/coordinate_fallback.py` 共享同一对象，改了会连坐打挂别人。
    """
    out = {"hwnd": 0, "root": 0, "class": "", "pid": 0, "title": ""}
    try:
        from detection import coordinate_fallback as cf
        h = int(cf.window_from_point(int(x), int(y)) or 0)
        if not h:
            return out
        root = int(user32.GetAncestor(wt.HWND(h), GA_ROOT) or h)
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, buf, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(wt.HWND(root), ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(wt.HWND(root))
        tbuf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(wt.HWND(root), tbuf, n + 1)
        out.update({"hwnd": h, "root": root, "class": buf.value,
                    "pid": int(pid.value), "title": tbuf.value})
    except Exception as e:                     # 纯取证，绝不因此让主流程失败
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# --------------------------------------------------------------- 合成点击
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


class _U(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", ctypes.c_ulong * 8)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _mouse_input(flags, ax=0, ay=0) -> int:
    inp = _INPUT()
    inp.type = 0                                   # INPUT_MOUSE
    inp.u.mi.dx = ax
    inp.u.mi.dy = ay
    inp.u.mi.dwFlags = flags
    try:
        return int(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)))
    except Exception:
        return 0


def click_at(x: int, y: int) -> bool:
    """在屏幕 (x,y) 合成一次真人式左键点击。

    必须带 `MOUSEEVENTF_ABSOLUTE`：按下事件用的是「此刻的系统光标位置」，
    负载高时 `SetCursorPos` 与生效会错开一拍 → 前提全对却点不中（实测踩过）。
    """
    smx = int(user32.GetSystemMetrics(0))
    smy = int(user32.GetSystemMetrics(1))
    ax = int(int(x) * 65535 / max(1, smx - 1))
    ay = int(int(y) * 65535 / max(1, smy - 1))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    n1 = _mouse_input(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, ax, ay)
    time.sleep(0.12)
    n2 = _mouse_input(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, ax, ay)
    return bool(n1 and n2)


# --------------------------------------------------------------- 前台
def bring_front(hwnd, tries: int = 5) -> bool:
    """把窗口抢到前台。雷神最小化时会被移到 (-25600,-25600)，先还原。"""
    try:
        r = wt.RECT()
        user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r))
        if r.left < -10000 or r.top < -10000:
            user32.ShowWindow(wt.HWND(int(hwnd)), 9)       # SW_RESTORE
            time.sleep(0.6)
    except Exception:
        pass
    for i in range(max(1, tries)):
        try:
            if int(user32.GetForegroundWindow() or 0) == int(hwnd):
                return True
            user32.ShowWindow(wt.HWND(int(hwnd)), 5)       # SW_SHOW
            fg = int(user32.GetForegroundWindow() or 0)
            cur = ctypes.windll.kernel32.GetCurrentThreadId()
            tgt = user32.GetWindowThreadProcessId(wt.HWND(fg), None)
            user32.AttachThreadInput(wt.DWORD(tgt), wt.DWORD(cur), True)
            user32.SetForegroundWindow(wt.HWND(int(hwnd)))
            user32.BringWindowToTop(wt.HWND(int(hwnd)))
            user32.AttachThreadInput(wt.DWORD(tgt), wt.DWORD(cur), False)
            time.sleep(0.3)
            if int(user32.GetForegroundWindow() or 0) == int(hwnd):
                return True
        except Exception:
            pass
        time.sleep(0.2 + 0.2 * i)
    try:
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        return False


# --------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="真机闭环验证（自动版，用户零操作）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--arm-wait", type=float, default=25.0,
                    help="等雷神进入「计时中」的上限（秒），默认 25")
    ap.add_argument("--result-wait", type=float, default=60.0,
                    help="点完 ✕ 后等结论的上限（秒）。真机一次「确保暂停」实测 ~26s，"
                         "默认 60 留足余量；走到终态会提前结束")
    ap.add_argument("--no-dialog", action="store_true", help="结尾不弹结论框")
    args, _unknown = ap.parse_known_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    con_log = os.path.join(OUT_DIR, "verify_auto.console.log")
    sys.stdout = _Tee(con_log)

    rep = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid(),
           "phase": "boot", "verdict": "pending", "steps": []}
    started_at = time.time()

    def say(s: str) -> None:
        rep["steps"].append(f"[{time.time() - started_at:6.1f}s] {s}")
        print(f"[{time.time() - started_at:6.1f}s] {s}", flush=True)

    def save():
        rep["elapsed_s"] = round(time.time() - started_at, 1)
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=2)
            with open(args.out.replace(".json", ".txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(rep["steps"]) + "\n\n"
                        + f"结论：{rep.get('verdict')} —— {rep.get('verdict_text','')}\n")
        except Exception:
            pass

    save()

    from core.config import load_config
    from core.state_machine import DurationState
    from detection import coordinate_fallback as cf
    from detection import ui_automation as uia
    from leigod import window as win_mod
    from leigod.close_protection import CloseIntentGuard, is_elevated
    from leigod.duration_controller import DurationController
    from leigod.duration_detector import DurationDetector

    elevated = is_elevated()
    rep["elevated"] = elevated
    say(f"提权状态 = {elevated}")
    if not elevated:
        rep["verdict"] = "cannot_run"
        rep["verdict_text"] = "未提权：低层钩子与合成输入都会被 UIPI 无效化。请用管理员运行。"
        say("✗ " + rep["verdict_text"])
        save()
        return 2

    cfg = load_config()
    det = DurationDetector(cfg)
    ctrl = DurationController(cfg, det)

    # ---- 1. 找窗口 + 前台化 ----
    win = win_mod.find_main_window(cfg)
    if not win or not win.hwnd:
        rep["verdict"] = "cannot_run"
        rep["verdict_text"] = "没找到雷神主窗口。请先把雷神从托盘打开。"
        say("✗ " + rep["verdict_text"])
        save()
        return 2
    rep["window"] = {"hwnd": int(win.hwnd), "pid": int(getattr(win, "pid", 0) or 0),
                     "title": getattr(win, "title", ""), "rect": list(win.rect or [])}
    say(f"雷神窗口 hwnd=0x{int(win.hwnd):X}｜矩形={win.rect}")
    rep["phase"] = "front"
    fg_ok = bring_front(win.hwnd)
    rep["foreground_ok"] = fg_ok
    say(f"抢前台 = {'成功' if fg_ok else '失败'}（失败会让 OCR 拿不到画面 → UNKNOWN）")
    save()

    # ---- 2. 读状态 ----
    def read_state(timeout=12.0):
        v = call_with_timeout(lambda: det.detect(win, allow_ocr=True),
                              timeout=timeout, name="detect")
        if v is None:
            return DurationState.UNKNOWN, f"状态读取超过 {timeout:.0f}s 未返回（卡住）"
        if v[0] != "ok":
            return DurationState.UNKNOWN, f"状态读取异常：{v[1]}"
        r = v[1]
        return r.state, r.summary()

    rep["phase"] = "state"
    st, detail = read_state(20.0)
    rep["state_initial"] = st.value
    rep["state_initial_detail"] = detail
    say(f"初始状态 = {st.value}（{detail}）")
    save()

    if st is DurationState.UNKNOWN:
        # §十四：状态未知时不点任何东西。
        rep["verdict"] = "unknown_state"
        rep["verdict_text"] = ("读不到总时长状态，按 §十四 不点任何东西。"
                               "常见原因：雷神不在最前面 / 被遮挡 / 处于 1~2 秒过渡态。"
                               f"证据：{detail}")
        say("✗ " + rep["verdict_text"])
        save()
        return 2

    # ---- 3. 准备：必须是「计时中」，否则钩子按设计放行，测不到吞点 ----
    # 这里会消耗几秒时长。工具的使命是省时长，所以严格限时并记账。
    rep["phase"] = "arm"
    t_arm = time.time()
    if st is DurationState.PAUSED:
        say("当前已暂停 → 脚本自己点「开启时长」切到计时中（会消耗几秒时长，已记账）")
        started = False
        try:
            # 注意：`detect_uia()` 返回的是 Evidence **列表**；控件清单在
            # `detector.last_uia`（由它顺带填好），要通过 `duration_controls()` 取。
            det.detect_uia(win)
            cands = (det.duration_controls().get("start") or [])
            if cands:
                ok, why = uia.invoke(cands[0])
                say(f"  UIA 点「开启时长」：ok={ok}｜{why}")
                started = bool(ok)
        except Exception as e:
            say(f"  点「开启时长」异常：{type(e).__name__}: {e}")
        if not started:
            rep["verdict"] = "cannot_run"
            rep["verdict_text"] = "无法自动切到「计时中」（定位不到「开启时长」按钮），已放弃，未消耗额外时长。"
            say("✗ " + rep["verdict_text"])
            save()
            return 2
        # 真机事实：点完会先变灰底、字仍为「开启时长」，1~2 秒后才变红色「暂停时长」。
        time.sleep(2.0)
        st, detail = read_state()
        say(f"  切换后状态 = {st.value}（{detail}）")
    else:
        say("当前已在计时中，无需切换")

    # 等它稳定为 RUNNING（过渡态最多 1~2 秒，给足余量但不无限等）
    waited = 0.0
    while st is not DurationState.RUNNING and waited < args.arm_wait:
        say(f"  等「计时中」成立… {st.value}（{detail[:80]}）")
        time.sleep(2.0)
        waited += 2.0
        st, detail = read_state()
        if st is DurationState.UNKNOWN and ("同时存在" in detail or "冲突" in detail):
            say("    （两种按钮同时出现 = 刚点过「开启时长」的 1~2 秒过渡态，正常）")
    rep["state_before"] = st.value
    rep["state_before_detail"] = detail
    save()

    if st is not DurationState.RUNNING:
        rep["verdict"] = "cannot_run"
        rep["verdict_text"] = (f"等了 {waited:.0f}s 仍未进入「计时中」（当前 {st.value}）。"
                               "未点 ✕，未消耗额外时长。")
        say("✗ " + rep["verdict_text"])
        save()
        return 2

    # ---- 4. 装输入层 + 死手泵 ----
    rep["phase"] = "hook"
    guard = CloseIntentGuard(cfg, logger=None)
    guard.bind(win.hwnd)
    if guard.confirm is not None:
        guard.confirm.bind(win.hwnd, win.pid)
    guard.set_state(st)
    pause_recs: list = []
    guard.set_pause_callback(lambda: _pause_now(ctrl, win, det, say, pause_recs))
    started = guard.start()
    rep["hook"] = {"started": started, "installed": guard.watcher.installed,
                   "error": guard.watcher.error}
    say(f"输入层钩子 installed={guard.watcher.installed}（{guard.watcher.error or 'ok'}）")
    if not guard.watcher.installed:
        rep["verdict"] = "cannot_run"
        rep["verdict_text"] = "低层鼠标钩子没装上（多半是没提权），无法验证。"
        say("✗ " + rep["verdict_text"])
        save()
        return 2

    stop_ev = threading.Event()

    def _pump():
        while not stop_ev.is_set():
            try:
                guard.maintain()
            except Exception:
                pass
            time.sleep(0.05)

    threading.Thread(target=_pump, daemon=True).start()
    time.sleep(0.8)                       # 让 maintain() 先武装死手开关
    save()

    # ---- 5. 合成点 ✕ ----
    rep["phase"] = "click"
    r0 = cf.get_window_rect(win.hwnd)
    import leigod.close_intent as ci
    hot = ci.zone_rect(tuple(r0), guard.zone)
    cx, cy = (hot[0] + hot[2]) // 2, (hot[1] + hot[3]) // 2
    rep["window_rect"] = list(r0)
    rep["hot_zone"] = list(hot)
    rep["click_point"] = [cx, cy]
    say(f"窗口={r0}｜✕ 热区={hot}｜点击点=({cx},{cy})")
    if r0[0] < -10000 or r0[1] < -10000:
        rep["verdict"] = "cannot_run"
        rep["verdict_text"] = "雷神窗口处于托盘位置（坐标 -25600），点 ✕ 无意义。"
        say("✗ " + rep["verdict_text"])
        stop_ev.set()
        guard.stop()
        save()
        return 2

    fg_ok = bring_front(win.hwnd)
    say(f"点击前抢前台 = {'成功' if fg_ok else '失败'}")
    over = window_at(cx, cy)
    rep["click_point_owner"] = over
    say(f"点击点归属窗口 = 0x{over.get('root', 0):X} class={over.get('class')}"
        f"（雷神=0x{int(win.hwnd):X}）")
    try:
        guard.watcher.pop_clicks()
    except Exception:
        pass
    ok_click = click_at(cx, cy)
    rep["click_injected"] = ok_click
    say(f"已合成点击 ✕（SendInput 返回 {'成功' if ok_click else '失败/被拦截'}）")
    save()

    # ---- 6. 收证据 ----
    # ⚠️ 这里曾经写错过（真机第一轮就栽在这）：观察循环的退出条件不是「等够 N 秒」，
    # 而是**等到 guard 走到终态**。真机一次「确保暂停」实测要 ~26s（含控制器内部的
    # 复核读取），而 `_handle` 是同步阻塞在 `_ensure_paused()` 里的 —— 固定等 20s
    # 就收工，会在暂停还没返回时拍下 `replayed=0`，然后立刻 `guard.stop()`
    # **把马上要发生的重放亲手掐掉**，得出「吞了点但没重放」的假结论。
    # 终态三选一：replayed（放行）/ blocked（按 Fail Safe 不放行）/ false_swallow（核查未通过）。
    rep["phase"] = "observe"
    t0 = time.time()
    last = {}
    while time.time() - t0 < args.result_wait:
        time.sleep(0.5)
        try:
            snap = dict(guard.stats)
        except Exception:
            snap = {}
        last = snap
        if (snap.get("replayed", 0) >= 1 or snap.get("blocked", 0) >= 1
                or snap.get("false_swallow", 0) >= 1):
            break
    rep["stats"] = last
    rep["pause_callback_records"] = pause_recs
    rep["watcher_reason"] = getattr(guard.watcher, "last_reason", "")
    # 收工前给 guard 的收尾动作留一点余量：停得太急会把刚要发出的重放掐断。
    time.sleep(2.0)
    # 重放之后雷神应该弹自己的确认框 —— 那是「点击真的送达应用」的**独立证据**。
    # 只看 `replayed=1` 只能证明"我们发出去了"，证明不了"雷神收到了"（§三十三：
    # 不许用发送成功冒充送达成功）。所以这里专门再等一轮确认框。
    confirm_deadline = time.time() + 12.0
    while time.time() < confirm_deadline:
        try:
            if int(dict(guard.stats).get("confirm_seen", 0) or 0) >= 1:
                break
        except Exception:
            pass
        time.sleep(0.5)
    try:
        last = dict(guard.stats)
        rep["stats_final"] = last
    except Exception:
        pass
    try:
        rep["confirm_status"] = guard.confirm.status() if guard.confirm else None
    except Exception:
        pass
    try:
        rep["events"] = [e for e in (guard.events or [])
                         if e.get("ev") in ("close_released", "close_blocked",
                                            "false_swallow", "confirm_dialog",
                                            "close_intent")][-10:]
    except Exception:
        pass
    # ⚠️ 真机第 5 轮暴露的现象：重放点击 → 雷神弹出自己的确认框（在主窗口内绘制
    # 的模态层）→ UIA 从 ~476 个控件掉到 5 个、OCR 报「截屏区域被其它窗口遮挡」
    # → 单次读取必然 UNKNOWN。这是**复核不了**，不是**没暂停**（暂停回调早就
    # success=True 了，重放前也已复核为 PAUSED）。所以要**重试**（等确认框被处理
    # 掉之后就能读到了），并把「复核不了」如实写进报告 —— 绝不能拿一次 UNKNOWN
    # 就把已经证实的主路径抹成 inconclusive（§三十三：两头都不许碰）。
    st_after, detail_after = read_state()
    tries = 1
    deadline = time.time() + 30.0
    while st_after.value == "UNKNOWN" and tries < 4 and time.time() < deadline:
        say(f"  终态读不到（第 {tries} 次）→ 抢前台后重试…")
        try:
            bring_front(int(win.hwnd))
        except Exception:                                     # noqa: BLE001
            pass
        time.sleep(3.0)
        st_after, detail_after = read_state()
        tries += 1
    rep["state_after_tries"] = tries
    rep["state_after"] = st_after.value
    rep["state_after_detail"] = detail_after
    rep["running_seconds"] = round(time.time() - t_arm, 1)
    say(f"结果统计 = {last}｜钩子理由 = {rep['watcher_reason']}")
    say(f"关键事件 = {rep.get('events')}")
    say(f"点击后状态 = {st_after.value}（{detail_after}）")
    say(f"本次处于「计时中」的时长 ≈ {rep['running_seconds']}s")

    # ---- 7. 收尾：确保恢复暂停（不能白让用户消耗时长）----
    rep["phase"] = "restore"
    if st_after is not DurationState.PAUSED:
        say("收尾：尚未暂停，执行暂停…")
        try:
            out = ctrl.pause(win)
            say(f"  暂停 success={out.success}｜{out.detail}")
            rep["restore_pause"] = {"success": bool(out.success), "detail": out.detail}
        except Exception as e:
            rep["restore_pause"] = {"error": f"{type(e).__name__}: {e}"}
    else:
        rep["restore_pause"] = {"success": True, "detail": "已是 PAUSED"}
    stop_ev.set()
    try:
        guard.stop()
    except Exception:
        pass
    save()

    # ---- 8. 判定（结论必须由证据推出，推不出就报未决）----
    rep["phase"] = "done"
    # 判定走纯函数 `_judge`：真机结论这个环节已经出过两次假结论，
    # 必须能被单测逐分支覆盖，不能埋在 300 行的 main() 里靠人肉核对。
    verdict, text = _judge(
        last, st_after.value,
        detail=detail_after,
        reason=rep.get("watcher_reason", ""),
        click_root=int(over.get("root", 0)),
        main_hwnd=int(win.hwnd),
        result_wait=args.result_wait)

    rep["verdict"] = verdict
    rep["verdict_text"] = text
    say(f"结论 = {verdict}：{text}")
    save()

    if not args.no_dialog:
        try:
            user32.MessageBoxW(
                0,
                f"{text}\n\n"
                f"吞点={sw} 重放={rp} 确认框={cs}｜状态 {st_after.value}\n"
                f"本次计时中约 {rep['running_seconds']}s\n\n"
                f"详细报告：{args.out}",
                "雷神关闭保护 · 真机闭环结论",
                0x40 | 0x10000 | 0x40000)          # MB_ICONINFORMATION|SETFOREGROUND|TOPMOST
        except Exception:
            pass
    return 0


def _judge(stats: dict, state_value: str, *, detail: str = "", reason: str = "",
           click_root: int = 0, main_hwnd: int = 0, result_wait: float = 60.0):
    """由证据推出真机结论 —— 纯函数（不碰窗口、不读全局），因此可被单测逐分支覆盖。

    ⚠️ 这个函数是被**两次真机假结论**逼出来的，两次都栽在同一个环节上：

    1. 观察循环固定等 20s 就收工，而真机一次「确保暂停」实测约 26s，
       于是在暂停还没返回时拍下 `replayed=0`，又立刻 `guard.stop()`
       **亲手掐断马上要发生的重放**，得出「吞了点却没重放」的假结论。
    2. 判定分支写成了**两段独立的 if**：第二段 `if/elif/.../else` 链的
       `else` 是**无条件兜底**，把第一段刚判出的 `protected` 覆盖成了
       `inconclusive`。那一轮证据其实已经齐了（swallowed=1、replayed=1、
       confirm_seen=1、state=PAUSED），报告却写「证据不足以定论」——
       **把已经证实的成功报成了未决**。

    §三十三 的两头都不能碰：不许把没成功的说成成功，也不许把已经成功的
    报成未决（后者会让整条链路的真机结论永远是一笔糊涂账）。
    所以判定必须是**单条 if/elif 链** + 纯函数 + 逐分支单测。
    """
    sw = int(stats.get("swallowed", 0) or 0)
    rp = int(stats.get("replayed", 0) or 0)
    cs = int(stats.get("confirm_seen", 0) or 0)
    handled = int(stats.get("handled", 0) or 0)
    blocked = int(stats.get("blocked", 0) or 0)
    paused = (state_value == "PAUSED")

    if sw >= 1 and paused and rp >= 1 and cs >= 1:
        verdict, text = "protected", (
            "✅ 关闭保护在真机上**完整**走通：✕ 被吞住 → 自动暂停（已复核为 PAUSED）"
            " → 重放点击 → **雷神自己的确认框被监测到**（证明重放真的送达了应用，"
            "而不只是我们发出去了）。"
            "确认框请你自己选（建议「最小化到托盘」，时长已暂停，不会继续消耗）。")
    elif sw >= 1 and paused and rp >= 1 and cs == 0:
        verdict, text = "protected_confirm_unverified", (
            "⚠️ 主路径走通了（✕ 被吞住 → 自动暂停 → 已复核为 PAUSED → 重放），"
            "但**层级3 没监测到雷神的确认框**。这只能证明「我们把点击发出去了」"
            "（SendInput 返回成功），**不能证明雷神真的收到并弹了框** —— "
            "按 §三十三 不能用发送成功冒充送达成功。"
            "请你目视确认：雷神当时有没有弹出「最小化到托盘 / 真的退出」那个框？"
            "若弹了 → 是层级3 的识别口径问题（形态/文案），另行修；"
            "若没弹 → 重放没真正送达，需要查坐标与窗口状态。")
    elif sw >= 1 and rp >= 1 and cs >= 1 and state_value == "UNKNOWN":
        verdict, text = "protected_state_unreadable", (
            "⚠️ 主路径已在真机跑通（✕ 被吞住 → 自动暂停 → 重放 → "
            "**雷神的确认框被监测到**），但**收尾时状态复核不了**："
            "雷神弹出确认框后控件树被遮住、截屏也被判为遮挡。"
            "§六：UNKNOWN 绝不当 PAUSED —— 所以这里**不声称已经暂停**，"
            "只说主路径成立。请你在雷神那个框里做出选择（建议「最小化到托盘」），"
            f"再目视确认时长是否已停止。证据：{detail}")
    elif sw >= 1 and blocked >= 1:
        verdict, text = "swallowed_no_replay", (
            "⚠️ 吞点了但**按 Fail Safe 没放行**：暂停没被复核为 PAUSED，"
            f"宁可不放也不让你丢时长。当前状态={state_value}，证据：{detail}")
    elif sw >= 1 and rp == 0 and paused:
        verdict, text = "replay_pending", (
            "⏳ 点被吞住、暂停也已确认（PAUSED），但**在观察窗口内没等到重放** —— "
            "guard 当时仍阻塞在暂停回调里（真机实测约 26s）。"
            "这是**观测未完成**，不是产品结论 —— 绝不能报成「没重放」。"
            f"请把 --result-wait 加大后重跑（本次 {result_wait:.0f}s）。"
            f"当前状态={state_value}")
    elif sw >= 1 and rp == 0:
        verdict, text = "swallowed_no_replay", (
            "⚠️ 点击被吞住了，但**没有重放** —— 说明暂停未被复核为 PAUSED，"
            "Fail Safe 按设计生效（宁可不放也不让你丢时长）。"
            f"当前状态={state_value}，证据：{detail}")
    elif handled >= 1 and sw == 0:
        verdict, text = "not_swallowed", (
            "⚠️ 收到了 ✕ 点击但**没有吞** —— 钩子按判据主动放行。"
            f"理由：{reason or '未记录'}")
    elif sw == 0 and handled == 0:
        verdict, text = "no_click_seen", (
            "⚠️ 钩子一次点击都没收到 —— 多半是没提权（UIPI）或点击没落在雷神上。"
            f"点击点归属窗口：0x{click_root:X} / 雷神 0x{main_hwnd:X}")
    else:
        verdict, text = "inconclusive", (
            f"证据不足以定论：{stats}｜状态 {state_value}")
    if blocked:
        text += f"（另有 blocked={blocked} 次被判不放行）"
    return verdict, text



def _pause_now(ctrl, win, det, say, rec_out: list):
    """暂停回调：**必须返回 `DurationState`**，诊断信息只能走 `rec_out` 带出。

    ⚠️ 这里踩过一个真机才暴露的坑（代价 = 整整两轮真机验证）：
    早先为了把诊断带出来，回调返回的是**字典 `rec`**。而消费端
    `_ensure_paused()` 拿回调返回值当状态用（`self._state = res`，
    随后判 `res is DurationState.PAUSED`）—— 字典永远 `is not` PAUSED，
    于是**暂停明明成功了，却每次都判成"未确认" → blocked → 永不重放**。
    返回值类型是**契约**，不是实现细节。
    """
    from core.state_machine import DurationState
    rec = {}
    try:
        v = call_with_timeout(lambda: det.detect(win, allow_ocr=True),
                              timeout=12.0, name="detect-cb")
        if v is None or v[0] != "ok":
            rec["action"] = "状态读取超时，按 §十四 不点任何东西"
            say("  暂停回调：读取超时，未点击")
            return DurationState.UNKNOWN
        r = v[1]
        rec["state_before"] = r.state.value
        if r.state.value == "PAUSED":
            rec["action"] = "已是 PAUSED，未点击"
            return DurationState.PAUSED
        if r.state.value == "UNKNOWN":
            rec["action"] = "状态未知，按 §十四 不点任何东西"
            say("  暂停回调：状态未知，未点击")
            return DurationState.UNKNOWN
        out = ctrl.pause(win)
        rec.update({"action": "已执行暂停", "success": bool(out.success),
                    "detail": out.detail})
        say(f"  暂停回调：success={out.success}｜{out.detail}")
        # `ctrl.pause` 内部已含复核读取；失败则如实报 RUNNING（§十六：不假装成功）
        return DurationState.PAUSED if out.success else DurationState.RUNNING
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        return DurationState.UNKNOWN
    finally:
        try:
            rec_out.append(rec)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                                   # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            with open(os.path.join(OUT_DIR, "verify_auto.error.txt"), "w",
                      encoding="utf-8") as f:
                f.write(f"argv={sys.argv[1:]}\n\n{tb}\n")
            with open(os.path.join(OUT_DIR, "verify_auto.console.log"), "a",
                      encoding="utf-8", errors="replace") as f:
                f.write("\n!!! 崩溃 !!!\n" + tb + "\n")
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(9)
