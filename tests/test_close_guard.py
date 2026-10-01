"""关闭保护 v2 闭环验证（**结构仿真靶机 v2**，真机式的自绘 ✕ + 应用内确认框）。

## 这套测试要回答什么

真机实测已确认：雷神的 ✕ 由应用自绘、点击由渲染进程处理、
**不关窗也不走 `SC_CLOSE`**，只弹应用内确认框（见
`docs/真机发现-关闭保护架构问题.md` §2.3）。
所以关闭保护必须改成**输入层**：

    吞掉 ✕ 的点击 → 确保「重新检测到 PAUSED」→ 重放这次点击 → 雷神弹出它自己的确认框

本文件用真实鼠标点击 + 真实低层钩子验证这条链路，**不是**模拟调用。
每一步都用靶机自己写出的事件日志作为证据，而不是只看被测对象的自述。

## 证据链（关键）

被吞掉的点击**不会**出现在靶机日志里；只有重放之后靶机才会记 `chrome_close_clicked`。
因此**事件顺序本身就是「吞点是否真的发生」的证明**：

    期望顺序：chrome_button_clicked（暂停）→ chrome_close_clicked（重放到达）
    若吞点失效：chrome_close_clicked 会**先**出现（因为点击直接送达了应用）

这条断言无法被「只看窗口是否消失」这种弱判据伪造——那正是首版真机测试翻车的原因。
"""
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
sys.path.insert(0, HERE)

import harness as H                                    # noqa: E402
from core.state_machine import DurationState           # noqa: E402
from detection import coordinate_fallback as cf        # noqa: E402
from leigod.close_protection import CloseIntentGuard, is_elevated   # noqa: E402

user32 = ctypes.windll.user32
BM_CLICK = 0x00F5


class Pump:
    """在后台线程里按固定节拍调用 guard.maintain()。

    真实运行时这一节拍由保护引擎的主循环提供；这里必须真的跑起来，
    因为 `swallow_deadman_ms` 死手开关依赖它持续刷新。
    """

    def __init__(self, guard, interval: float = 0.05):
        self.guard = guard
        self.interval = interval
        self.ticks = 0
        self.errors = []
        self._stop = threading.Event()
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._run, name="guard-pump", daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.guard.maintain()
                self.ticks += 1
            except Exception as e:
                self.errors.append(f"{type(e).__name__}: {e}")
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1.5)
            self._t = None


def find_button(hwnd):
    return user32.FindWindowExW(wt.HWND(int(hwnd)), None, "Button", None)


def make_pause_cb(hwnd, outcome: DurationState):
    """构造「确保已暂停」回调。

    `PAUSED` 时**真的**去点靶机的暂停按钮（走真实链路，状态真的会变），
    其余情况什么都不做——用于验证「暂停失败绝不放行」。
    """
    calls = []

    def cb():
        calls.append(time.time())
        if outcome is not DurationState.PAUSED:
            return outcome
        btn = find_button(hwnd)
        if btn:
            user32.SendMessageW(wt.HWND(int(btn)), BM_CLICK, 0, 0)
            time.sleep(0.2)
        return DurationState.PAUSED

    cb.calls = calls
    return cb


def idx(names, name):
    try:
        return names.index(name)
    except ValueError:
        return -1


def delta(before: dict, after: dict, key: str) -> int:
    """统计增量。

    用增量而不是绝对值：靶机刚创建/刚置前的那一刻，钩子就可能已经看到一次
    光标事件并触发预暂停（这正是**正确**行为）。用例只应断言「我这一步
    操作带来的变化」，不该被前置活动的残留计数干扰。
    """
    return int(after.get(key, 0)) - int(before.get(key, 0))


def snap(guard) -> dict:
    return dict(guard.stats)


def start_guard(hwnd, pause_outcome=DurationState.PAUSED, prepause=False,
                replay_marker=0x4C474452, swallow=True, confirm=None):
    # 先把光标挪到无关角落：上一个用例常把光标停在 ✕ 上，而新靶机一创建就
    # 落在同一个位置，会造成「一进场就已悬停」的假象（实测的用例间污染）。
    H.park_cursor()
    cp = {
        "enabled": True, "swallow_close_click": bool(swallow),
        "prepause_on_hover": bool(prepause),
        "swallow_deadman_ms": 2500, "replay_marker": replay_marker,
        "block_when_unknown": True, "block_when_pause_failed": True,
        # 默认关掉层级3：输入层用例不该被另一条通路的额外开销干扰
        # （整窗 OCR 单次几百毫秒，会拖慢 Pump 节拍）。层级3 有自己的专门用例。
        "confirm_dialog": {"enabled": False} if confirm is None else confirm,
    }
    cfg = H.test_config(close_protection=cp)
    sink = _Sink()
    guard = CloseIntentGuard(cfg, logger=None, sink=sink,
                             on_blocked=sink.notify_blocked)
    guard.bind(hwnd)
    started = guard.start()
    cb = make_pause_cb(hwnd, pause_outcome)
    guard.set_pause_callback(cb)
    pump = Pump(guard)
    pump.start()
    time.sleep(0.5)                       # 让 maintain() 先武装死手开关
    return guard, pump, sink, cb, started


