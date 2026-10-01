"""关闭保护（规格书 §十二 / §十三 / §十四）——本项目的核心功能。

## 现状（2026-09-29 真机实测后重写，详见 docs/关闭保护-设计方案v2.md）

真机实测推翻了两条旧假设：

1. **依赖 `HTCLOSE` 的意图识别恒假**——真实雷神窗口整片客户区对 `WM_NCHITTEST`
   一律返回 `HTCLIENT(1)`（176 个采样点无一例外）。标题栏（含 ✕）是应用自绘的，
   点击由 Chromium 渲染进程接收。
2. **✕ 不走 `SC_CLOSE`、也不直接关窗**——真人点 ✕ 后窗口**没有关闭**，
   而是弹出雷神自己的确认框询问「最小化到托盘」/「真的退出」。
   因此「禁用系统菜单项」对 ✕ **前提不成立**，
   而「窗口是否消失」也**不是**拦截成功的有效判据
   （第一次 `real_close_test.py` 正是在这里把假象写成了「拦截生效」）。

于是本模块分两层：

| 层 | 类 | 职责 | 真机有效性 |
| --- | --- | --- | --- |
| **主路径（输入层）** | `CloseIntentGuard` | 吞掉 ✕ 热区的点击 → 确保已暂停 → 重放点击放行 | 待真机闭环验证 |
| **兜底（系统关闭路径）** | `CloseProtection` | `EnableMenuItem(SC_CLOSE, MF_GRAYED)` | 对 ✕ **无效**；覆盖 `Alt+F4` / 任务栏右键关闭 |

`CloseProtection` 的历史设计说明保留在下面，但请以本段为准。

---

原设计说明（仅对「普通带系统标题栏的窗口」成立，不可外推到真机）：
  方案1 禁用系统菜单项 SC_CLOSE  → 点击 ✕ 窗口存活、目标进程收不到 WM_CLOSE
  方案2 去掉窗口 WS_SYSMENU      → 同样拦得住，但 ✕ 按钮直接消失，视觉侵入大
  对照组 不做任何处理            → 窗口关闭并收到 WM_CLOSE（证明测试本身有效）

失败教训（必须保留）：上面三条结论**只在靶机 `LeigodMockWnd` 上成立**，因为靶机是
"普通 Win32 窗口 + 系统标题栏 + `WM_NCHITTEST` 返回 `HTCLOSE`"，
而真机是"Electron + 自绘标题栏 + 全窗 `HTCLIENT` + 应用内确认框"。
**自动化全绿、真机静默失效**，根因就是靶机与真机结构不一致。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass

from core.state_machine import CloseDecision, DurationState, decide_close_action
from detection import coordinate_fallback as cf
from leigod import close_intent as ci
from leigod import confirm_dialog as cd

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MF_BYCOMMAND = 0x0000
MF_GRAYED = 0x00000001
MF_ENABLED = 0x00000000
GWL_STYLE = -16
WS_SYSMENU = 0x00080000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x20

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_MOUSEMOVE = 0x0200
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
VK_F4 = 0x73


def window_alive(hwnd) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(wt.HWND(int(hwnd))))


def _pid_of(hwnd) -> int:
    """取窗口属主进程 pid（0 表示无效）。确认框监测要按 pid 过滤窗口事件。"""
    if not hwnd:
        return 0
    p = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(int(hwnd)), ctypes.byref(p))
    return int(p.value)


def is_elevated() -> bool:
    """本进程是否以管理员运行。

    雷神 PE 清单是 `requireAdministrator`，恒以**高完整性级别**运行。
    非提权进程的低层钩子拦不住发往高完整性窗口的输入（UIPI），
    对提权窗口的 `EnableMenuItem` 也会被静默拒绝（返回 `0xFFFFFFFF`）。
    → **关闭保护必须提权，否则只能降级为「只读告警」。**
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass
class ProtectionReport:
    decision: CloseDecision = CloseDecision.BLOCK_UNKNOWN
    sc_close_disabled: bool = False
    detail: str = ""
    ts: float = 0.0


# ---------------------------------------------------------------------------
# 底层鼠标钩子：只用来「识别关闭意图」，不用来拦截
# ---------------------------------------------------------------------------
class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _BaseWatcher:
    """低层钩子的公共骨架。

    钩子回调里**只做入队**：任何耗时操作（尤其是跨进程 SendMessage 做命中测试）
    都会让系统认为钩子超时，进而把钩子悄悄摘掉。命中测试放到消费端做。
    """

    hook_id = 0
    name = "hook"
    queue_size = 64

    def __init__(self, logger=None):
        self.log = logger
        self._queue = deque(maxlen=self.queue_size)
        self._lock = threading.Lock()
        self._thread = None
        self._hook = None
        self._stop = threading.Event()
        self._proc = HOOKPROC(self._callback)
        self.installed = False
        self.error = ""

    def _record(self, wparam, lparam):     # 子类实现
        raise NotImplementedError

    def _callback(self, code, wparam, lparam):
        try:
            if code >= 0:
                self._record(wparam, lparam)
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _run(self):
        # 句柄返回值必须显式声明，否则会被截断成 32 位（实测直接导致错误码 126）
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
        user32.CallNextHookEx.restype = ctypes.c_longlong
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.UnhookWindowsHookEx.restype = wt.BOOL
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

        hmod = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(self.hook_id, self._proc, hmod, 0)
        if not self._hook:
            # 低层钩子的回调在本进程线程里执行，因此 hMod 允许为 NULL；
            # 某些环境下传入模块句柄会被拒，这里退一步再试一次。
            self._hook = user32.SetWindowsHookExW(self.hook_id, self._proc, None, 0)
        if not self._hook:
            self.installed = False
            self.error = f"SetWindowsHookExW({self.name}) 失败，错误码 {ctypes.GetLastError()}"
            if self.log:
                self.log.warning("%s 安装失败：%s", self.name, self.error)
            return
        self.installed = True
        if self.log:
            self.log.info("%s 已安装", self.name)
        msg = wt.MSG()
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self.installed = False
        if self.log:
            self.log.info("%s 已卸载", self.name)

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return self.installed
        self._stop.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        for _ in range(30):
            if self.installed or self.error:
                break
            time.sleep(0.1)
        return self.installed

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t:
            try:
                user32.PostThreadMessageW(t.native_id, 0x0012, 0, 0)   # WM_QUIT
            except Exception:
                pass
            t.join(timeout=2.0)
        self._thread = None

    def pop(self) -> list:
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()


