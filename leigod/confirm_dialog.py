"""层级3：确认框监测（关闭保护 v2 的第三层）。

## 它在防什么

真机实测（2026-09-29）：点雷神自绘的 ✕ **不关窗、也不走 `SC_CLOSE`**，
而是弹出雷神自己的确认框，询问「最小化到托盘」还是「真的退出」。
由此产生一个层级2（输入层吞点）**覆盖不到的漏洞**：

  · 用户用**触摸屏**点 ✕ —— 不产生 `WH_MOUSE_LL` 的鼠标消息（走 `WM_TOUCH`）；
  · 用户通过**远程桌面**操作 —— 合成输入由 RDP 会话注入，本机低层钩子未必看到；
  · 用户从**托盘右键菜单**选「退出」 —— 根本不经 ✕；
  · 本程序未运行/钩子安装失败时，用户照样能点 ✕。

上述任何一种情况下，用户都能直接走到确认框并选「真的退出」，
**在总时长仍处于 RUNNING 时把雷神关掉** —— 正是本项目要防的事。

## 本层的职责边界（刻意不做的事）

**不与用户的选择对抗。** 发现确认框后，本层只做一件事：
**立刻确保总时长已暂停**。这样用户接下来无论选「最小化到托盘」还是「真的退出」，
都不会再损失时长。它**不去点**确认框上的任何按钮，也不阻止用户退出雷神——
「用户想关掉雷神」是正当诉求，本项目的目标只是「别让总时长白白跑」。

## 两条检测通路（真机形态未定，故都实现）

| 通路 | 适用 | 依据 |
| --- | --- | --- |
| `winevent` | 确认框是**独立顶层窗口** | `SetWinEventHook` 收到该 pid 的新窗口事件 |
| `ocr` | 确认框是 **Electron 页内绘制** | 截主窗口图 → OCR 找到按钮文案 |

`mode="auto"`（默认）两条都开，谁先命中用谁，并在事件里记录 `via`。
这样**不必先知道真机形态**就能工作；真机观测完（`tests/probe_real_close_sequence.py`）
再把 `mode` 固定成实际那条，省掉另一条的运行开销。

## 合规性（规格书 §四）

`SetWinEventHook` 是 Windows 官方提供的**跨进程旁观**接口：事件由系统回调到本进程，
**不注入 DLL、不修改目标进程、不发送任何网络请求**。

## 判据为什么要求「两组关键词同时命中」

OCR 是对**整个窗口**做的，主界面本身可能出现「退出」之类的字。只匹配一组关键词
必然会误报（把主界面文字当成确认框）。因此默认要求
**「最小化」组与「退出」组同时命中**——这正是确认框独有的文字组合，
与 `close_intent.should_swallow` 的「联合判据」思路一致（规格书 §十三）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from collections import deque

from detection import coordinate_fallback as cf
from detection import ocr as ocr_mod

user32 = ctypes.windll.user32

# 显式声明「返回句柄」的 API：ctypes 默认按 32 位解释返回值会把 64 位句柄截断。
# （同一个坑在 detection/coordinate_fallback.py 里已踩过一次，见其 _declare_prototypes 注释。）
user32.GetWindow.restype = ctypes.c_void_p
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [wt.HWND]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetDesktopWindow.restype = ctypes.c_void_p
user32.IsWindow.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]

# ---- WinEvent 常量 ----
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0
CHILDID_SELF = 0

GA_PARENT = 1
GW_OWNER = 4

#: 确认框的两组按钮文案。要求**两组同时命中**才算发现确认框。
#: 之所以把「最小化」放在必命中组：它是确认框里最具辨识度的词
#: （主界面不会出现），而「退出」单独出现太容易误报。
KEYWORDS_MINIMIZE = ("最小化到托盘", "最小化")
KEYWORDS_EXIT = ("真的退出", "确定退出", "退出程序")

#: **强特征词**：单独命中即可判定为确认框，不受 `require_both` 约束。
#:
#: 真机实测依据（雷神 v11.3.2.9）：确认框出现时 OCR **稳定识别出「最小化到托盘」**，
#: 但「真的退出」那一组常常识别不出来（确认框文字与背后的主界面文字叠在一起，
#: RapidOCR 把它认成了别的字）。而 `require_both` 要求两组同时命中 →
#: 结果「明明看见了确认框，却判成不是」（`last_detail` 会写
#: 「只命中一组关键词（最小化=[...] 退出=[]）」）。
#:
#: 为什么只有完整短语算强特征、而短词「最小化」不算：
#: 「最小化」两个字可能在别处（网页正文、其它软件界面）出现，会误报；
#: 而「最小化到托盘」这种完整按钮文案在雷神主界面上不会出现，是确认框的独有特征。
STRONG_KEYWORDS = ("最小化到托盘",)

#: 默认识别参数
DEFAULT_MIN_CONFIDENCE = 0.4
DEFAULT_TITLE_SETTLE_MS = 400


# ---------------------------------------------------------------------------
# 纯逻辑：从 OCR 结果里判确认框（可单测，不依赖 RapidOCR / 窗口）
# ---------------------------------------------------------------------------
def match_confirm_lines(lines, min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                        require_both: bool = True,
                        kw_min: tuple = KEYWORDS_MINIMIZE,
                        kw_exit: tuple = KEYWORDS_EXIT,
                        kw_strong: tuple = STRONG_KEYWORDS) -> dict:
    """从 OCR 结果判定是否为雷神的「确认框」。

    返回 `{found, min_hits, exit_hits, strong_hits, joined, detail}`。

    判定顺序：
      1. **强特征词**（如完整的「最小化到托盘」）命中 → 直接算确认框，
         不受 `require_both` 约束（真机依据见 `STRONG_KEYWORDS` 的注释）；
      2. 否则按 `require_both`：True 要求「最小化」与「退出」两组同时命中
         （只命中「退出」太容易误报，主界面就有「退出登录」之类），
         False 则命中任一组即可。
    """
    texts = []
    for line in (lines or []):
        try:
            if float(line.get("confidence", 0)) < min_confidence:
                continue
        except (TypeError, ValueError):
            continue
        t = ocr_mod.normalize(line.get("text") or "")
        if t:
            texts.append(t)
    joined = " ".join(texts)
    min_hits = [k for k in kw_min if ocr_mod.normalize(k) in joined]
    exit_hits = [k for k in kw_exit if ocr_mod.normalize(k) in joined]
    strong_hits = [k for k in kw_strong if ocr_mod.normalize(k) in joined]
    if require_both:
        normal = bool(min_hits) and bool(exit_hits)
    else:
        normal = bool(min_hits) or bool(exit_hits)
    found = bool(strong_hits) or normal
    if not texts:
        detail = "未识别到可用文字"
    elif strong_hits and not normal:
        detail = (f"命中强特征词 {strong_hits} → 判为确认框"
                  f"（最小化={min_hits} 退出={exit_hits}；"
                  "退出组未识别出来在真机上是常态，见 STRONG_KEYWORDS）")
    elif found:
        detail = f"识别到确认框文案（最小化={min_hits} 退出={exit_hits}）"
    elif min_hits or exit_hits:
        detail = (f"只命中一组关键词（最小化={min_hits} 退出={exit_hits}），"
                  "按联合判据不算确认框")
    else:
        detail = f"未见确认框文案：{joined[:120]}"
    return {"found": found, "min_hits": min_hits, "exit_hits": exit_hits,
            "strong_hits": strong_hits, "joined": joined, "detail": detail}


def is_candidate_window(info: dict, main_hwnd: int, pid: int,
                        known: set | None = None) -> bool:
    """该窗口信息是否可能是「确认框」。

    过滤条件（顺序即短路顺序，先便宜的判断）：
      · 不是主窗口自己；不属于已知窗口集合；
      · 属目标 pid；是**顶层窗口**；可见；
      · 尺寸不像「零碎窗口」（宽高都 ≥ 60px，排除 tooltip / 输入法候选框）；
      · 不在屏幕外（雷神最小化到托盘时主窗口会被挪到 -25600）。

    ⚠️ 「是否顶层」绝不能用 `GetParent(hwnd) != 0` 判断：按 Win32 文档，
    `GetParent` 对带 `WS_POPUP` 的窗口返回的是**属主(owner)** 而不是父窗口。
    确认框正是「WS_POPUP + 以主窗口为 owner」这种形态，
    用 `GetParent` 判会把真正的确认框全部误杀（实测踩过）。
    正确做法是 `GetAncestor(hwnd, GA_PARENT) == 桌面窗口`。
    """
    if not info:
        return False
    hwnd = int(info.get("hwnd") or 0)
    if not hwnd or hwnd == int(main_hwnd or 0):
        return False
    if known and hwnd in known:
        return False
    if int(info.get("pid") or 0) != int(pid or 0):
        return False
    if not info.get("top_level", True):
        return False
    if not info.get("visible"):
        return False
    w, h = (info.get("size") or [0, 0])[:2]
    if w < 60 or h < 60:
        return False
    if info.get("offscreen"):
        return False
    return True


# ---------------------------------------------------------------------------
# 窗口信息 / 枚举
# ---------------------------------------------------------------------------
def window_info(hwnd) -> dict:
    """读取窗口的轻量信息（比 Window Inspector 少，够本模块判断即可）。"""
    h = wt.HWND(int(hwnd))
    r = wt.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    tbuf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(h, tbuf, 512)
    # 顶层判定：GA_PARENT 指向桌面窗口即为顶层。
    # （不能用 GetParent——它对 WS_POPUP 返回的是 owner，会把确认框误判成子窗口。）
    ga_parent = int(user32.GetAncestor(h, GA_PARENT) or 0)
    return {
        "hwnd": int(hwnd), "hwnd_hex": hex(int(hwnd)),
        "class": buf.value, "title": tbuf.value, "pid": int(pid.value),
        "rect": [r.left, r.top, r.right, r.bottom],
        "size": [r.right - r.left, r.bottom - r.top],
        "visible": bool(user32.IsWindowVisible(h)),
        "top_level": ga_parent == get_desktop_window(),
        "parent": ga_parent,
        "owner": int(user32.GetWindow(h, GW_OWNER) or 0),
        "offscreen": (r.left < -20000 or r.top < -20000),
    }


def get_desktop_window() -> int:
    user32.GetDesktopWindow.restype = ctypes.c_void_p
    return int(user32.GetDesktopWindow() or 0)


def list_top_level(pid: int) -> list:
    """枚举该 pid 的所有顶层窗口（判定方式与 `window_info` 一致）。"""
    desktop = get_desktop_window()
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        try:
            p = wt.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value == pid and int(user32.GetAncestor(h, GA_PARENT) or 0) == desktop:
                out.append(int(h))
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return out


def _is_visible(hwnd) -> bool:
    """轻量可见性判断（单个系统调用）。`_init_known` 专用，见其注释。"""
    try:
        return bool(user32.IsWindowVisible(wt.HWND(int(hwnd))))
    except Exception:
        return False


def pid_of_window(hwnd) -> int:
    if not hwnd:
        return 0
    p = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(int(hwnd)), ctypes.byref(p))
    return int(p.value)


# ---------------------------------------------------------------------------
# 通路1：SetWinEventHook
# ---------------------------------------------------------------------------
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)


class WindowEventListener:
    """自带消息循环的线程里装 `SetWinEventHook`，回调只做入队。

    回调里只 append 一个整数到 deque —— 任何耗时操作都会让系统认为钩子超时。
    """

    def __init__(self, pid: int = 0, logger=None, capacity: int = 512):
        self.pid = int(pid or 0)
        self.log = logger
        self._events = []
        self._lock = threading.Lock()
        self._hooks = []
        self._thread = None
        self._stop = threading.Event()
        self._proc = WINEVENTPROC(self._callback)
        self.installed = False
        self.error = ""
        self._capacity = capacity

    def _callback(self, hhook, event, hwnd, id_object, id_child, tid, ts):
        if id_object != OBJID_WINDOW or id_child != CHILDID_SELF:
            return
        with self._lock:
            if len(self._events) < self._capacity:
                self._events.append(int(hwnd or 0))

    def _run(self):
        user32.SetWinEventHook.restype = ctypes.c_void_p
        user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                           WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
        user32.UnhookWinEvent.restype = wt.BOOL
        user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        h = user32.SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW, None,
                                   self._proc, self.pid, 0, flags)
        if h:
            self._hooks.append(h)
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
        for hh in self._hooks:
            user32.UnhookWinEvent(hh)
        self._hooks = []
        self.installed = False

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return self.installed
        self._stop.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="winevent-confirm",
                                        daemon=True)
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
            t.join(timeout=2.0)
        self._thread = None

    def pop(self) -> list:
        with self._lock:
            out = list(self._events)
            self._events.clear()
        return out


# ---------------------------------------------------------------------------
# 编排：两条通路合一
# ---------------------------------------------------------------------------
class ConfirmWatch:
    """确认框监测。`poll()` 由消费端每轮调用。

    ⚠️ **`poll()` 必须是 O(1) 的，绝不能在里面跑 OCR。**
    消费端是本项目的主循环 tick，它同时承担三件不能被拖慢的事：
      ① 消费「点了 ✕」的关闭意图（`pop_close_intents` 有 1.5s 新鲜度上限，
         主循环一卡，真实点击就会被当成过期事件丢掉 —— 关闭保护直接失灵）；
      ② 刷新输入层钩子的**死手开关**（超时即停止吞点，吐点变得时灵时不灵）；
      ③ 维持界面状态的刷新节拍。
    整窗 OCR 单次几百毫秒到数秒，塞进 tick 就会把上面三件事一起拖垮。
    所以 OCR 跑在**自己的线程**里，`poll()` 只负责把已产出的结论取走。

    可注入 `lister` / `grabber` / `ocr_reader` 以便离线测试
    （靶机 v2 的确认框是真实顶层窗口，可用真实 `winevent` 通路验证）。
    """

    def __init__(self, config, logger=None, main_hwnd: int = 0, pid: int = 0,
                 lister=None, grabber=None, ocr_reader=None,
                 require_foreground: bool = True):
        c = config.get("close_protection.confirm_dialog", {}) or {}
        self.config = config
        self.log = logger
        self.main_hwnd = int(main_hwnd or 0)
        self.pid = int(pid or 0)
        self.mode = str(c.get("mode", "auto")).lower()
        self.enabled = self.mode != "off" and bool(c.get("enabled", True))
        self.poll_ms = max(150, int(c.get("poll_ms", 2000)))
        self.min_confidence = float(c.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
        self.require_both = bool(c.get("require_both", True))
        self.kw_min = tuple(c.get("keywords_minimize") or KEYWORDS_MINIMIZE)
        self.kw_exit = tuple(c.get("keywords_exit") or KEYWORDS_EXIT)
        self.pause_on_dialog = bool(c.get("pause_on_dialog", True))
        self.require_foreground = bool(require_foreground)

        self._lister = lister or list_top_level
        self._grabber = grabber or self._default_grab
        self._ocr = ocr_reader or ocr_mod.read_text

        self.listener = None
        self.known: set = set()
        self.recent_new: list = []      # 首次见到的窗口（不过滤，供排查）
        self._last_ocr = 0.0
        self.stats = {"winevent_hits": 0, "ocr_hits": 0, "checked": 0,
                      "ocr_skipped": 0, "errors": 0}
        self.last_detail = ""
        self.last_ocr_text = ""

        # OCR 扫描线程（见类注释：绝不能放在 poll() 里）
        self._hits: deque = deque(maxlen=8)
        self._hits_lock = threading.Lock()
        self._scan_thread = None
        self._scan_stop = threading.Event()
        self._scan_wake = threading.Event()

    # ------------------------------------------------------------ 默认实现
    @staticmethod
    def _default_grab(hwnd):
        arr = cf.capture_window_printwindow(hwnd)
        if arr is None:
            rect = cf.get_window_rect(hwnd)
            arr = cf.capture_region_screen(rect) if rect else None
        return arr

    def _init_known(self, visible_fn=None) -> None:
        """记下「启动时就已经在屏幕上」的窗口，避免把它们当成新出现的确认框。

        ⚠️ **只记可见窗口**，隐藏的一律不记 —— 这是真机踩出来的（雷神 v11.3.2.9）：

        Electron 的对话框常是**预建一个隐藏窗口、要显示时再 Show**（实测雷神进程里
        就有一个 `class=Chrome_WidgetWin_0`、尺寸 1536×904、`visible=False` 的窗口）。
        若按「该 pid 的所有顶层窗口」初始化 known，那个窗口就永远在 known 里，
        而 `_normalize_new()` 对 known 里的 hwnd 是 `continue` ——
        结果确认框**每次都真弹了，却永远监测不到**（`confirm_seen` 恒为 0）。

        语义上这也是对的：已经**看得见**的窗口才叫「启动前就存在」；
        一个隐藏窗口显示出来，对用户而言就是"新出现的窗口"。
        """
        try:
            hwnds = self._lister(self.pid) if self.pid else []
        except Exception:
            self.known = set()
            return
        # ⚠️ 只调 `IsWindowVisible`，**不要**在这里用 `window_info()`：
        # 后者一次要打 6 个系统调用（GetWindowRect/ClassName/WindowText/…），
        # 对上百个顶层窗口跑一遍会明显占用主线程 —— 实测把 UI 面板的布局节奏拖慢，
        # 表现为「面板高度自适应」这类依赖布局完成的断言间歇性失败。
        fn = visible_fn or _is_visible
        known = set()
        for h in hwnds:
            try:
                if fn(h):
                    known.add(int(h))
            except Exception:
                continue
        self.known = known

    # -------------------------------------------------------------- 生命周期
    def bind(self, main_hwnd: int, pid: int = 0) -> None:
        if int(main_hwnd or 0) != self.main_hwnd:
            self.main_hwnd = int(main_hwnd or 0)
            self.known = set()
        if pid:
            self.pid = int(pid)
        if self.listener is not None and self.pid and not self.listener.installed:
            self.listener.pid = self.pid
        # OCR 扫描线程依赖 main_hwnd，绑定时确保它已经起来
        self._ensure_scan()

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self.mode in ("auto", "winevent") and self.pid:
            self.listener = WindowEventListener(self.pid, self.log)
            if not self.listener.start():
                if self.log:
                    self.log.warning("确认框监测：SetWinEventHook 安装失败：%s",
                                     self.listener.error)
                self.listener = None
        self._init_known()
        self._ensure_scan()
        return True

    def set_enabled(self, on: bool) -> None:
        """由 `CloseIntentGuard` 控制的总开关。

        与「配置里关掉输入层」是两件事：本层在钩子安装失败时依然应当工作，
        因此只有用户**主动关掉整层保护**（或本层 mode=off）才停摆。
        """
        self.enabled = bool(on) and self.mode != "off" and bool(
            self.config.get("close_protection.confirm_dialog.enabled", True))
        if not self.enabled:
            self._scan_wake.set()          # 让扫描线程立刻从等待中醒来退出
            return
        # 之前是关的，可能连监听器都没建起来 → 这里补上
        if self.mode in ("auto", "winevent") and self.pid:
            if self.listener is None:
                self.listener = WindowEventListener(self.pid, self.log)
            if not self.listener.installed:
                self.listener.start()
        if not self.known:
            self._init_known()
        self._ensure_scan()

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        self._scan_stop.set()
        self._scan_wake.set()
        t = self._scan_thread
        if t is not None:
            t.join(timeout=2.0)
        self._scan_thread = None

    def status(self) -> dict:
        return {
            "mode": self.mode, "enabled": self.enabled,
            "main_hwnd": self.main_hwnd, "pid": self.pid,
            "hook_installed": bool(self.listener and self.listener.installed),
            "hook_error": (self.listener.error if self.listener else ""),
            "ocr_thread": bool(self._scan_thread and self._scan_thread.is_alive()),
            "pending_hits": len(self._hits),
            "known_windows": len(self.known),
            "recent_new": [
                {"hwnd": w["hwnd_hex"], "class": w["class"], "size": w["size"],
                 "top_level": w["top_level"], "visible": w["visible"],
                 "owner": hex(w["owner"]), "offscreen": w["offscreen"]}
                for w in self.recent_new[-8:]
            ],
            "stats": dict(self.stats), "last_detail": self.last_detail,
            "last_ocr_text": self.last_ocr_text,
        }

    # ------------------------------------------------------------------ 检测
    def _normalize_new(self, hwnds) -> list:
        """把候选 hwnd 收窄成「像确认框」的那些，并更新 known 集合。

        每一个首次见到的 hwnd 都会原样记进 `recent_new`（**不过滤**），
        这样万一全部被过滤掉，报告里仍能看到「到底来了些什么窗口」——
        而不是只看到一个干巴巴的 0，无从排查（真机观测尤其需要这个）。
        """
        out = []
        for h in hwnds:
            if not h or h in self.known:
                continue
            self.known.add(int(h))
            try:
                info = window_info(h)
            except Exception:
                self.stats["errors"] += 1
                continue
            self.recent_new.append(info)
            if len(self.recent_new) > 40:
                del self.recent_new[:-20]
            if is_candidate_window(info, self.main_hwnd, self.pid, None):
                out.append(info)
        return out

    def _drain_winevent(self) -> list:
        if self.listener is None:
            return []
        hwnds = self.listener.pop()
        if not hwnds:
            return []
        fresh = self._normalize_new(hwnds)
        if fresh:
            self.stats["winevent_hits"] += 1
        return fresh

    def _leigod_is_foreground(self) -> bool:
        """雷神（或其确认框）是否在前台。

        OCR 通路要整窗截图 + 识别，成本高（RapidOCR 单次几百毫秒起）。
        用户不在雷神上操作时，确认框不可能正在被点击，没必要扫这一趟。
        这个门槛是让本层「不拖慢主循环」的关键：主循环本来就要为状态识别做 OCR，
        再无条件加一路整窗 OCR 会明显加重负担。
        """
        fg = int(user32.GetForegroundWindow() or 0)
        if not fg:
            return False
        if fg == self.main_hwnd:
            return True
        return pid_of_window(fg) == self.pid

    def _scan_ocr(self) -> dict | None:
        if not self.main_hwnd:
            return None
        if self.require_foreground and not self._leigod_is_foreground():
            self.stats["ocr_skipped"] += 1
            return None
        try:
            arr = self._grabber(self.main_hwnd)
            if arr is None:
                return None
            res = match_confirm_lines(self._ocr(arr), self.min_confidence,
                                      self.require_both, self.kw_min, self.kw_exit)
        except Exception:
            self.stats["errors"] += 1
            return None
        self.last_detail = res["detail"]
        # 整窗 OCR 的原文留档：真机排障全靠它（例如"真的退出"实际被识别成了什么，
        # 只看"没命中"三个字是查不出原因的）。
        self.last_ocr_text = res.get("joined", "")[:300]
        if res["found"]:
            self.stats["ocr_hits"] += 1
            return {"via": "ocr", "detail": res["detail"],
                    "min_hits": res["min_hits"], "exit_hits": res["exit_hits"],
                    "strong_hits": res.get("strong_hits") or []}
        return None

    def poll(self) -> dict | None:
        """返回确认框信息（`{via, windows|detail}`），未发现返回 `None`。

        `auto` 模式下先看 `winevent`（零成本），再看 OCR 线程已经产出的结论。

        **本函数必须保持 O(1)**：它跑在引擎主循环里，卡住会连带丢掉关闭意图、
        让死手开关过期。所以这里绝不截图、绝不 OCR，只取结果。
        """
        if not self.enabled:
            return None

        fresh = self._drain_winevent()
        if fresh:
            # 立刻上报：越早确保暂停，用户选「真的退出」时损失越小。
            # 不需要等窗口绘制完——「确保暂停」这一步与确认框画没画好无关。
            return {"via": "winevent", "windows": fresh,
                    "detail": f"出现新的顶层窗口：{fresh[0]['class']}"}

        with self._hits_lock:
            if self._hits:
                return self._hits.popleft()
        return None

    # -------------------------------------------------------- OCR 扫描线程
    def _ensure_scan(self) -> None:
        """保证 OCR 扫描线程在跑（幂等；mode=off 或总开关关掉时不启动）。"""
        if not self.enabled or self.mode not in ("auto", "ocr"):
            return
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        self._scan_stop.clear()
        self._scan_thread = threading.Thread(target=self._scan_loop, name="confirm-ocr",
                                            daemon=True)
        self._scan_thread.start()

    def _scan_loop(self) -> None:
        """OCR 扫描线程主体：按 `poll_ms` 限频做「截图 + OCR」，命中就入队。

        为什么必须独立成线程：见类文档。一句话——消费端一次都不能被 OCR 阻塞。
        """
        while not self._scan_stop.is_set():
            # 用 Event 等待而不是 sleep：关闭时能立刻退出，不必等满一个 poll_ms
            self._scan_wake.wait(timeout=self.poll_ms / 1000.0)
            self._scan_wake.clear()
            if self._scan_stop.is_set():
                break
            if not self.enabled:
                continue
            try:
                self.stats["checked"] += 1
                hit = self._scan_ocr()
            except Exception as e:                     # 线程绝不能死，死了就静默失效
                self.stats["errors"] += 1
                if self.log:
                    self.log.exception("确认框 OCR 扫描异常：%s", e)
                continue
            if hit:
                with self._hits_lock:
                    self._hits.append(hit)