class _Sink:
    def __init__(self):
        self.events = []

    def emit(self, kind, **kw):
        self.events.append(kind)

    def notify_blocked(self, state):
        self.events.append(f"notify_blocked:{state}")


def teardown(guard, pump, proc):
    try:
        pump.stop()
    except Exception:
        pass
    try:
        guard.stop()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    time.sleep(0.3)


# --------------------------------------------------------------------- 用例
def case_structure(rep) -> bool:
    """第 0 节：先证明靶机**结构上**与真机等价，否则后面的结论没有意义。"""
    rep.section("0. 靶机结构等价性（前置条件：不通过则后续用例全部无意义）")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    try:
        r = wt.RECT()
        c = wt.RECT()
        user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r))
        user32.GetClientRect(wt.HWND(int(hwnd)), ctypes.byref(c))
        style = user32.GetWindowLongW(wt.HWND(int(hwnd)), -16) & 0xFFFFFFFF
        rep.check("样式与真机一致（0x14C20000）", style == 0x14C20000,
                  f"实际 0x{style:08X}")
        rep.check("无 WS_SYSMENU", not (style & 0x00080000), f"style=0x{style:08X}")
        rep.check("零非客户区（GetWindowRect == GetClientRect）",
                  (r.right - r.left) == c.right and (r.bottom - r.top) == c.bottom,
                  f"窗口 {r.right - r.left}x{r.bottom - r.top} / 客户区 {c.right}x{c.bottom}")

        x, y = H.chrome_close_point(hwnd)
        ht = cf.hit_test(hwnd, x, y)
        rep.check("✕ 处 WM_NCHITTEST 返回 HTCLIENT(1)（真机特征：系统不知道有 ✕）",
                  ht == 1, f"实际 {ht}（20=HTCLOSE）")

        names0 = H.chrome_event_names()
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd)
        rep.check("合成点击前提成立（靶机在前台且点位落在靶机上）", hit_ok, hit_diag)
        time.sleep(0.8)
        names = H.chrome_event_names()
        rep.check("点 ✕ 会到达应用（chrome_close_clicked）",
                  "chrome_close_clicked" in names, f"{str(names[:6])}｜{hit_diag}")
        rep.check("点 ✕ **不关窗**（真机特征）",
                  bool(user32.IsWindow(wt.HWND(int(hwnd)))), "窗口仍然存活")
        dlg = H.find_chrome_dialog()
        rep.check("弹出了应用内确认框", bool(dlg),
                  f"HWND={hex(dlg) if dlg else None}")
        rep.check("确认框里同时有「最小化到托盘」与「真的退出」",
                  bool(dlg) and bool(user32.FindWindowExW(wt.HWND(int(dlg)), None, "Button", "真的退出"))
                  and bool(user32.FindWindowExW(wt.HWND(int(dlg)), None, "Button", "最小化到托盘")))
        if dlg:
            exit_btn = user32.FindWindowExW(wt.HWND(int(dlg)), None, "Button", "真的退出")
            user32.SendMessageW(wt.HWND(int(exit_btn)), BM_CLICK, 0, 0)
            time.sleep(0.6)
        rep.check("选「真的退出」才真的关闭窗口",
                  not user32.IsWindow(wt.HWND(int(hwnd))))
        rep.info(f"启动后事件 = {names0}")
    finally:
        proc.terminate()
        time.sleep(0.3)
    return True