class MouseClickWatcher(_BaseWatcher):
    """记录全局左键按下的坐标（用于识别「点了 ✕」）。"""

    hook_id = WH_MOUSE_LL
    name = "mouse-hook"

    def _record(self, wparam, lparam):
        if wparam != WM_LBUTTONDOWN:
            return
        data = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
        with self._lock:
            self._queue.append((int(data.pt.x), int(data.pt.y), time.time()))


class KeyPressWatcher(_BaseWatcher):
    """记录全局按键（用于识别 Alt+F4 关闭意图）。

    Alt+F4 与点 ✕ 走同一条 SC_CLOSE 通路，因此禁用系统菜单同样能拦住它；
    但「拦住了也不告诉用户」体验很差，所以这里识别出意图后去跑保护流程。
    """

    hook_id = WH_KEYBOARD_LL
    name = "keyboard-hook"

    def _record(self, wparam, lparam):
        if wparam not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return
        data = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
        if data.vkCode != VK_F4:
            return
        # Alt 按下（GetAsyncKeyState 高位表示当前按下）
        alt = bool(user32.GetAsyncKeyState(0x12) & 0x8000)
        if not alt:
            return
        with self._lock:
            self._queue.append((VK_F4, time.time()))


# ---------------------------------------------------------------------------
# 关闭保护 v2 —— 主路径：输入层拦截（吞点 → 确保暂停 → 重放）
# ---------------------------------------------------------------------------
class CloseIntentWatcher(_BaseWatcher):
    """**可以吞掉点击**的低层鼠标钩子。

    与 `MouseClickWatcher`（只记录、不干预）的关键区别：
    回调返回非 0 会让 Windows **不再把这次点击派发给任何窗口**，
    这就是关闭保护的拦截点。

    回调里的纪律（违反就会静默失效，务必守住）：
      · 只读一次 `self._snap`（不可变快照，不需要加锁）；
      · 只做几步整数比较（`close_intent.should_swallow` 是纯函数）；
      · **绝不**跨进程 `SendMessage`、不读文件、不打日志、不取锁。
        Windows 规定低层钩子回调超时（默认 300ms）就**静默摘掉钩子**，
        届时保护彻底失效且无人察觉。

    「死手开关」`_swallow_deadline`：消费端每轮刷新；
    若消费端卡住或崩溃，超过 `deadman_s` 后自动停止吞点。
    这是刻意的取舍——一直吞点而没人处理，用户会连雷神都关不掉，
    比偶尔漏拦一次更糟。届时会发出 `degraded` 事件告知用户。
    """

    hook_id = WH_MOUSE_LL
    name = "mouse-hook-intent"

    def __init__(self, logger=None, marker: int = 0x4C474452, dwell_ms: int = 300):
        super().__init__(logger)
        self.marker = int(marker)
        self.dwell_ms = max(0, int(dwell_ms))
        self._snap = ci.GuardSnapshot(armed=False)
        self._pending = deque(maxlen=16)
        self._clicks = deque(maxlen=64)      # 全部左键按下（诊断用）
        self._hover = None
        self._hover_since = 0.0              # 光标进入 ✕ 邻域的时刻（停留时长判定）
        self._swallow_deadline = 0.0
        self._plock = threading.Lock()
        self.swallowed = 0
        self.passed = 0
        self.replayed_seen = 0
        self.last_reason = ""
        self.degraded = False

    # ------------------------------------------------------------- 与主线程
    def update_snapshot(self, snap) -> None:
        """整体替换引用即可——赋值在 CPython 下是原子的，钩子侧无需加锁。"""
        self._snap = snap

    def arm(self, seconds: float) -> None:
        self._swallow_deadline = time.time() + max(0.0, seconds)
        self.degraded = False

    def pop_swallowed(self) -> list:
        with self._plock:
            out = list(self._pending)
            self._pending.clear()
        return out

    def pop_hover(self):
        h, self._hover = self._hover, None
        return h

    def pop_clicks(self) -> list:
        with self._plock:
            out = list(self._clicks)
            self._clicks.clear()
        return out

    # --------------------------------------------------------------- 钩子
    def _record(self, wparam, lparam) -> bool:
        data = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
        if int(data.dwExtraInfo or 0) == self.marker:
            self.replayed_seen += 1
            return False                     # 我们自己重放的输入，直接放行
        x, y = int(data.pt.x), int(data.pt.y)
        snap = self._snap                    # 只读一次

        if wparam == WM_MOUSEMOVE:
            eligible = ci.should_prepause(snap, x, y)[0]
            now = time.time()
            if not eligible:
                self._hover_since = 0.0
                return False
            # 停留时长门槛：快速划过不触发，避免误暂停打断加速。
            # 只用两个浮点数做记录，仍然是 O(1)、无锁、无跨进程调用。
            if self._hover_since == 0.0:
                self._hover_since = now
                return False
            if (now - self._hover_since) * 1000.0 >= self.dwell_ms:
                self._hover = (x, y, now)
            return False

        if wparam != WM_LBUTTONDOWN:
            return False

        with self._plock:
            self._clicks.append((x, y, time.time()))

        if time.time() > self._swallow_deadline:
            # 消费端没在刷新 → 停止吞点（离手保护），并置降级标志
            self.degraded = True
            self.passed += 1
            return False

        swallow, why = ci.should_swallow(snap, x, y)
        self.last_reason = why
        if not swallow:
            self.passed += 1
            return False
        self.swallowed += 1
        with self._plock:
            self._pending.append((x, y, time.time(), snap.state))
        return True

    def _callback(self, code, wparam, lparam):
        if code >= 0:
            try:
                if self._record(wparam, lparam):
                    return 1                 # ← 吞掉：系统不再派发这次点击
            except Exception:
                pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def status(self) -> dict:
        return {
            "installed": self.installed, "error": self.error,
            "swallowed": self.swallowed, "passed": self.passed,
            "replayed_seen": self.replayed_seen, "degraded": self.degraded,
            "last_reason": self.last_reason,
        }