def case_swallow_release(rep) -> bool:
    """用例 A（主路径）：吞掉 ✕ 点击 → 先暂停 → 重放 → 应用自己弹确认框。"""
    rep.section("A. 主路径：吞点 → 确保暂停 → 重放放行")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(hwnd, DurationState.PAUSED)
        rep.check("低层鼠标钩子安装成功", started and guard.watcher.installed,
                  guard.watcher.error or f"installed={guard.watcher.installed}")
        rep.info(f"本进程提权={is_elevated()}（靶机非提权，故本环境足以验证机制）")
        rep.check("靶机初始为 RUNNING", H.chrome_state() == "RUNNING", H.chrome_state())

        guard.set_state(DurationState.RUNNING)
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd)   # ← 真实鼠标点击
        rep.check("合成点击前提成立（靶机在前台且点位落在靶机上）", hit_ok, hit_diag)
        H.wait_until(lambda: guard.stats["replayed"] > 0, timeout=8.0)
        time.sleep(0.6)
        names = H.chrome_event_names()

        rep.check("钩子确实吞掉了这次点击（swallowed ≥ 1）",
                  guard.stats["swallowed"] >= 1,
                  f"消费端={guard.stats}｜钩子侧={guard.watcher.status()}｜{hit_diag}")
        rep.check("暂停流程被触发", len(cb.calls) >= 1, f"调用 {len(cb.calls)} 次")
        rep.check("暂停真的生效（靶机状态变为 PAUSED）",
                  H.chrome_state() == "PAUSED", H.chrome_state())
        rep.check("点击被重放并送达应用（chrome_close_clicked 出现）",
                  "chrome_close_clicked" in names, f"{names}｜{hit_diag}")
        rep.check("重放计数 ≥ 1", guard.stats["replayed"] >= 1, f"stats={guard.stats}")

        i_pause = idx(names, "chrome_button_clicked")
        i_close = idx(names, "chrome_close_clicked")
        rep.check("【关键证据】暂停发生在 ✕ 点击送达之前",
                  i_pause >= 0 and i_close >= 0 and i_pause < i_close,
                  f"暂停@{i_pause} vs 点击@{i_close}｜事件={names}")
        rep.check("雷神自己的确认框出现了（说明重放是真的送到应用，不是伪造的）",
                  bool(H.find_chrome_dialog()) and "chrome_dialog_opened" in names)
        rep.check("未发生「不放行」（blocked == 0）",
                  guard.stats["blocked"] == 0, f"stats={guard.stats}")
        rep.check("泵线程无异常", not pump.errors, str(pump.errors[:2]))
    finally:
        teardown(guard, pump, proc)
    return True


def case_already_paused(rep) -> bool:
    """用例 B：当前已是 PAUSED → 完全不干预，点击原样送达。"""
    rep.section("B. 已暂停：不吞点、不重放、点击原样送达")
    proc, hwnd = H.start_mock_chrome("PAUSED")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(hwnd, DurationState.PAUSED)
        guard.set_state(DurationState.PAUSED)
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd)
        rep.check("合成点击前提成立（靶机在前台且点位落在靶机上）", hit_ok, hit_diag)
        time.sleep(1.0)
        names = H.chrome_event_names()
        rep.check("没有吞点（swallowed == 0）",
                  guard.stats["swallowed"] == 0, f"stats={guard.stats}")
        rep.check("没有重放（replayed == 0）",
                  guard.stats["replayed"] == 0, f"stats={guard.stats}")
        rep.check("点击原样送达应用并弹出确认框",
                  "chrome_close_clicked" in names and bool(H.find_chrome_dialog()),
                  f"{names}｜{hit_diag}")
        rep.check("未触发暂停流程", len(cb.calls) == 0, f"调用 {len(cb.calls)} 次")
    finally:
        teardown(guard, pump, proc)
    return True


def case_pause_failed(rep) -> bool:
    """用例 C（Fail Safe）：暂停无法确认为 PAUSED → **绝不放行**。"""
    rep.section("C. Fail Safe：暂停未确认 → 阻止关闭，不重放")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(hwnd, DurationState.UNKNOWN)
        guard.set_state(DurationState.RUNNING)
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd)
        rep.check("合成点击前提成立（靶机在前台且点位落在靶机上）", hit_ok, hit_diag)
        H.wait_until(lambda: guard.stats["blocked"] > 0, timeout=8.0)
        time.sleep(0.6)
        names = H.chrome_event_names()
        rep.check("确实吞掉了点击", guard.stats["swallowed"] >= 1,
                  f"stats={guard.stats}｜{hit_diag}")
        rep.check("尝试过暂停", len(cb.calls) >= 1, f"调用 {len(cb.calls)} 次")
        rep.check("【关键】没有重放（replayed == 0）",
                  guard.stats["replayed"] == 0, f"stats={guard.stats}")
        rep.check("点击没有送达应用（无 chrome_close_clicked）",
                  "chrome_close_clicked" not in names, f"{names}｜{hit_diag}")
        rep.check("没有弹出确认框（窗口关不掉）", not H.find_chrome_dialog())
        rep.check("窗口仍然存活", bool(user32.IsWindow(wt.HWND(int(hwnd)))))
        rep.check("发出了阻止通知", any("notify_blocked" in str(e) for e in sink.events),
                  str(sink.events))
        rep.check("靶机状态仍为 RUNNING（没有假装暂停成功）",
                  H.chrome_state() == "RUNNING", H.chrome_state())
    finally:
        teardown(guard, pump, proc)
    return True