class CloseIntentGuard:
    """关闭保护 v2 主路径：**吞掉 ✕ 点击 → 确保已暂停 → 重放点击放行**。

    为什么必须是这条路径（而不是「禁用系统菜单」）：
      真机实测，雷神的 ✕ 是应用自绘的、点击由渲染进程处理、只弹应用内确认框，
      既不关窗也不走 `SC_CLOSE`。想「先确保暂停才放行」，
      唯一不注入 DLL 的办法就是在**输入层**把这次点击先截下来。

    与规格书的对应：
      · §十三 判据不是单点几何，而是「保护开启 + 状态非 PAUSED + 在窗口内
        + 在 ✕ 热区」，消费端再加一条「`WindowFromPoint` 根窗口必须雷神」；
      · §十四 先确保暂停**才**放行：暂停未确认为 PAUSED 时**绝不重放**；
      · §十六 放行依据是**重新检测到 PAUSED**（`pause_callback` 的返回值），
        与「点击是否成功」无关；
      · §三十四 Fail Safe：`UNKNOWN` 与暂停失败一律不放行，只发通知。
    """

    def __init__(self, config, logger=None, sink=None, pause_callback=None,
                 on_blocked=None):
        self.config = config
        self.log = logger
        self.sink = sink
        self.on_blocked = on_blocked      # fn(state_value) → 由引擎负责弹通知
        self.hwnd = 0
        self.zone = config.get("close_protection.close_hot_zone", {}) or {}
        self.marker = int(config.get("close_protection.replay_marker", 0x4C474452))
        self.enabled = bool(config.get("close_protection.swallow_close_click", True))
        self.protect_on = bool(config.get("close_protection.enabled", True))
        self.prepause = bool(config.get("close_protection.prepause_on_hover", True))
        self.deadman = max(0.5, int(config.get("close_protection.swallow_deadman_ms", 2500)) / 1000.0)
        #: 自适应死手窗口的上限：消费端真死了，最迟这么久一定停止吞点
        self._max_arm = max(self.deadman, int(config.get(
            "close_protection.swallow_deadman_max_ms", 10000)) / 1000.0)
        self._last_maintain = 0.0
        # 没量过 tick 时保持 0 → `_arm_window()` 用配置值。
        # ⚠️ 曾经试过"初值就给到上限"，被 `test_close_guard.case_deadman` 抓住：
        # 那会让**消费端从未运行**的情况下也吞 10 秒点击 —— 用户会被自己锁死。
        # 死手的下限意义（消费端死了就尽快放手）不能为了启动期好过而牺牲。
        # 启动期那次"首轮 tick 很慢"改由**显式的启动宽限**解决：见 `set_startup_grace`。
        self._tick_ewma = 0.0
        #: 启动宽限的截止时刻（见 `set_startup_grace`），0 表示无宽限
        self._grace_until = 0.0

        self.watcher = CloseIntentWatcher(
            logger=logger, marker=self.marker,
            dwell_ms=int(config.get("close_protection.prepause_dwell_ms", 300)))
        # ---- 层级3：确认框监测 ----
        # 与层级2 相互独立：层级2 防「用户点 ✕ 时时长还在跑」，
        # 层级3 防「层级2 被绕过（触摸/远程桌面/托盘菜单退出/钩子没起来）」。
        # 发现确认框后只做一件事：确保 PAUSED。不去点确认框上的按钮。
        self.confirm = None
        cd_cfg = config.get("close_protection.confirm_dialog", {}) or {}
        if cd_cfg.get("enabled", True) and str(cd_cfg.get("mode", "auto")).lower() != "off":
            self.confirm = cd.ConfirmWatch(config, logger)
        self._state = DurationState.UNKNOWN
        self._pause_cb = pause_callback
        self._escaping = False
        self._jobs: "queue.Queue" = queue.Queue(maxsize=32)
        self._worker = None
        self._stop = threading.Event()
        self._busy = threading.Lock()           # 单飞：同一时刻只跑一条暂停流程
        self._last_prepause = 0.0
        self.events: list = []                  # 供 UI/测试读取的事件流水
        self._seq = 0                           # 事件单调序号（供消费端增量读取）
        self.errors: list = []                  # worker 内异常（绝不静默吞掉，§三十三）
        self.stats = {"swallowed": 0, "replayed": 0, "false_swallow": 0,
                      "blocked": 0, "prepause": 0, "prepause_ok": 0,
                      "prepause_skipped": 0, "prepause_submitted": 0, "handled": 0,
                      "confirm_seen": 0, "confirm_paused": 0}

    # -------------------------------------------------------------- 生命周期
    def bind(self, hwnd: int) -> None:
        if hwnd != self.hwnd:
            self.hwnd = hwnd
            if self.confirm is not None:
                self.confirm.bind(hwnd, _pid_of(hwnd))
            if self.log:
                self.log.info("关闭保护 v2 绑定窗口 HWND=0x%X｜%s", hwnd,
                              ci.describe_zone(cf.get_window_rect(hwnd), self.zone)
                              if hwnd else "无窗口")

    def start(self) -> bool:
        """启动两层保护。任一层起来了就启动消费端 worker。

        两层**相互独立**：层级3（确认框监测）不依赖输入层钩子。
        因此 `swallow_close_click=false` 或钩子安装失败时，
        只要确认框监测可用，仍然是有效的降级保护，不能一并放弃。
        """
        input_ok = False
        if self.enabled:
            input_ok = self.watcher.start()
            if not input_ok:
                self._emit("degraded", reason=f"低层鼠标钩子安装失败：{self.watcher.error}")
        elif self.log:
            self.log.warning("输入层关闭保护已被配置关闭（swallow_close_click=false）")

        confirm_ok = self.confirm.start() if self.confirm is not None else False
        if not (input_ok or confirm_ok):
            return False
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="close-guard", daemon=True)
        self._worker.start()
        if input_ok and not is_elevated():
            # Fail Safe 的另一面：能力不足必须**明说**，不能静默假装可用
            self._emit("not_elevated",
                       reason="本程序未提权，雷神恒以管理员运行 → 输入层拦截会被 UIPI 无效化")
            if self.log:
                self.log.error("关闭保护 v2：**未提权**，对提权运行的雷神无效（UIPI）。"
                               "请以管理员身份运行本程序。")
        return True

    def stop(self) -> None:
        self._stop.set()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass
        t = self._worker
        if t:
            t.join(timeout=2.0)
        self._worker = None
        self.watcher.stop()
        if self.confirm is not None:
            self.confirm.stop()

    # ------------------------------------------------------------------ 状态
    def set_state(self, state: DurationState) -> None:
        self._state = state

    def set_enabled(self, on: bool) -> None:
        """临时关闭/开启保护（例：已放行关闭、用户关掉保护开关）。

        关掉时**不做**任何「重放」，因为此时被吞住的点击已经过去了；
        真正要做的是让钩子立刻停止吞点，把控制权还给用户。

        `protect_on` 与 `enabled` 的区别：前者是「用户是否要这层保护」，
        后者是「输入层吞点是否可用」。层级3（确认框监测）只看前者，
        因此配置里关掉输入层不会连带把确认框监测也关掉。
        """
        self.protect_on = bool(on)
        self.set_swallow(self.protect_on)
        if self.confirm is not None:
            self.confirm.set_enabled(self.protect_on)

    def set_swallow(self, on: bool) -> None:
        """只开关「输入层吞点」，**不动层级3**。

        单独提供这个开关是因为两者确实需要分开控制：
          · 用户显式选择「允许关闭」→ 整层保护都该停（用 `set_enabled`）；
          · 诊断/观测需要「不吞点但仍监测确认框」→ 用本方法。
        """
        self.enabled = bool(on) and bool(
            self.config.get("close_protection.swallow_close_click", True))
        if not self.enabled:
            self.watcher.arm(0.0)          # 死手立即过期 → 钩子不再吞点

    def set_pause_callback(self, fn) -> None:
        """`fn() -> DurationState`：执行「确保已暂停」并返回**重新检测后**的状态。

        返回 `PAUSED` 才允许放行（§十六）。返回 `UNKNOWN`/`RUNNING` 一律不放行。
        """
        self._pause_cb = fn

    def allow_once(self) -> None:
        """逃生通道：用户显式选择「允许关闭雷神（跳过保护）」。"""
        self._escaping = True
        if self.log:
            self.log.warning("用户显式放行关闭（跳过保护）HWND=0x%X", self.hwnd)
        self._emit("escaping", reason="用户选择了放行")

    def clear_escape(self) -> None:
        self._escaping = False

    def snapshot(self) -> ci.GuardSnapshot:
        hwnd = self.hwnd
        if not (hwnd and window_alive(hwnd)):
            return ci.GuardSnapshot(armed=False)
        return ci.GuardSnapshot(
            hwnd=hwnd,
            rect=cf.get_window_rect(hwnd),
            state=self._state.value,
            enabled=self.enabled,
            escaping=self._escaping,
            foreground=int(user32.GetForegroundWindow() or 0),
            zone=self.zone,
            armed=not self._stop.is_set(),
            ts=time.time(),
        )

    # ------------------------------------------------------------ 主循环入口
    def set_startup_grace(self, seconds: float) -> None:
        """给**启动后的第一轮 tick** 一段宽限：这段时间内窗口直接给到上限。

        为什么需要它（真机证据 2026-09-30 19:40:49→19:40:59）：
        首轮 tick 要等 Chromium 建好无障碍树、并构造 RapidOCR 引擎，
        实测要 **10 秒**量级；而这时 `_tick_ewma` 还没量到任何值，
        `_arm_window()` 会退化成 2.5 秒 → 首轮就被顶穿 →
        日志出现 `[degraded] 消费端超时未刷新`，恰好在"刚启动、用户最可能点 ✕"的时段。

        为什么不做成"初值就给上限"：那会让**消费端从未运行**时也吞 10 秒点击，
        用户会被锁死（`test_close_guard.case_deadman` 专门盯这条）。
        所以宽限必须是**显式的、由引擎主动申请的、且有时限的**：
        引擎知道自己即将做一轮昂贵的初始化，就明说；其余场景一律按配置值走。
        """
        self._grace_until = time.time() + max(0.0, float(seconds))

    def _arm_window(self) -> float:
        """死手开关窗口：必须大于「一次 tick 的耗时」，否则钩子会周期性自行解锁。

        死手要防的是**消费端已经死了**，不是消费端**正忙**。而一次 tick 里包含一次
        整窗 OCR（本机实测 1.3s 量级），固定 2.5s 的窗口在稍慢的机器/更大的窗口上
        就会被顶穿 —— 表现为「吞点时灵时不灵，点了 ✕ 有时拦有时不拦」，
        比完全不吞还难排查。所以按实测 tick 间隔自适应放宽，并设上限：
        消费端真的死掉时，最迟 `swallow_deadman_max_ms`（默认 10s）一定停止吞点。
        """
        if self._grace_until and time.time() < self._grace_until:
            return self._max_arm                      # 启动宽限：显式申请，有时限
        if self._tick_ewma <= 0:
            return self.deadman
        return min(self._max_arm, max(self.deadman, self._tick_ewma * 3.0))

    def maintain(self) -> None:
        """由主循环每轮调用（周期必须短于 `swallow_deadman_ms`）。

        做三件事：刷新快照与死手开关、消费被吞掉的点击、监测确认框。
        输入层钩子没起来时仍要跑层级3，因此**不能**在最外层因为
        「钩子未安装」就整段 return（那会让降级保护形同不存在）。
        """
        w = self.watcher
        # 先量出本轮 tick 的真实间隔：它就是死手窗口的下限依据（见 `_arm_window`）。
        # 这段放在钩子判定**之前**，因为「消费端多快算正常」与钩子装没装上无关，
        # 而且状态里报出来的 `arm_ms` 也就始终有意义。
        now = time.time()
        if self._last_maintain > 0:
            # 单次尖峰必须被算进去：把窗口顶穿的就是"某一轮特别慢"，
            # 而纯 EWMA 会把尖峰抹平（0.3 权重），于是窗口永远跟不上它。
            # 上限夹到 max_arm，免得一次异常长的 tick 把窗口推到无穷。
            dt = min(now - self._last_maintain, self._max_arm)
            self._tick_ewma = dt if self._tick_ewma <= 0 else (
                self._tick_ewma * 0.7 + dt * 0.3)
        self._last_maintain = now

        if w.installed:
            if w.degraded:
                w.degraded = False
                self._emit("degraded", reason="消费端超时未刷新，已自动停止吞点（离手保护）")
            w.update_snapshot(self.snapshot())
            w.arm(self._arm_window())

            # 悬停样本始终取出（哪怕本次不启用预暂停），避免留下过期样本，
            # 造成「后来打开预暂停开关，却对着一分钟前的旧光标位置执行暂停」。
            h = w.pop_hover()
            if self.prepause and h and self._state is not DurationState.PAUSED:
                cooldown = max(0.5, int(self.config.get(
                    "duration.retry_interval_ms", 1500)) / 1000.0)
                if now - self._last_prepause >= cooldown and not self._busy.locked():
                    self._last_prepause = now
                    self.stats["prepause_submitted"] += 1
                    self._submit({"kind": "prepause", "x": h[0], "y": h[1], "ts": h[2]})

            for it in w.pop_swallowed():
                x, y, ts, state_at_click = it
                self._submit({"kind": "swallow", "x": x, "y": y,
                              "ts": ts, "state_at_click": state_at_click})

        self._check_confirm()

    def _check_confirm(self) -> None:
        """层级3：确认框监测。发现确认框 → 交给消费端确保暂停。

        刻意**不**在钩子回调里做这件事：`SetWinEventHook` 的
        `WINEVENT_OUTOFCONTEXT` 回调由系统在**本进程线程**里执行，
        在里面截图 + OCR 会把该线程阻塞住（事件丢失），
        所以只入队、由主循环消费。
        """
        cw = self.confirm
        if cw is None or not self.protect_on or self._escaping:
            return
        if not (self.hwnd and window_alive(self.hwnd)):
            return
        try:
            hit = cw.poll()
        except Exception as e:
            self.errors.append(f"confirm.poll: {type(e).__name__}: {e}")
            if self.log:
                self.log.exception("确认框监测异常：%s", e)
            return
        if not hit:
            return
        self.stats["confirm_seen"] += 1
        self._emit("confirm_dialog", detail=hit.get("detail") or "",
                   via=hit.get("via") or "")
        if not cw.pause_on_dialog or self._state is DurationState.PAUSED:
            return
        if self._busy.locked():
            return                       # 已有暂停流程在跑，不重复发起
        self._submit({"kind": "confirm", "via": hit.get("via") or ""})

    # ---------------------------------------------------------------- 内部
    def _submit(self, job) -> None:
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            pass

    def _emit(self, kind: str, **kw) -> None:
        # `i` 是单调递增序号：消费端（引擎）据此增量取事件，
        # 不会因为 events 列表被裁剪（>500 时删前 200 条）而漏读或重读。
        self._seq += 1
        ev = {"i": self._seq, "t": time.time(), "ev": kind, **kw}
        self.events.append(ev)
        if len(self.events) > 500:
            del self.events[:200]
        if self.log:
            self.log.info("关闭保护v2 [%s] %s", kind,
                          kw.get("reason") or kw.get("detail") or "")
        if self.sink is not None:
            try:
                self.sink.emit(kind, **kw)
            except Exception:
                pass

    def _ensure_paused(self) -> DurationState:
        """跑一次「确保已暂停」，返回**重新检测后**的状态。"""
        if self._pause_cb is None:
            return self._state
        if not self._busy.acquire(blocking=False):
            return self._state               # 已有流程在跑，不重复发起
        try:
            res = self._pause_cb()
            # 返回值类型是**契约**：调用方可能为了带诊断信息而返回 dict / 元组 / None。
            # 直接 `self._state = res` 会把状态污染成非枚举，此后
            # `res is DurationState.PAUSED` 恒假 → 暂停明明成功却永远判「未确认」
            # → blocked → 永不重放（真机实测踩到，代价两轮真机验证）。
            # 类型不对就**如实报错并保持原状态**，绝不能当成 PAUSED（§三十四）。
            if isinstance(res, DurationState):
                self._state = res
            elif res is not None:
                self.errors.append(
                    f"暂停回调返回了非 DurationState（{type(res).__name__}），已忽略")
                self._emit("pause_cb_bad_type",
                           reason=f"回调返回 {type(res).__name__}，状态未更新")
            return self._state
        except Exception as e:
            if self.log:
                self.log.exception("暂停流程抛异常：%s", e)
            return self._state
        finally:
            self._busy.release()

    def _handle(self, job) -> None:
        self.stats["handled"] += 1
        kind = job.get("kind")

        # ---- 层级3：发现雷神的确认框 → 确保暂停（不去点它的按钮） ----
        # 这一路的触发与「用户点 ✕」无关，可能来自触摸、远程桌面、
        # 托盘右键菜单退出，或输入层钩子失效的时候。
        if kind == "confirm":
            if self._state is DurationState.PAUSED:
                return
            res = self._ensure_paused()
            if res is DurationState.PAUSED:
                self.stats["confirm_paused"] += 1
                self._emit("confirm_paused",
                           detail="发现雷神确认框，已自动暂停总时长"
                                  "（用户随后选「最小化到托盘」或「真的退出」都不会再消耗）")
            else:
                self._emit("confirm_pause_failed",
                           reason=f"发现确认框但未能确认为 PAUSED（当前 {res.value}）",
                           detail="请手动点击雷神界面里的「暂停时长」")
            return

        if kind == "prepause":
            if self._state is DurationState.PAUSED:
                return
            hwnd, x, y = self.hwnd, job["x"], job["y"]
            # 跨进程核查（只能在消费端做）：光标此刻是否真的还在雷神身上。
            # 少了这一步，用户「划过」也会被当成「接近 ✕」而误暂停、打断加速。
            if not (hwnd and window_alive(hwnd) and cf.is_point_over_window(hwnd, x, y)):
                self.stats["prepause_skipped"] += 1
                return
            if not ci.in_hover_zone(cf.get_window_rect(hwnd), x, y, self.zone):
                self.stats["prepause_skipped"] += 1
                return
            res = self._ensure_paused()
            self.stats["prepause"] += 1
            if res is DurationState.PAUSED:
                self.stats["prepause_ok"] += 1
                self._emit("prepause_ok", detail="光标接近 ✕ 时已提前暂停，用户点下去即放行")
            else:
                self._emit("prepause_failed",
                           reason=f"提前暂停未能确认为 PAUSED（当前 {res.value}）")
            return

        # ---- 被吞掉的一次点击：先确保暂停，再决定是否放行 ----
        hwnd, x, y = self.hwnd, job["x"], job["y"]
        self.stats["swallowed"] += 1
        if not (hwnd and window_alive(hwnd)):
            return
        # 第 5 条联合条件：跨进程核查（只能在消费端做，钩子里做会拖垮钩子）
        if not cf.is_point_over_window(hwnd, x, y):
            self.stats["false_swallow"] += 1
            ok, why = cf.send_left_click(x, y, self.marker)
            self._emit("false_swallow", reason=f"核查未通过（{why}），已立即回放这次点击")
            return

        if self._state is DurationState.PAUSED:
            self._release(x, y, "点击时已是 PAUSED（悬停预暂停生效）")
            return

        res = self._ensure_paused()
        if res is DurationState.PAUSED:
            self._release(x, y, "已重新检测到 PAUSED，放行关闭")
        else:
            # §十四 / §十六 / §三十四：没有「重新检测到 PAUSED」就绝不放行
            self.stats["blocked"] += 1
            self._emit("close_blocked",
                       reason=f"未能确认已暂停（当前 {res.value}），已阻止关闭",
                       detail="请手动点击雷神界面里的「暂停时长」后重试")
            target = self.on_blocked
            if target is not None:
                try:
                    target(res.value)
                except Exception:
                    pass

    def _release(self, x: int, y: int, why: str) -> None:
        ok, detail = cf.send_left_click(x, y, self.marker)
        self.stats["replayed"] += 1
        self._emit("close_released", reason=why, detail=f"{detail}（{ok}）")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                self._handle(job)
            except Exception as e:
                # 绝不静默吞掉：logger 可能为 None（测试/嵌入场景），
                # 那就把异常留在 errors 里，并由 status() 暴露出去。
                self.errors.append(f"{type(e).__name__}: {e}")
                if self.log:
                    self.log.exception("关闭保护 v2 处理任务异常：%s", e)

    def status(self) -> dict:
        snap = self.snapshot()
        d = {
            "enabled": self.enabled, "protect_on": self.protect_on,
            "elevated": is_elevated(),
            "prepause": self.prepause, "hwnd": self.hwnd,
            "state": self._state.value, "escaping": self._escaping,
            "deadman_ms": int(self.deadman * 1000),
            # 实际武装出去的窗口（按实测 tick 间隔自适应，见 `_arm_window`）
            "arm_ms": int(self._arm_window() * 1000),
            "tick_ms": int(self._tick_ewma * 1000),
            "stats": dict(self.stats),
            "watcher": self.watcher.status(),
        }
        d["confirm"] = self.confirm.status() if self.confirm is not None else {"enabled": False}
        if snap.rect:
            d["zone"] = ci.describe_zone(snap.rect, self.zone)
        return d