def case_hover_prepause(rep) -> bool:
    """用例 D（可选优化）：光标在 ✕ 旁**停留**后提前暂停，用户点下去时可零等待放行。"""
    rep.section("D. 悬停预暂停：光标在 ✕ 邻域停留后提前暂停（默认关闭，此处显式打开）")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(hwnd, DurationState.PAUSED,
                                                     prepause=True)
        r = wt.RECT()
        user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r))
        rect = (r.left, r.top, r.right, r.bottom)
        # 悬停区比 ✕ 热区宽 26px，但这一点**不在**热区内（否则就成吞点用例了）
        hx, hy = r.right - 55, r.top + 45
        from leigod import close_intent as ci
        rep.check("选点确实在悬停区但不在 ✕ 热区",
                  ci.in_hover_zone(rect, hx, hy) and not ci.in_hot_zone(rect, hx, hy),
                  f"hot={ci.in_hot_zone(rect, hx, hy)} hover={ci.in_hover_zone(rect, hx, hy)}")

        # 预暂停真的会停掉加速，因此判据要求「雷神在前台」——测试必须如实置前
        H.force_foreground(hwnd)
        H.park_cursor()                      # 清掉上一用例残留的光标位置
        guard.set_state(DurationState.RUNNING)
        before = snap(guard)

        # 只 SetCursorPos 不会产生低层钩子可见的移动事件，必须真的发一次移动。
        # 且钩子侧有「停留满 prepause_dwell_ms（默认 300ms）」门槛，故需要两次。
        H.move_mouse_to(hx, hy)
        time.sleep(0.45)
        H.move_mouse_to(hx + 2, hy)
        # 等**内部计数**，而不是等外部副作用：靶机状态在暂停回调里就被改了，
        # 那一刻 `_handle` 还停在 `_ensure_paused()` 内部、计数尚未写入。
        # 若那时去 dict(guard.stats) 拷贝，会读到「handled 有、prepause 还没有」
        # 的中间态——这正是本用例第一版假失败的根因。
        H.wait_until(lambda: delta(before, snap(guard), "prepause") >= 1, timeout=8.0)
        H.wait_until(lambda: H.chrome_state() == "PAUSED", timeout=3.0)
        time.sleep(0.4)
        after = snap(guard)

        rep.check("未点击就已完成暂停（靶机状态 PAUSED）",
                  H.chrome_state() == "PAUSED", H.chrome_state())
        rep.check("本用例触发了预暂停（Δprepause ≥ 1）",
                  delta(before, after, "prepause") >= 1,
                  f"before={before.get('prepause')} after={after.get('prepause')}")
        rep.check("预暂停成功并记录（Δprepause_ok ≥ 1）",
                  delta(before, after, "prepause_ok") >= 1, str(after))
        rep.check("全程没有吞点（Δswallowed == 0）",
                  delta(before, after, "swallowed") == 0, str(after))
        rep.check("没有把点击放行出去（Δreplayed == 0）",
                  delta(before, after, "replayed") == 0, str(after))
        rep.check("暂停流程被调用", len(cb.calls) >= 1, f"调用 {len(cb.calls)} 次")
        rep.check("泵线程无异常", not pump.errors, str(pump.errors[:2]))
        rep.info(f"before={before}")
        rep.info(f"after ={after}")
        rep.info(f"guard.events={[e['ev'] for e in guard.events]}")
        rep.info(f"watcher={guard.watcher.status()}")
    finally:
        teardown(guard, pump, proc)
    return True