# ---------------------------------------------------------------------------
# 关闭保护（系统关闭路径的兜底：禁用 SC_CLOSE）
# ---------------------------------------------------------------------------
class CloseProtection:
    def __init__(self, config, logger=None):
        self.config = config
        self.log = logger
        self.hwnd = 0
        self.last_report = ProtectionReport()
        self._forced_allow_hwnd = 0      # 逃生通道：用户显式选择「允许关闭雷神」
        self._last_enforce_ts = 0.0
        self._last_applied = None
        self.mouse_watcher = None
        self.key_watcher = None
        # 关闭意图的消费观测（真机排查用：区分「钩子没看到」/「被判过期」/「命中测试不过」）
        self.intent_stats = {"accepted": 0, "expired": 0, "missed": 0}
        self.last_intent_note = ""
        self._last_intent_pop = 0.0
        self._intent_interval = 0.0
        self._intent_max_age_base = max(0.5, int(config.get(
            "close_protection.intent_max_age_ms", 1500)) / 1000.0)
        self._intent_max_age_cap = max(self._intent_max_age_base, int(config.get(
            "close_protection.intent_max_age_max_ms", 5000)) / 1000.0)
        if self.config.get("close_protection.detect_close_intent", True):
            self.mouse_watcher = MouseClickWatcher(logger)
            self.key_watcher = KeyPressWatcher(logger)

    # ---------------------------------------------------------------- 绑定
    def bind(self, hwnd: int) -> None:
        if hwnd != self.hwnd:
            if self.log:
                self.log.info("关闭保护绑定窗口 HWND=0x%X", hwnd)
            self.hwnd = hwnd
            self._last_applied = None
            self._last_enforce_ts = 0.0

    def start_watcher(self) -> bool:
        ok = False
        if self.mouse_watcher is not None:
            ok = self.mouse_watcher.start() or ok
        if self.key_watcher is not None:
            ok = self.key_watcher.start() or ok
        return ok

    def stop_watcher(self) -> None:
        for w in (self.mouse_watcher, self.key_watcher):
            if w is not None:
                w.stop()

    # ---------------------------------------------------------------- 策略
    def policy(self, state: DurationState, pause_failed: bool = False) -> CloseDecision:
        enabled = bool(self.config.get("close_protection.enabled", True))
        if enabled and self._forced_allow_hwnd and self._forced_allow_hwnd == self.hwnd:
            return CloseDecision.ALLOW       # 用户已明确要求放行
        return decide_close_action(
            state,
            enabled=enabled,
            block_when_unknown=bool(self.config.get("close_protection.block_when_unknown", True)),
            block_when_pause_failed=bool(self.config.get("close_protection.block_when_pause_failed", True)),
            pause_failed=pause_failed,
        )

    # ---------------------------------------------------------------- 执行
    def menu_state(self):
        hmenu = user32.GetSystemMenu(wt.HWND(int(self.hwnd)), False)
        if not hmenu:
            return None, None
        state = user32.GetMenuState(hmenu, cf.SC_CLOSE, MF_BYCOMMAND)
        return hmenu, state

    def is_sc_close_disabled(self) -> bool:
        _, state = self.menu_state()
        if state is None or state == 0xFFFFFFFF:
            return False
        return bool(state & MF_GRAYED)

    def enforce(self, decision: CloseDecision, force: bool = False) -> ProtectionReport:
        """把策略落到系统菜单上。幂等：状态没变就不重复调用。"""
        if not self.hwnd or not window_alive(self.hwnd):
            return ProtectionReport(decision, False, "窗口无效", time.time())

        want_disabled = decision.blocked
        now = time.time()
        interval = int(self.config.get("close_protection.reapply_interval_ms", 2000)) / 1000.0
        if (not force and self._last_applied is want_disabled
                and now - self._last_enforce_ts < interval):
            return self.last_report

        hmenu, _ = self.menu_state()
        if not hmenu:
            rep = ProtectionReport(decision, False, "取系统菜单失败（窗口可能没有系统菜单）", now)
            self.last_report = rep
            return rep

        flags = MF_BYCOMMAND | (MF_GRAYED if want_disabled else MF_ENABLED)
        user32.EnableMenuItem(hmenu, cf.SC_CLOSE, flags)
        # 刷新标题栏，让 ✕ 的灰/亮立刻可见
        user32.DrawMenuBar(wt.HWND(int(self.hwnd)))
        user32.InvalidateRect(wt.HWND(int(self.hwnd)), None, True)

        actually = self.is_sc_close_disabled()
        detail = (f"{'禁用' if want_disabled else '启用'} SC_CLOSE → 实际状态 "
                  f"{'禁用' if actually else '启用'}")
        if want_disabled and not actually:
            detail += "（未生效，可能因雷神提权运行而本程序未提权）"
        rep = ProtectionReport(decision, actually, detail, now)
        self._last_applied = want_disabled
        self._last_enforce_ts = now
        self.last_report = rep
        if self.log:
            self.log.info("关闭保护：%s（决策=%s）", detail, decision.value)
        return rep

    # ------------------------------------------------------------ 关闭意图
    def is_close_button_at(self, x: int, y: int) -> bool:
        """该屏幕点是否正压在雷神的「关闭」按钮上。

        用 WM_NCHITTEST 让**窗口自己**回答命中码，比按坐标猜区域可靠得多
        （规格书 §十三 明确反对「鼠标位置==右上角」这种简单判断）。
        """
        if not self.hwnd or not window_alive(self.hwnd):
            return False
        try:
            return cf.hit_test(self.hwnd, x, y) == cf.HTCLOSE
        except Exception:
            return False

    def _intent_max_age(self) -> float:
        """关闭意图的「新鲜度」上限，按消费端的实际节拍自适应。

        这条上限的**本意**是「很久以前的点击不该触发保护」。但它有个硬约束：必须
        **大于消费端两次消费之间的间隔**，否则事件会纯粹因为「排在自己人后面」而被
        判过期 —— 那不是策略，是 bug。实测本机一次 tick 被整窗 OCR 拖到 1.4s，
        固定 1.5s 的上限刚好卡在临界点上，表现为「注入成功但引擎毫无反应」。
        所以按实测消费间隔放宽（2× 间隔 + 余量），并保留上限：
        超过 `intent_max_age_max_ms`（默认 5s）的点击仍然按过期丢弃。
        """
        if self._intent_interval <= 0:
            return self._intent_max_age_base
        return min(self._intent_max_age_cap,
                   max(self._intent_max_age_base, self._intent_interval * 2.0 + 0.3))

    def pop_close_intents(self, max_age: float = None) -> list:
        """取出「判定为点了 ✕」的点击（丢弃过期的）。

        WM_NCHITTEST 的命中测试放在这里（消费端）执行，而不是钩子回调里，
        避免跨进程 SendMessage 拖慢钩子导致被系统摘掉。
        """
        if self.mouse_watcher is None:
            return []
        now = time.time()
        if self._last_intent_pop > 0:
            dt = now - self._last_intent_pop
            self._intent_interval = dt if self._intent_interval <= 0 else (
                self._intent_interval * 0.7 + dt * 0.3)
        self._last_intent_pop = now
        if max_age is None:
            max_age = self._intent_max_age()
        out = []
        for x, y, ts in self.mouse_watcher.pop():
            if now - ts > max_age:
                self.intent_stats["expired"] += 1
                self.last_intent_note = (f"有点击被判过期（停留 {now - ts:.2f}s > "
                                         f"上限 {max_age:.2f}s）")
                continue
            if self.is_close_button_at(x, y):
                self.intent_stats["accepted"] += 1
                out.append({"kind": "mouse", "x": x, "y": y, "ts": ts})
            else:
                self.intent_stats["missed"] += 1
        return out

    def pop_alt_f4_intents(self, max_age: float = None) -> list:
        """取出「Alt+F4 且雷神在前台」的关闭意图。"""
        if self.key_watcher is None or not self.hwnd or not window_alive(self.hwnd):
            return []
        now = time.time()
        if max_age is None:
            max_age = self._intent_max_age()
        out = []
        for _vk, ts in self.key_watcher.pop():
            if now - ts > max_age:
                continue
            if cf.user32.GetForegroundWindow() == self.hwnd:
                out.append({"kind": "alt_f4", "ts": ts})
        return out

    def close_window(self) -> bool:
        """放行并主动关闭雷神（用于「暂停成功后替用户完成关闭」）。"""
        if not self.hwnd or not window_alive(self.hwnd):
            return False
        self.enforce(CloseDecision.ALLOW, force=True)
        time.sleep(0.15)
        user32.PostMessageW(wt.HWND(int(self.hwnd)), cf.WM_SYSCOMMAND, cf.SC_CLOSE, 0)
        if self.log:
            self.log.info("已放行并请求关闭雷神窗口 HWND=0x%X", self.hwnd)
        return True

    # ------------------------------------------------------------ 逃生通道
    def allow_once(self) -> None:
        """用户显式选择「允许关闭雷神（跳过保护）」。"""
        self._forced_allow_hwnd = self.hwnd
        self.enforce(CloseDecision.ALLOW, force=True)
        if self.log:
            self.log.warning("用户显式放行关闭（跳过保护）HWND=0x%X", self.hwnd)

    def clear_escape(self) -> None:
        self._forced_allow_hwnd = 0

    def release(self) -> None:
        """程序退出前务必调用：把 ✕ 恢复成可用，别把用户的雷神锁死。"""
        try:
            if self.hwnd and window_alive(self.hwnd):
                self.enforce(CloseDecision.ALLOW, force=True)
        except Exception:
            pass