def case_hover_not_foreground(rep) -> bool:
    """用例 D2（防误暂停）：雷神**不在前台**时，光标掠过其角落不得暂停。

    风险场景：用户在别的程序里操作，光标恰好掠过「雷神所在屏幕区域的右上角」。
    若判据只看矩形几何，此时会把加速**平白停掉**——用户什么都没做却被打断。
    """
    rep.section("D2. 防误暂停：雷神不在前台时，光标掠过不触发预暂停")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(hwnd, DurationState.PAUSED,
                                                     prepause=True)
        r = wt.RECT()
        user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r))
        hx, hy = r.right - 55, r.top + 45

        # 把靶机藏起来，并**真的**让另一个窗口成为前台。
        # 注意两个坑：① 仅仅「新建并显示」靶机就会让它成为前台窗口（实测）；
        # ② 单纯 SW_HIDE 不会把前台交给别人（实测前台仍是靶机）。
        user32.ShowWindow(wt.HWND(int(hwnd)), 0)     # SW_HIDE
        other = H.create_other_window()
        time.sleep(0.6)
        fg = user32.GetForegroundWindow()
        rep.check("前提成立：靶机不在前台", fg != hwnd,
                  f"前台=0x{fg:X}（别的窗口=0x{other:X}） 靶机=0x{hwnd:X}")

        guard.set_state(DurationState.RUNNING)
        before = snap(guard)
        for _ in range(3):
            H.move_mouse_to(hx, hy)
            time.sleep(0.45)
        time.sleep(0.6)
        after = snap(guard)

        rep.check("没有触发预暂停（Δprepause == 0）",
                  delta(before, after, "prepause") == 0, str(after))
        rep.check("暂停流程完全没有被调用", len(cb.calls) == 0, f"调用 {len(cb.calls)} 次")
        rep.check("靶机状态仍为 RUNNING（加速没被打断）",
                  H.chrome_state() == "RUNNING", H.chrome_state())
    finally:
        H.destroy_other_window()
        teardown(guard, pump, proc)
    return True


def case_not_hotzone(rep) -> bool:
    """用例 E：窗口中间点击**不能**被吞（§十三 防误吞）。"""
    rep.section("E. 防误吞：窗口中部点击不受影响")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(hwnd, DurationState.PAUSED)
        guard.set_state(DurationState.RUNNING)
        cx, cy = H.chrome_center_point(hwnd)
        H.force_foreground(hwnd)
        H.click_at(cx, cy, settle=0.4)
        time.sleep(0.8)
        rep.check("窗口中部点击没有被吞（swallowed == 0）",
                  guard.stats["swallowed"] == 0, f"stats={guard.stats}")
        rep.check("没有误触发暂停流程", len(cb.calls) == 0, f"调用 {len(cb.calls)} 次")
        rep.check("窗口仍然存活", bool(user32.IsWindow(wt.HWND(int(hwnd)))))
        rep.info(f"钩子侧原因统计：passed={guard.watcher.passed} last_reason="
                 f"{guard.watcher.last_reason!r}")
    finally:
        teardown(guard, pump, proc)
    return True


def case_deadman(rep) -> bool:
    """用例 F：消费端停摆 → 死手开关必须让钩子停止吞点（否则用户会被自己锁死）。"""
    rep.section("F. 死手开关：消费端停摆时自动停止吞点")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        cfg = H.test_config(close_protection={
            "enabled": True, "swallow_close_click": True,
            "prepause_on_hover": False, "swallow_deadman_ms": 600,
        })
        guard = CloseIntentGuard(cfg, logger=None, sink=_Sink())
        guard.bind(hwnd)
        started = guard.start()
        guard.set_state(DurationState.RUNNING)
        guard.maintain()                        # 武装一次死手开关
        time.sleep(0.9)                         # 故意不再 maintain，等它过期
        n0 = len(H.chrome_event_names())
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd)
        rep.check("合成点击前提成立（靶机在前台且点位落在靶机上）", hit_ok, hit_diag)
        time.sleep(0.8)
        names = H.chrome_event_names()[n0:]
        rep.check("钩子安装成功", started and guard.watcher.installed)
        rep.check("死手过期后不再吞点（swallowed == 0）",
                  guard.stats["swallowed"] == 0, f"stats={guard.stats}")
        rep.check("点击原样送达应用（不会把用户锁死）",
                  "chrome_close_clicked" in names, f"{names}｜{hit_diag}")
    finally:
        teardown(guard, pump, proc)
    return True


def case_engine_wiring(rep) -> bool:
    """用例 G：**保护引擎与输入层关闭保护的接线**。

    为什么必须有这一条：前面的用例都是直接构造 `CloseIntentGuard`。
    把新机制接进 `ProtectionEngine` 本身就会引入一类新风险——
    钩子没启动、窗口没绑定、死手没被主循环刷新、关掉保护后仍在吞点
    （用户会连雷神都关不掉）。这些只有走真实引擎才测得出来。
    """
    rep.section("G. 引擎接线：启动 / 绑定 / 主循环维持 / 关闭保护时释放")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    engine = None
    try:
        cfg = H.test_config(
            leigod={"window_class_candidates": [H.MOCK_CHROME_CLS],
                    "window_title_keywords": ["雷神"]},
            detection={"ocr_enabled": False, "capture": "printwindow"},
            ui={"refresh_ms": 150},
            close_protection={"enabled": True, "method": "input_swallow",
                              "swallow_close_click": True,
                              "prepause_on_hover": False,
                              "swallow_deadman_ms": 2500})
        engine, sink = H.make_engine(cfg)
        engine.start()
        H.wait_until(lambda: engine.guard.hwnd == hwnd, timeout=8.0)
        time.sleep(0.6)

        rep.check("引擎已启动", engine._running)
        rep.check("输入层低层钩子已安装", engine.guard.watcher.installed,
                  engine.guard.watcher.error or "")
        rep.check("已绑定到雷神主窗口", engine.guard.hwnd == hwnd,
                  f"guard.hwnd={engine.guard.hwnd} 期望={hwnd}")
        rep.check("主循环确实在维持它（死手开关被刷新）",
                  engine.guard.watcher._swallow_deadline > time.time(),
                  f"deadline 剩余 {engine.guard.watcher._swallow_deadline - time.time():.2f}s")
        st = sink.last_status() or {}
        il = st.get("input_layer") or {}
        rep.check("状态如实暴露输入层信息（含提权与钩子状态）",
                  il.get("hook_installed") is True and "elevated" in il, str(il))
        rep.check("未提权时**如实上报**（不假装可用）", il.get("elevated") is False, str(il))

        # 「吞点为什么忽然不吞了」必须能从状态里看出来：死手窗口与实测 tick
        # 节拍是唯一线索。如果窗口被顶穿，✕ 会在用户不知不觉间恢复可用。
        rep.check("状态暴露死手窗口与实测 tick 节拍",
                  {"deadman_ms", "arm_ms", "tick_ms"} <= set(il.keys()),
                  str({k: il.get(k) for k in ("deadman_ms", "arm_ms", "tick_ms")}))
        rep.check("实测 tick 节拍被真的测到了（非 0）", float(il.get("tick_ms") or 0) > 0,
                  f"tick_ms={il.get('tick_ms')}")
        rep.check("武装窗口不小于死手下限", float(il.get("arm_ms") or 0) >= 2500.0,
                  f"arm_ms={il.get('arm_ms')} deadman_ms={il.get('deadman_ms')}")

        # 关闭意图的消费情况：`expired` 持续增长 = 「点了 ✕ 没反应」这一头号故障。
        ci = st.get("close_intent") or {}
        rep.check("状态暴露关闭意图消费情况",
                  {"accepted", "expired", "missed", "max_age_ms"} <= set(ci.keys()), str(ci))
        rep.check("新鲜度上限已按实测间隔自适应（≥ 基准 1500ms）",
                  float(ci.get("max_age_ms") or 0) >= 1500.0, f"max_age_ms={ci.get('max_age_ms')}")
        rep.check("尚无过期意图（引擎刚启动）", int(ci.get("expired") or 0) == 0, str(ci))

        # 层级3 的 OCR 扫描线程状态：线程若死了，`poll()` 永远取不到东西。
        cl = st.get("confirm_layer") or {}
        rep.check("状态暴露层级3 的 OCR 扫描线程状态",
                  "ocr_thread" in cl and "pending_hits" in cl, str(cl))

        # 关掉保护 → 必须立刻停止吞点，把 ✕ 还给用户
        engine.set_protection(False)
        time.sleep(0.4)
        rep.check("关闭保护后输入层同步停止", engine.guard.enabled is False,
                  f"enabled={engine.guard.enabled}")
        n0 = len(H.chrome_event_names())
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd)
        rep.check("合成点击前提成立（靶机在前台且点位落在靶机上）", hit_ok, hit_diag)
        time.sleep(0.8)
        rep.check("关闭保护后 ✕ 正常工作（点击送达应用）",
                  "chrome_close_clicked" in H.chrome_event_names()[n0:],
                  f"{H.chrome_event_names()[n0:]}｜{hit_diag}")

        engine.set_protection(True)
        time.sleep(0.5)
        rep.check("重新开启保护后输入层恢复", engine.guard.enabled is True,
                  f"enabled={engine.guard.enabled}")

        engine.stop()
        time.sleep(0.4)
        rep.check("引擎停止后钩子已卸载（不残留全局钩子）",
                  not engine.guard.watcher.installed)
    finally:
        try:
            if engine is not None and engine._running:
                engine.stop()
        except Exception:
            pass
        proc.terminate()
        time.sleep(0.3)
    return True


def case_confirm_bypass(rep) -> bool:
    """用例 H（层级3）：**输入层被绕过**时，确认框监测仍能保住时长。

    这是真机上真实存在的场景：用户用触摸屏点 ✕、通过远程桌面操作、
    或从托盘右键菜单退出——这些路径都不产生本机低层鼠标钩子可见的点击，
    层级2 完全看不到，用户却照样能走到确认框并选「真的退出」。

    这里用「输入层配置关闭」来模拟被绕过：靶机 RUNNING → 真人式点 ✕
    → 点击直接送达应用（不吞）→ 应用弹确认框 → 层级3 应当发现它并**自动暂停**。
    关键断言是**靶机自己的状态变成 PAUSED**，而不是被测对象自述「我暂停了」。
    """
    rep.section("H. 层级3：输入层被绕过时，确认框出现即自动暂停")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(
            hwnd, DurationState.PAUSED, swallow=False,
            confirm={"enabled": True, "mode": "winevent", "pause_on_dialog": True})
        rep.check("输入层已按配置关闭（模拟被绕过）", guard.enabled is False,
                  f"enabled={guard.enabled}")
        rep.check("确认框监测已启用且钩子装上",
                  guard.confirm is not None and guard.confirm.enabled
                  and guard.confirm.status()["hook_installed"],
                  str(guard.confirm.status() if guard.confirm else None))
        rep.check("保护总开关仍为开（层级3 不依赖输入层）", guard.protect_on is True)
        guard.set_state(DurationState.RUNNING)
        rep.check("靶机初始为 RUNNING", H.chrome_state() == "RUNNING", H.chrome_state())

        before = snap(guard)
        n0 = len(H.chrome_event_names())
        # 输入层已关，只看应用自己的反应 → 不强制前台，只要求点位落在靶机上
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd, need_foreground=False)
        rep.check("合成点击前提成立（点位落在靶机上）", hit_ok, hit_diag)
        time.sleep(0.6)
        names = H.chrome_event_names()[n0:]
        rep.check("点击直达应用（说明输入层确实没介入）",
                  "chrome_close_clicked" in names, f"{names}｜{hit_diag}")
        dlg = H.wait_until(H.find_chrome_dialog, timeout=3.0)
        rep.check("应用弹出了自己的确认框", bool(dlg), f"HWND={hex(dlg) if dlg else None}")

        # 回归守卫（实测踩过的坑）：GetParent 对 WS_POPUP 窗口返回的是 **owner**，
        # 不是父窗口。若拿 `GetParent != 0` 当「是否顶层」，真确认框会被全部误杀，
        # 表现就是「钩子装上了、窗口也弹了，但 confirm_seen 永远是 0」。
        if dlg:
            from leigod import confirm_dialog as cd
            info = cd.window_info(dlg)
            rep.check("确认框是 WS_POPUP：GetParent 返回 owner（≠0）",
                      int(user32.GetParent(wt.HWND(int(dlg))) or 0) != 0,
                      f"GetParent={hex(int(user32.GetParent(wt.HWND(int(dlg))) or 0))}")
            rep.check("但 window_info 判定它是顶层（GA_PARENT == 桌面）", info["top_level"],
                      f"top_level={info['top_level']} parent={hex(info['parent'])}")
            rep.check("因此 is_candidate_window 不会误杀它",
                      cd.is_candidate_window(info, hwnd, info["pid"]),
                      f"class={info['class']} size={info['size']}")

        ok = H.wait_until(lambda: delta(before, snap(guard), "confirm_seen") >= 1,
                          timeout=8.0)
        rep.check("层级3 发现确认框（confirm_seen 增加）", ok,
                  f"confirm_seen={snap(guard).get('confirm_seen')}")
        ok2 = H.wait_until(lambda: delta(before, snap(guard), "confirm_paused") >= 1,
                           timeout=12.0)
        rep.check("层级3 自动确保暂停（confirm_paused 增加）", ok2,
                  f"confirm_paused={snap(guard).get('confirm_paused')}")

        # 最关键的证据：靶机**自己**的状态变了，而不是我们的计数变了
        ok3 = H.wait_until(lambda: H.chrome_state() == "PAUSED", timeout=6.0)
        rep.check("★ 靶机状态真的变成 PAUSED（时长不会再被消耗）", ok3,
                  f"chrome_state={H.chrome_state()}")
        vias = [e.get("via") for e in guard.events if e.get("ev") == "confirm_dialog"]
        rep.info(f"确认框由哪条通路发现 = {vias}（靶机确认框是独立顶层窗口 → 应为 winevent）")

        # 用户随后选「真的退出」——此时已 PAUSED，关掉也不损失时长
        if dlg:
            H.click_chrome_dialog(dlg, "exit")
            time.sleep(0.6)
        rep.check("选「真的退出」后窗口关闭（暂停已完成，关掉无损失）",
                  not user32.IsWindow(wt.HWND(int(hwnd))))
        rep.check("worker 无未捕获异常", not guard.errors, str(guard.errors[-2:]))
    finally:
        teardown(guard, pump, proc)
    return True


def case_confirm_no_extra_pause(rep) -> bool:
    """用例 I（层级3）：输入层正常工作时，层级3 **不重复**发起暂停。

    输入层走完「吞点→暂停→重放」后确认框才出现，此时状态已是 PAUSED。
    层级3 必须能识别这一点并放手——否则每弹一次确认框就多跑一轮暂停流程，
    既浪费又会干扰用户（例如光标被挪动、误点）。
    """
    rep.section("I. 层级3：已 PAUSED 时不重复暂停（两层不打架）")
    proc, hwnd = H.start_mock_chrome("RUNNING")
    guard = pump = None
    try:
        guard, pump, sink, cb, started = start_guard(
            hwnd, DurationState.PAUSED,
            confirm={"enabled": True, "mode": "winevent", "pause_on_dialog": True})
        guard.set_state(DurationState.RUNNING)
        before = snap(guard)
        hit_ok, hit_diag = H.click_chrome_close_checked(hwnd)
        rep.check("合成点击前提成立（靶机在前台且点位落在靶机上）", hit_ok, hit_diag)
        ok = H.wait_until(lambda: delta(before, snap(guard), "replayed") >= 1, timeout=8.0)
        rep.check("输入层完成「吞点→暂停→重放」", ok, f"{snap(guard)}｜{hit_diag}")
        dlg = H.wait_until(H.find_chrome_dialog, timeout=3.0)
        rep.check("确认框出现（层级2 的重放送达）", bool(dlg))
        # 层级3 应当看到确认框（记录在案），但**不**额外暂停
        H.wait_until(lambda: delta(before, snap(guard), "confirm_seen") >= 1, timeout=8.0)
        time.sleep(1.0)
        rep.check("层级3 看到确认框并记录（confirm_seen≥1）",
                  delta(before, snap(guard), "confirm_seen") >= 1,
                  f"confirm_seen={snap(guard).get('confirm_seen')}")
        rep.check("层级3 未重复发起暂停（confirm_paused 保持 0）",
                  delta(before, snap(guard), "confirm_paused") == 0,
                  f"confirm_paused={snap(guard).get('confirm_paused')}")
        rep.check("暂停回调只被调用了一次", len(cb.calls) == 1, f"calls={len(cb.calls)}")
        if dlg:
            H.click_chrome_dialog(dlg, "tray")
            time.sleep(0.5)
    finally:
        teardown(guard, pump, proc)
    return True


def main() -> int:
    rep = H.Report("关闭保护 v2 闭环验证（结构仿真靶机 v2：自绘 ✕ + 应用内确认框）")
    H.kill_stale_mocks()
    try:
        case_structure(rep)
        case_swallow_release(rep)
        case_already_paused(rep)
        case_pause_failed(rep)
        case_hover_prepause(rep)
        case_hover_not_foreground(rep)
        case_not_hotzone(rep)
        case_deadman(rep)
        case_engine_wiring(rep)
        case_confirm_bypass(rep)
        case_confirm_no_extra_pause(rep)
    finally:
        H.kill_stale_mocks()
    path = rep.save("close_guard_report.txt")
    fails = sum(1 for l in rep.lines if "[FAIL]" in l)
    passes = len([l for l in rep.lines if "[PASS]" in l])
    # `结果：` 前缀是 `run_all.py` 汇总表提取摘要的约定（其余 7 套都这么打）。
    # 这里少一个前缀，汇总表就会缺掉本套的尾注，看起来像是没出报告。
    print(f"\n关闭保护 v2：{passes} PASS / {fails} FAIL")
    print(f"结果：{'全部通过' if not fails else f'有 {fails} 项失败'}；报告 {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
