"""保护引擎：把窗口/识别/暂停/关闭保护/游戏监控串成一条闭环。

刻意**不依赖 Qt**：UI 通过一个 sink（回调集合）接事件，测试用记录型 sink。
这样规格书 §三十三 要求的「不许假装成功」才能被自动化验证。

主循环节拍：
  每 tick（默认 500ms，config ui.refresh_ms）
    定位/刷新雷神窗口 → 按 poll_ms 节奏识别状态 → 关闭保护策略落地
    → 消费关闭意图 → 游戏退出延迟保护 → 上报状态
"""
from __future__ import annotations

import threading
import time

import psutil

from core.events import (ACTION_ALLOW_CLOSE, ACTION_OK, ACTION_PAUSE_NOW, Event, EventKind,
                         LEVEL_ERROR, LEVEL_INFO, LEVEL_WARN, Notice)
from core.state_machine import (BLOCK_MESSAGES, CloseDecision, DurationState, Reading, StateMachine)
from detection import coordinate_fallback as cf
from game.process_monitor import GameMonitor
from leigod import window as win_mod
from leigod.close_protection import CloseIntentGuard, CloseProtection
from leigod.duration_controller import DurationController, PauseOutcome
from leigod.duration_detector import DurationDetector


class Sink:
    """引擎对外的全部输出。实现方只需实现关心的几个方法。"""

    def on_event(self, event: Event): pass
    def on_status(self, status: dict): pass
    def on_notice(self, notice: Notice): pass
    def on_pause(self, outcome: PauseOutcome): pass


class NullSink(Sink):
    pass


class RecordingSink(Sink):
    """测试用：把一切记下来以便断言。"""

    def __init__(self):
        self.events = []
        self.statuses = []
        self.notices = []
        self.pauses = []

    def on_event(self, event):
        self.events.append(event)

    def on_status(self, status):
        self.statuses.append(status)

    def on_notice(self, notice):
        self.notices.append(notice)

    def on_pause(self, outcome):
        self.pauses.append(outcome)

    # 便捷查询
    def has(self, kind) -> bool:
        return any(e.kind is kind for e in self.events)

    def kinds(self) -> list:
        return [e.kind for e in self.events]

    def count(self, kind) -> int:
        return sum(1 for e in self.events if e.kind is kind)

    def last_status(self) -> dict:
        return self.statuses[-1] if self.statuses else {}


class ProtectionEngine:
    def __init__(self, config, sink: Sink = None, logger=None):
        # 必须在任何坐标 API 之前声明 DPI 感知：本机是 125% 缩放，
        # 不声明的话读到的是逻辑坐标（实测 1080x700 的窗口被读成 864x560），
        # 按下 ✕ 的判断与坐标点击都会整体偏移。
        cf.set_dpi_aware()
        self.config = config
        self.sink = sink or NullSink()
        self.log = logger
        self.sm = StateMachine()
        self.detector = DurationDetector(config, logger)
        self.controller = DurationController(config, self.detector, logger)
        self.close = CloseProtection(config, logger)
        # 关闭保护 v2 主路径（输入层）：吞掉 ✕ 点击 → 确保暂停 → 重放放行。
        # 真机实测，雷神的 ✕ 是应用自绘的、只弹应用内确认框、不走 SC_CLOSE，
        # 所以「禁用系统菜单」那条路对它是无效的（见 docs/关闭保护-设计方案v2.md）。
        self.guard = CloseIntentGuard(config, logger, sink=self.sink,
                                      on_blocked=self._on_close_blocked)
        self.guard.set_pause_callback(self._guard_ensure_paused)
        self.games = GameMonitor(config, logger)

        self.win = None
        self._thread = None
        self._running = False
        self._protect_enabled = True

        # 窗口绑定方式（MATCH_*）与"等待严格匹配"的起点。
        # 弱匹配不是一次性决定：`_maybe_rebind_weak` 会持续复核并改绑，
        # 这样"绑错窗口 → 状态永远读不出来 → 只能重启程序"就不再是终局。
        self._win_match = "unknown"
        #: 最近一次**确认**为 PAUSED 的时刻（0 = 尚未确认，或已被 RUNNING 作废）。
        #: 用途见 `_settle_lost`：窗口消失瞬间的读失败不该被当成状态变化。
        self._paused_confirmed_ts = 0.0
        self._strict_wait_since = None
        self._last_rebind_check = 0.0

        # 请求标志
        self._wake = threading.Event()
        self._want_check = False
        self._want_pause = False
        self._want_recheck = False
        self._quit_requested = threading.Event()

        # 计时与去抖
        self._last_state_poll = 0.0
        self._last_scan = 0.0
        self._last_auto_pause = 0.0
        self._last_status = {}
        self._pause_failed = False
        self._pause_fails = 0
        self._allowed_close = False
        self._gone_since = None
        self._guard_seen_seq = 0        # 已消费到的事件序号（见 _consume_guard_events）
        self._hidden_warned = 0.0
        self._lost_pending = None
        self._started_ts = 0.0

    # ================================================================ 生命周期
    def _use_input_layer(self) -> bool:
        """配置是否要求走输入层拦截（关闭保护 v2 主路径）。"""
        return str(self.config.get("close_protection.method", "input_swallow")) in (
            "input_swallow", "both")

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_ts = time.time()
        # ⚠️ 启动宽限**必须在线程起来之前**设好。
        # 踩过：原先写在后面（guard.start() 之后），而 tick 线程在它之前就起来了 ——
        # 线程可能抢先 maintain() 一次，用 2.5s 的默认窗口把钩子武装上，
        # 随后首轮那个 10s 的 tick 立刻把窗口顶穿，`[degraded]` 照样出现
        # （真机 2026-09-30 20:46:49 实测复现）。
        # 首轮 tick 要等 Chromium 建好无障碍树 + 构造 RapidOCR 引擎（10s 量级），
        # 而死手窗口默认只有 2.5s —— 正好落在"刚启动、用户最可能点 ✕"的时段。
        # 宽限是**显式申请、有时限**的（不做成全局初值：那会让消费端真死时也吞 10s，
        # 用户被锁死，见 leigod/close_protection.set_startup_grace 的说明）。
        try:
            self.guard.set_startup_grace(float(self.config.get(
                "close_protection.startup_grace_seconds", 15)))
        except Exception as e:
            if self.log:
                self.log.warning("设置启动宽限失败：%s", e)
        # 把**实际生效**的识别节拍写进日志。为什么值得一行日志：
        # 「调快了没有」这件事过去只能靠感觉，而节拍同时受 tick(`ui.refresh_ms`)
        # 与自适应倍率影响 —— 只改一个配置项时，很容易出现"改了却没生效"。
        # 写出来之后，看一眼日志就能确认，不必再翻代码推算。
        try:
            base = max(0.5, int(self.config.get("duration.poll_ms", 500)) / 1000.0)
            tick_ms = int(self.config.get("ui.refresh_ms", 500))
            cad = " / ".join(
                f"{k} {base * f:.1f}s"
                for k, f in (("RUNNING", self.POLL_FACTOR["RUNNING"]),
                             ("UNKNOWN", self.POLL_FACTOR["UNKNOWN"]),
                             ("PAUSED", self.POLL_FACTOR["PAUSED"])))
            flag = "" if self.config.get("duration.adaptive_poll", True) else "（自适应已关）"
            self._log(LEVEL_INFO, EventKind.STARTED,
                      f"状态识别节拍：{cad}{flag}｜tick={tick_ms}ms"
                      f"｜全量枚举由缓存时限门控（每 "
                      f"{self.config.get('ui_automation.cache_ttl', 5)}s 量级一次）")
        except Exception as e:                                    # noqa: BLE001
            if self.log:
                self.log.warning("输出识别节拍失败：%s", e)
        self._thread = threading.Thread(target=self._loop, name="protection", daemon=True)
        self._thread.start()
        started = self.close.start_watcher()
        ui_started = self.guard.start() if self._use_input_layer() else False
        detail = (f"关闭保护：输入层拦截={'已启用' if ui_started else '未启用'}"
                  f"；系统菜单兜底={'钩子就绪' if started else '不可用'}"
                  f"；提权={'是' if self.guard.status()['elevated'] else '否'}")
        self._log(LEVEL_INFO, EventKind.STARTED, f"保护引擎启动（{detail}）")
        # OCR 自检放到后台线程：它要构造 RapidOCR 引擎（1~2 秒 + 几十 MB 常驻内存），
        # 卡在这里会白白推迟**第一次状态识别**。放后台既不阻塞首读，又不省掉这句话
        # （失败时照样会写 WARNING）。
        threading.Thread(target=self._check_ocr_engine, name="ocr-selfcheck",
                         daemon=True).start()
        if self._use_input_layer() and not self.guard.status()["elevated"]:
            # 能力不足必须**明说**，不能静默假装可用（§三十三 / §三十四）
            self._log(LEVEL_ERROR, EventKind.NOTICE,
                      "本程序未提权：雷神恒以管理员运行，输入层关闭保护会被系统拒绝。"
                      "请以管理员身份重新运行本程序。")

    def stop(self, timeout: float = 3.0) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.guard.stop()             # 先卸钩子：绝不能让「吞点」在无人处理时残留
        self.close.stop_watcher()
        self.close.release()          # 退出前必须把 ✕ 还给用户，别把雷神锁死
        self._log(LEVEL_INFO, EventKind.STOPPED, "保护引擎已停止")

    @property
    def quit_requested(self) -> bool:
        return self._quit_requested.is_set()

    # ================================================================ 外部请求
    def request_check(self) -> None:
        self._want_check = True
        self._wake.set()

    def request_pause(self) -> None:
        self._want_pause = True
        self._wake.set()

    def request_recheck(self) -> None:
        """用户点「重新检测状态」：重定位窗口 + 强制全量识别 + 如实汇报。

        为什么要给用户这个按钮：状态读不出来有好几种成因（窗口绑错、被别的
        窗口挡住、雷神没加载完、正弹着它自己的确认框），而程序**不能替用户
        猜**哪种。给一个"你说不清就按一下"的入口，比让用户重启程序友好得多，
        也比分不清成因就乱猜要诚实。
        """
        self._want_recheck = True
        self._wake.set()

    def _consume_recheck(self) -> None:
        if not self._want_recheck:
            return
        self._want_recheck = False

        # ① 先重新定位一次窗口：如果之前绑错了（弱匹配），这里会顺带纠正。
        self._last_scan = 0.0
        try:
            self._ensure_window(time.time())
        except Exception as e:
            if self.log:
                self.log.exception("重新定位雷神窗口失败：%s", e)

        # ② 雷神不在前台时，**先把它切到前面**再读。
        #    这不是"猜"，而是创造可读条件：Chromium/Electron 的窗口一旦不在前台，
        #    它会把无障碍树收起来（真机实测 476 个控件 → 5 个），此时 UIA 必然读不到。
        #    用户按这个按钮就是在要求"给我一个确定答案"，切窗口是用户动作的合理后果；
        #    而**轮询路径绝不会这么做**（那会在用户玩游戏时反复抢焦点）。
        if self.win is not None:
            try:
                from detection import coordinate_fallback as cf
                if cf.user32.GetForegroundWindow() != self.win.hwnd:
                    cf.activate_window(self.win.hwnd)
                    time.sleep(0.25)      # 给渲染进程一点时间把无障碍树打开
            except Exception as e:
                if self.log:
                    self.log.warning("切换到雷神窗口失败（继续尝试读取）：%s", e)

        if self.win is None:
            self._log(LEVEL_WARN, EventKind.NOTICE, "重新检测：未找到雷神窗口")
            self._notice("重新检测：没找到雷神窗口",
                         "没有检测到雷神的主窗口。\n"
                         "· 雷神没启动 → 先把它打开\n"
                         "· 雷神缩在托盘里 → 从托盘图标把它打开\n"
                         "然后点「重新检测状态」再试一次。",
                         [("知道了", ACTION_OK)], strong=True)
            return

        # ② 强制**全量**识别：这一次不省 OCR（ocr_mode=always），
        #    把 UIA 与 OCR 两条证据都拿到手 —— 用户按这个按钮就是想要一个确定答案。
        try:
            reading = self.detector.detect(self.win, allow_ocr=True, ocr_mode="always")
        except Exception as e:
            self._log(LEVEL_ERROR, EventKind.NOTICE, f"重新检测识别失败：{e}")
            self._notice("重新检测失败", f"识别过程出错：{e}", [("知道了", ACTION_OK)],
                         strong=True)
            return
        self._note_reading(reading)

        st = reading.state
        win_kind = ("弱匹配（可能绑到了雷神的辅助窗口）"
                    if getattr(self.win, "weak_match", False) else "身份匹配")
        self._log(LEVEL_INFO, EventKind.STATE_CHANGED,
                  f"用户触发重新检测 → {st.value}（窗口绑定：{win_kind}；{reading.summary()}）")
        if st is DurationState.PAUSED:
            self._notice("重新检测：总时长已暂停",
                         "已确认处于暂停状态，可以安全关闭雷神。",
                         [("知道了", ACTION_OK)])
        elif st is DurationState.RUNNING:
            self._notice("重新检测：总时长正在消耗",
                         "关雷神之前先点「立即暂停时长」。",
                         [("立即暂停", ACTION_PAUSE_NOW), ("知道了", ACTION_OK)])
        else:
            self._notice("重新检测：仍然无法确认",
                         "读不出状态，常见原因：\n"
                         "· 雷神窗口被别的窗口挡住 / 不在最前面 → 点一下雷神窗口再试\n"
                         "· 雷神刚启动还没加载完 → 等两秒再试\n"
                         "· 雷神正弹着它自己的确认框 → 先把那个框处理掉\n"
                         f"（窗口绑定：{win_kind}）\n"
                         f"识别依据：{reading.summary()}",
                         [("知道了", ACTION_OK), ("立即暂停", ACTION_PAUSE_NOW)],
                         strong=True)

    def request_allow_close(self) -> None:
        self.close.allow_once()
        self.guard.allow_once()       # 输入层同样要让开，否则 ✕ 永远点不动
        if self.log:
            self.log.warning("用户显式允许关闭雷神（跳过保护）")
        self._wake.set()

    def set_protection(self, on: bool) -> None:
        self._protect_enabled = bool(on)
        if not on:
            self.close.release()
            self.guard.set_enabled(False)     # 立即停止吞点，把 ✕ 还给用户
        else:
            self.close.clear_escape()
            self.guard.clear_escape()
            self.guard.set_enabled(self._use_input_layer())
        self._want_check = True
        self._wake.set()
        if self.log:
            self.log.info("保护开关：%s", "开启" if on else "关闭")

    @property
    def protection_enabled(self) -> bool:
        return self._protect_enabled

    def exit_readiness(self) -> tuple:
        """退出本程序前，判断「现在退出去会不会把雷神留在计时状态」。

        为什么要过这一道关：本程序存在的唯一目的就是别让总时长白白消耗。
        如果它自己在雷神正在计时的时候悄悄退出，用户会以为"我已经收拾干净了"，
        而计时器其实还在跑——那恰恰是本程序要防的那件事。
        所以**退出自己的路径也必须走同一条纪律**：读状态 → 确认已暂停 → 才放行。

        `UNKNOWN` 在这里同样**不能**当成 `PAUSED`（规格书 §六）：
        「不知道」不等于「安全」，此时退出与「假装成功」没有区别。

        返回 `(是否可以安全退出, 人话说明)`。本方法只读内存状态，不发起任何点击。
        """
        if not self._protect_enabled:
            return True, "关闭保护已关闭，退出不做干预"
        if self.win is None:
            return True, "雷神未在运行"
        state = self.sm.state
        if state is DurationState.PAUSED:
            return True, "雷神总时长已暂停"
        if state is DurationState.RUNNING:
            return False, "雷神总时长正在计时"
        return False, "无法确认雷神是否仍在计时（UNKNOWN）"

    # ================================================================ 主循环
    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as e:       # 监控循环绝不能崩
                if self.log:
                    self.log.exception("保护循环异常: %s", e)
            self._wake.wait(timeout=0.1)
            self._wake.clear()

    def _tick(self) -> None:
        cfg_refresh = max(0.1, int(self.config.get("ui.refresh_ms", 500)) / 1000.0)
        now = time.time()

        # 必须**每轮**调用（含窗口不存在时）：
        # 它负责刷新快照与「死手开关」，钩子的吞点能力完全依赖它。
        # 注意 cfg_refresh 必须显著小于 close_protection.swallow_deadman_ms，
        # 否则死手会周期性过期、吞点时灵时不灵。
        self.guard.maintain()
        # 后台线程的结论（吞点/重放/发现确认框/自动暂停/死手降级）必须转成
        # 用户可见的通知，否则等于「做了但没说」（本项目的最高纪律：不许假装成功）。
        self._consume_guard_events()

        self._ensure_window(now)
        # 用户点了「重新检测状态」：立刻重定位窗口 + 强制全量识别 + 如实汇报。
        # 排在 `_poll_state` 之前，是因为它本身就要做一次识别，没必要先白跑一轮。
        self._consume_recheck()
        if self.win is None:
            self._emit_status()
        else:
            # ⚠️ 顺序是有讲究的：**关闭意图必须排在慢步骤前面**。
            # `pop_close_intents` 有 1.5s 的新鲜度上限（超过就当成过期事件丢弃），
            # 而 `_poll_state` 在本机做一次整窗 OCR 要 1.3s 量级。若把意图消费放在
            # 它后面，用户点下的那一下会在队列里白白等过一次 OCR 再被判「过期」——
            # 表现就是「点了 ✕ 保护毫无反应」，实测踩过。
            if self._consume_close_intents():
                # 刚刚产生了新决策（放行 / 阻止 / 暂停失败）→ 立刻上报一次。
                # 不能等这一轮 tick 末尾：`_poll_state` 的 OCR 还要 1.3s 以上，
                # 界面在这期间会一直显示上一个决策，用户看到的就不是刚发生的事。
                self._emit_status()
            self._poll_state(now)
            self._apply_close_policy(now)
            self._consume_pause_request()
            self._maybe_auto_pause_on_game_exit(now)
            self._emit_status()

        self._handle_follow_exit(now)
        # 把 tick 做成固定节拍
        elapsed = time.time() - now
        if elapsed < cfg_refresh:
            time.sleep(cfg_refresh - elapsed)

    def _check_ocr_engine(self) -> None:
        """启动时确认 OCR 引擎**真的能用**，不能用就当场说清楚。

        为什么值得单独一步（真机事故 2026-09-30）：打包版曾漏打 RapidOCR 的
        .onnx 模型，`import` 却成功，于是「OCR 永远认不出文字」这件事
        在整个日志里只表现为 `ocr=UNKNOWN(未识别到可用文字)` —— 和
        「这一帧确实没有文字」长得一模一样，根本分不出是故障还是正常。
        结果：UIA 一旦也读不到控件（雷神窗口不在前台时 Chromium 的
        无障碍树会塌缩），整个工具就彻底失明，而日志里看不出任何异常。
        """
        try:
            from detection import ocr as ocr_mod
        except Exception:
            return
        try:
            if not ocr_mod.engine_available():
                why = ocr_mod.last_error() or "未安装 rapidocr-onnxruntime"
                self._log(LEVEL_WARN, EventKind.NOTICE,
                          f"OCR 引擎不可用（{why}）—— UI Automation 一旦读不到控件，"
                          "时长状态将无法识别，界面会一直显示「无法确认」")
        except Exception as e:
            if self.log:
                self.log.warning("OCR 引擎自检异常：%s", e)

    # ------------------------------------------------------------ 窗口
    #: 弱匹配（没对上配置的窗口类/标题）最多先等多久才允许"先用着"（秒）。
    #: 为什么不能立刻接受：雷神启动途中主窗口还没建好，此时唯一的候选是
    #: Electron 的辅助顶层窗口；绑上它 = 状态永远读不出来（真机 13:06 实例）。
    STRICT_BIND_WAIT_S = 15.0
    #: 弱匹配绑定后，每隔多久复核一次是否有"认准的"窗口出现（秒）
    REBIND_CHECK_S = 2.0

    def _bind(self, found) -> None:
        """把引擎绑定到一个已定位的窗口上（初次绑定与改绑共用一套动作）。

        抽出来的原因：改绑必须和初次绑定做**完全相同**的复位动作
        （解/绑钩子、复位状态机、清放行标记……），少做一步就会留下
        一个"用旧窗口的状态管新窗口"的错位，比不绑定更难查。
        """
        self.win = found
        self._win_match = getattr(found, "match", "unknown")
        self._lost_pending = None
        self._strict_wait_since = None
        self._last_rebind_check = time.time()
        self.close.bind(found.hwnd)
        self.close.clear_escape()
        self.guard.bind(found.hwnd)
        self.guard.clear_escape()
        self.guard.set_enabled(self._use_input_layer() and self._protect_enabled)
        self._allowed_close = False
        self._pause_failed = False
        self._pause_fails = 0
        self._gone_since = None
        self.sm.reset()
        self._last_state_poll = 0.0
        self._log(LEVEL_INFO, EventKind.LEIGOD_FOUND,
                  f"已绑定雷神窗口：{found.title!r} class={found.class_name} "
                  f"HWND=0x{found.hwnd:X} PID={found.pid} v{found.version} "
                  f"DPI={found.dpi} 尺寸={found.size} match={self._win_match}")

    def _maybe_rebind_weak(self, now: float) -> None:
        """弱匹配绑定的自我修复：一旦出现"认准的"窗口就改绑过去。

        这是「工具忽然读不出状态、只能重启程序」的结构性解法：绑错窗口不再
        是终局，而是一个会被自动纠正的中间状态。改绑会留日志（可追溯），
        因为它是**改变保护对象**的动作，不能悄悄做（§三十三）。
        """
        if self._win_match not in ("fallback",):
            return
        if self.win is None:
            return
        if now - self._last_rebind_check < self.REBIND_CHECK_S:
            return
        self._last_rebind_check = now
        try:
            better = win_mod.find_main_window(self.config, strict=True)
        except Exception as e:
            if self.log:
                self.log.warning("改绑复核失败：%s", e)
            return
        if not better or better.hwnd == self.win.hwnd:
            return
        self._log(LEVEL_INFO, EventKind.LEIGOD_FOUND,
                  f"发现身份匹配的雷神主窗口，改绑：0x{better.hwnd:X} "
                  f"class={better.class_name} 尺寸={better.size}"
                  f"（原绑 0x{self.win.hwnd:X} class={self.win.class_name} "
                  f"尺寸={self.win.size}）")
        self._bind(better)

    def _ensure_window(self, now: float) -> None:
        if self.win is not None:
            if win_mod.is_valid(self.win.hwnd):
                self.win = win_mod.refresh(self.win)
                self._lost_pending = None
                self._maybe_rebind_weak(now)
                return
            # 窗口刚刚消失：先挂起，等进程状态落定再判定是「隐藏」还是「真退出」
            if self._lost_pending is None:
                self._lost_pending = {"ts": now, "state": self.sm.state, "pid": self.win.pid}
            if self.log:
                self.log.info("雷神窗口消失，等待判定（PID=%s）", self.win.pid)
            self.win = None
            self.close.bind(0)
            self.close.release()
            self.guard.bind(0)      # 解绑：钩子快照变为「未武装」，不再吞任何点击

        if self._lost_pending is not None:
            if not self._settle_lost(now):
                return

        # 没有窗口时也需要周期性重扫
        if now - self._last_scan < 1.0 and self._last_scan:
            return
        self._last_scan = now

        # ① 先按配置**严格匹配**。宁可多等一秒，也不要在雷神启动途中绑错窗口。
        strict_win = win_mod.find_main_window(self.config, strict=True)
        if strict_win is not None:
            self._bind(strict_win)
            return

        # ② 严格匹配不到：先确认"到底有没有窗口"，以区分
        #    「雷神没在跑」与「在跑、但主窗口还没建好」。
        weak_win = win_mod.find_main_window(self.config)
        if weak_win is None:
            self._strict_wait_since = None
            return
        if self._strict_wait_since is None:
            self._strict_wait_since = now
            if self.log:
                self.log.info("找到雷神进程，但窗口身份与配置不匹配"
                              "（class=%s 尺寸=%s，期望类名=%s）—— 先等真正的主窗口出现",
                              weak_win.class_name, weak_win.size,
                              self.config.get("leigod.window_class_candidates"))
            return
        wait_s = float(self.config.get("leigod.strict_bind_wait_seconds",
                                       self.STRICT_BIND_WAIT_S))
        if now - self._strict_wait_since < max(0.0, wait_s):
            return

        # ③ 等够了仍只有弱匹配 → 先用着（否则用户等于没有保护），但**必须如实告知**：
        #    这种绑定很可能是错的窗口，状态会读不出来，用户要知道该点哪里补救。
        self._log(LEVEL_WARN, EventKind.NOTICE,
                  f"等待 {(now - self._strict_wait_since):.0f}s 仍未等到与配置匹配的窗口，"
                  f"先按 class={weak_win.class_name} 尺寸={weak_win.size} 绑定（弱匹配）")
        self._notice("雷神窗口身份与预期不符",
                     "找到的窗口类名不在配置的候选里，可能绑到了雷神的辅助窗口 —— "
                     "此时时长状态多半读不出来。\n"
                     "若界面显示「无法确认」，请点面板上的「重新检测状态」重试；"
                     "仍不行就重启一次本程序。",
                     [("知道了", ACTION_OK)], strong=True)
        self._bind(weak_win)

    def _settle_lost(self, now: float) -> bool:
        """判定「窗口消失」属于哪种情形。返回是否已落定。

        用绑定时的 PID 判断进程是否还活着——用进程名模式匹配是不行的：
        模式可能同时命中别的进程（例如启动器 leigod_launcher.exe），
        更不能用「雷神是否在跑」这种模糊判断。
        """
        pend = self._lost_pending
        if not pend:
            return True
        pid = pend.get("pid") or 0
        try:
            alive_proc = bool(pid) and psutil.pid_exists(pid)
        except Exception:
            alive_proc = False
        if alive_proc and (now - pend["ts"]) < 3.0:
            return False        # 进程还没退干净，再等一会儿
        self._lost_pending = None
        last = pend["state"]
        # ⚠️ 窗口**消失过程中**的识别失败（UNKNOWN）不是状态变化，是「读不到了」。
        # 实测（2026-10-01，轮询压到 0.5s 后更易撞上）：用户关掉雷神的那一两秒里，
        # 某次轮询正好读到半死的窗口 → UNKNOWN → 这里就判成「退出前未能确认暂停」，
        # 弹出一条**虚假的计费告警**（"可能仍在计费"）。
        # 判据改成：消失前一段时间内**确认过** PAUSED，且此后没有确认过 RUNNING。
        if (last is not DurationState.PAUSED
                and self._paused_confirmed_ts
                and (pend["ts"] - self._paused_confirmed_ts) <= self.LOST_PAUSED_GRACE_S):
            if self.log:
                self.log.info("退出前最后一次确认状态是 PAUSED（%.1fs 前），按已暂停处理"
                              "（消失瞬间的 UNKNOWN 视为「读不到」）",
                              pend["ts"] - self._paused_confirmed_ts)
            last = DurationState.PAUSED

        if alive_proc:
            # 进程还在，只是窗口抓不到（最小化到托盘/被隐藏）
            if now - self._hidden_warned > 60:
                self._hidden_warned = now
                msg = (f"雷神窗口当前不可用（进程 {pid} 仍在运行）。"
                       f"最近状态={last.value}，无法验证时长是否仍在消耗。")
                self._log(LEVEL_WARN, EventKind.NOTICE, msg)
                self._notice("雷神窗口不可用", msg,
                             [("知道了", ACTION_OK), ("立即暂停", ACTION_PAUSE_NOW)])
            return True

        # 进程已经退出：这是一次真正的关闭
        if self._allowed_close:
            self._log(LEVEL_INFO, EventKind.CLOSE_ALLOWED, "保护成功：已确认暂停并放行关闭")
        elif last is DurationState.PAUSED:
            self._log(LEVEL_INFO, EventKind.LEIGOD_GONE, "雷神已退出（退出前时长已确认为暂停）")
        else:
            msg = ("雷神已退出，但退出前未能确认总时长已暂停，可能仍在计费。\n"
                   "建议重新打开雷神，确认「开启时长」状态。")
            self._log(LEVEL_WARN, EventKind.WINDOW_CLOSED_UNPROTECTED,
                      f"雷神关闭时未确认暂停（最后状态={last.value}）")
            self._notice("雷神已退出", msg, [("知道了", ACTION_OK)], strong=True)
        self.sm.reset()
        self._allowed_close = False
        self._pause_failed = False
        self._gone_since = now
        return True

    # ------------------------------------------------------------ 状态识别
    #: 自适应轮询的倍率：状态越"安全"，间隔越大。
    #:
    #: 依据是**风险不对称**：
    #:   RUNNING → 随时可能被消耗，且用户可能马上点 ✕，必须跟得紧（原速）；
    #:   UNKNOWN → 半瞎，但也做不了什么，稍慢一点没损失；
    #:   PAUSED  → 时长没在走。唯一能变成 RUNNING 的途径是用户自己在雷神里点了
    #:             「开启时长」，晚一点知道**毫无风险**。
    #:
    #: 倍率上限刻意压在 2.0：用户要求「0.5~1 秒确认一次状态」，
    #: 而 base=500ms 时 2.0 恰好是 1.0s —— 三态的有效节拍都落在 0.5~1.0s 里。
    #: 改倍率前先想清楚：它一旦超过 2.0，最慢那一档就出界了。
    POLL_FACTOR = {"RUNNING": 1.0, "UNKNOWN": 1.4, "PAUSED": 2.0}

    def _poll_interval(self) -> float:
        base = max(0.5, int(self.config.get("duration.poll_ms", 1000)) / 1000.0)
        if not self.config.get("duration.adaptive_poll", True):
            return base
        factor = self.POLL_FACTOR.get(self.sm.state.value, 1.0)
        return base * factor

    def _poll_state(self, now: float) -> None:
        poll = self._poll_interval()
        if not self._want_check and (now - self._last_state_poll) < poll:
            return
        self._want_check = False
        self._last_state_poll = now
        # `ocr_mode="auto"`：UIA 已经定论时跳过整窗 OCR（本机实测 1.3s+）。
        # 这是「识别延迟高」的主因 —— 名义 500ms 的 tick 被 OCR 拖到 1.3s+。
        ocr_mode = str(self.config.get("detection.ocr_mode", "auto") or "auto").lower()
        # `uia_fast=True`：走控件缓存（只重读 1~2 个控件的 Name）。全量枚举
        # 一棵 ~476 控件的 Electron 树要 0.3~2 秒，而这里每 1 秒就要跑一次 ——
        # 用全量扫描等于把一个核的算力长期吃满（用户反馈"后台占用高"）。
        # 缓存每 `CACHE_TTL`(3s) 自动过期一次，届时会做全量扫描纠偏。
        reading = self.detector.detect(self.win, allow_ocr=True, ocr_mode=ocr_mode,
                                      uia_fast=True)
        self._note_reading(reading)

    #: 「窗口消失」与「最后确认过的 PAUSED」之间允许多久（秒）。见 `_settle_lost`。
    LOST_PAUSED_GRACE_S = 8.0

    def _note_reading(self, reading: Reading) -> None:
        changed = self.sm.note(reading)
        # 维护「最后确认过的 PAUSED」时间戳（用途见 `_settle_lost`）。
        # 窗口消失的过程本身会让识别失败（控件读不到 → UNKNOWN），
        # 那是「读不到」，不是「状态变了」，不能拿它当判据。
        if reading.state is DurationState.PAUSED:
            self._paused_confirmed_ts = time.time()
        elif reading.state is DurationState.RUNNING:
            self._paused_confirmed_ts = 0.0   # 确认在计时后，之前的「已暂停」作废
        # 输入层关闭保护依赖最新状态做判据：PAUSED 时它完全不干预，
        # 所以这里必须每轮同步，绝不能只在「状态变化时」同步。
        self.guard.set_state(self.sm.state)
        if changed:
            self._log(LEVEL_INFO, EventKind.STATE_CHANGED,
                      f"时长状态变更 → {reading.state.value}（{reading.summary()}）",
                      {"state": reading.state.value})
            if reading.state is DurationState.PAUSED:
                self._pause_failed = False
                self._pause_fails = 0
        elif reading.conflict:
            self._log(LEVEL_WARN, EventKind.NOTICE, f"识别结果冲突：{reading.summary()}")

    # ------------------------------------------------------------ 关闭保护
    def _apply_close_policy(self, now: float) -> None:
        if not self._protect_enabled:
            self.close.enforce(CloseDecision.ALLOW)
            return
        if self._allowed_close:
            return
        decision = self.close.policy(self.sm.state, pause_failed=self._pause_failed)
        self.close.enforce(decision)

    def _consume_close_intents(self) -> bool:
        """消费「用户点了 ✕」的意图。返回本轮是否真的走了一次关闭流程。

        为什么要有返回值：`_close_flow` 会改变对外的决策（比如从 BLOCK_RUNNING
        变成 BLOCK_PAUSE_FAILED），而状态是在一轮 tick 的**末尾**上报的。
        本机一轮 tick 含整窗 OCR，要 1.3s 以上 —— 于是「暂停失败」这个刚发生的事实
        会滞后一轮才出现在界面上，用户看到的仍是上一轮的旧决策。
        这与「关闭意图必须排在慢步骤前面」是同一类问题，所以由调用方立刻补报一次。
        """
        if not self._protect_enabled:
            return False
        intents = self.close.pop_close_intents() + self.close.pop_alt_f4_intents()
        if not intents:
            return False
        intent = intents[0]
        self._log(LEVEL_INFO, EventKind.CLOSE_REQUESTED,
                  f"检测到关闭雷神的意图（{intent['kind']}）")
        self._close_flow(intent["kind"])
        return True

    def _close_flow(self, reason: str) -> None:
        """规格书 §十二 的完整流程：拦截 → 读状态 → 必要时暂停 → 验证 → 放行/阻止。"""
        if self.win is None:
            return
        self._log(LEVEL_INFO, EventKind.CLOSE_REQUESTED, f"关闭请求已被拦截（来源={reason}）")

        reading = self.detector.detect(self.win, allow_ocr=True, uia_fast=True)
        self._note_reading(reading)
        state = reading.state

        # 情况1：已暂停 → 直接放行
        if state is DurationState.PAUSED:
            self._allow_and_close("时长已确认为暂停")
            return

        # 情况3：未知 → 禁止关闭，且不做任何点击
        if state is DurationState.UNKNOWN:
            decision = self.close.policy(DurationState.UNKNOWN)
            self.close.enforce(decision, force=True)
            msg = BLOCK_MESSAGES.get(decision, BLOCK_MESSAGES[CloseDecision.BLOCK_UNKNOWN])
            self._log(LEVEL_WARN, EventKind.CLOSE_BLOCKED, f"状态未知，已阻止关闭：{reading.summary()}")
            self._notice("已阻止关闭雷神", msg, [("知道了", ACTION_OK),
                                                ("重试识别", ACTION_PAUSE_NOW)], strong=True)
            return

        # 情况2：正在计时 → 先暂停，成功再放行（复用上面刚读到的状态，不再重读）
        self._log(LEVEL_INFO, EventKind.PAUSE_REQUESTED, "时长正在消耗，先执行暂停")
        outcome = self.controller.pause(self.win, before_reading=reading)
        self.sink.on_pause(outcome)
        if outcome.success:
            self._pause_failed = False
            self._pause_fails = 0
            self._note_reading(Reading(state=DurationState.PAUSED, evidence=outcome.evidence,
                                       hwnd=self.win.hwnd, rect=self.win.rect))
            self._log(LEVEL_INFO, EventKind.PAUSE_SUCCEEDED, outcome.detail)
            self._allow_and_close("暂停成功")
        else:
            self._pause_failed = True
            self._pause_fails += 1
            self._log(LEVEL_ERROR, EventKind.PAUSE_FAILED, outcome.detail)
            decision = self.close.policy(DurationState.RUNNING, pause_failed=True)
            self.close.enforce(decision, force=True)
            msg = BLOCK_MESSAGES.get(decision, BLOCK_MESSAGES[CloseDecision.BLOCK_PAUSE_FAILED])
            self._log(LEVEL_WARN, EventKind.CLOSE_BLOCKED, "暂停失败，已阻止关闭")
            self._notice("已阻止关闭雷神", msg + f"\n\n（失败原因：{outcome.detail}）",
                         [("知道了", ACTION_OK), ("立即暂停", ACTION_PAUSE_NOW)], strong=True)

    def _allow_and_close(self, why: str) -> None:
        auto = bool(self.config.get("close_protection.auto_close_after_pause", True))
        self.close.enforce(CloseDecision.ALLOW, force=True)
        self._allowed_close = True
        # 放行后输入层也不许再拦：否则用户点 ✕ 会被吞掉，界面看起来「点了没反应」
        self.guard.set_enabled(False)
        if auto:
            self._log(LEVEL_INFO, EventKind.CLOSE_ALLOWED, f"放行关闭（{why}）")
            self.close.close_window()
        else:
            self._log(LEVEL_INFO, EventKind.CLOSE_ALLOWED, f"放行关闭（{why}），请自行关闭雷神")
            self._notice("现在可以关闭雷神了", f"{why}。雷神已可正常关闭。",
                         [("知道了", ACTION_OK)])

    # ------------------------------------------------- 关闭保护 v2（输入层）回调
    def _guard_ensure_paused(self) -> DurationState:
        """给输入层关闭保护用的「确保已暂停」，返回**重新检测后**的状态。

        与规格书 §十 / §十四 完全一致：
          读状态 → 已 PAUSED 直接返回 → UNKNOWN **不点任何东西**（情况3）
          → RUNNING 才执行暂停 → 仍然以「重新检测到 PAUSED」为唯一成功依据。

        返回非 PAUSED 时，关闭保护会**拒绝放行**那次 ✕ 点击（Fail Safe）。

        外面包了一层计时：这条路径的耗时就是用户感受到的「点 ✕ 之后要等多久」，
        必须能从日志里直接读出来（用户要求做到 1~2 秒）。
        """
        t0 = time.time()
        try:
            return self._guard_ensure_paused_impl()
        finally:
            elapsed = (time.time() - t0) * 1000.0
            try:
                uia_ms = float(getattr(self.detector, "last_uia_ms", 0.0) or 0.0)
            except Exception:
                uia_ms = 0.0
            self._log(LEVEL_INFO, EventKind.NOTICE,
                      f"关闭保护：确保暂停耗时 {elapsed:.0f}ms"
                      f"（其中 UIA 扫描 {uia_ms:.0f}ms）")

    def _guard_ensure_paused_impl(self) -> DurationState:
        if self.win is None:
            return DurationState.UNKNOWN
        # 这是「点 ✕ 之后」的第一跳，必须最快：走控件缓存，只重读 1~2 个控件的 Name。
        reading = self.detector.detect(self.win, allow_ocr=True, uia_fast=True)
        self._note_reading(reading)
        if reading.state is DurationState.PAUSED:
            return DurationState.PAUSED
        if reading.state is DurationState.UNKNOWN:
            self._log(LEVEL_WARN, EventKind.CLOSE_BLOCKED,
                      f"状态未知，拒绝放行关闭：{reading.summary()}")
            return DurationState.UNKNOWN
        # 把刚读到的状态交给暂停流程复用：这里已经付过一次识别成本了，
        # 再读一遍纯属浪费 —— 用户盯着的是「点完 ✕ 多久才停」这件事。
        outcome = self.controller.pause(self.win, before_reading=reading)
        self.sink.on_pause(outcome)
        if outcome.success:
            self._pause_failed = False
            self._pause_fails = 0
            self._note_reading(Reading(state=DurationState.PAUSED, evidence=outcome.evidence,
                                       hwnd=self.win.hwnd, rect=self.win.rect))
            self._log(LEVEL_INFO, EventKind.PAUSE_SUCCEEDED, outcome.detail)
            return DurationState.PAUSED
        self._pause_failed = True
        self._pause_fails += 1
        self._log(LEVEL_ERROR, EventKind.PAUSE_FAILED, outcome.detail)
        return DurationState.RUNNING

    def _confirm_status(self) -> dict:
        """层级3（确认框监测）的状态，供 UI 如实展示。"""
        g = getattr(self, "guard", None)
        if g is None or getattr(g, "confirm", None) is None:
            return {"enabled": False}
        cw = g.confirm
        return {
            "enabled": bool(cw.enabled),
            "mode": cw.mode,
            "hook_installed": bool(cw.listener and cw.listener.installed),
            "hook_error": (cw.listener.error if cw.listener else ""),
            "seen": g.stats.get("confirm_seen", 0),
            "paused": g.stats.get("confirm_paused", 0),
            "ocr_checked": cw.stats.get("checked", 0),
            # OCR 扫描线程必须活着，否则 `poll()` 永远取不到东西，层级3 形同虚设。
            # 把它暴露出来，真机上「层级3 到底在不在跑」就不再靠猜。
            "ocr_thread": bool(cw._scan_thread and cw._scan_thread.is_alive()),
            "pending_hits": len(cw._hits),
            "errors": cw.stats.get("errors", 0),
            "last_detail": cw.last_detail,
        }

    def _consume_guard_events(self) -> None:
        """把输入层/层级3 的事件转成用户可见的通知。

        为什么需要它：`CloseIntentGuard` 跑在后台线程里，它的结论（吞点、重放、
        发现确认框、自动暂停、死手降级）如果没人消费，用户就永远不会知道
        —— 那就等于「做了但没告诉用户」，与「假装成功」是同一类问题。
        """
        g = getattr(self, "guard", None)
        if g is None:
            return
        for ev in list(g.events):
            i = ev.get("i", 0)
            if i <= self._guard_seen_seq:
                continue
            self._guard_seen_seq = i
            kind = ev.get("ev")
            if kind == "close_released":
                # 真机第 4/5 轮的共同结尾：暂停已复核为 PAUSED、关闭动作已放行，
                # 随后**雷神会弹它自己的确认框**。这两件事用户都必须被告知：
                #   (1) 时长已经停了（这就是本工具存在的全部意义）；
                #   (2) 那个框要**用户自己选** —— 我们不替他点（§二/§四：
                #       不碰雷神的控件语义，只在必要时点「暂停时长」）。
                # 少了 (2)，用户会以为程序卡住了或者把框关掉就等于退出。
                self._notice("已暂停总时长，可以关闭雷神了",
                             f"{ev.get('reason') or '总时长已暂停'}。"
                             "雷神随后弹出的确认框请你自己选："
                             "想让它继续待在后台就选「最小化到托盘」，"
                             "想彻底退出就选「退出加速器」。"
                             "时长已停，无论选哪个都不会继续消耗。",
                             [("知道了", ACTION_OK)])
            elif kind == "confirm_paused":
                self._notice("已自动暂停总时长",
                             ev.get("detail") or "发现雷神确认框，已先暂停总时长。",
                             [("知道了", ACTION_OK)])
            elif kind == "confirm_pause_failed":
                self._notice("暂停未成功，请手动处理",
                             f"{ev.get('reason', '')}。{ev.get('detail', '')}",
                             [("知道了", ACTION_OK)], strong=True)
            elif kind == "degraded":
                self._notice("关闭保护已暂时降级",
                             ev.get("reason") or "输入层拦截已停止。",
                             [("知道了", ACTION_OK)], strong=True)

    def _on_close_blocked(self, state_value: str) -> None:
        """输入层拦下了一次 ✕ 点击但无法确认已暂停 → 必须明确通知用户。"""
        self._notice("已阻止关闭雷神",
                     BLOCK_MESSAGES[CloseDecision.BLOCK_PAUSE_FAILED]
                     + f"\n\n（当前状态：{state_value}）",
                     [("知道了", ACTION_OK), ("立即暂停", ACTION_PAUSE_NOW)], strong=True)

    def _consume_pause_request(self) -> None:
        if not self._want_pause:
            return
        self._want_pause = False
        self._log(LEVEL_INFO, EventKind.PAUSE_REQUESTED, "收到手动暂停请求")
        outcome = self.controller.pause(self.win)
        self.sink.on_pause(outcome)
        if outcome.success:
            self._pause_failed = False
            self._note_reading(Reading(state=DurationState.PAUSED, evidence=outcome.evidence,
                                       hwnd=self.win.hwnd, rect=self.win.rect))
            self._log(LEVEL_INFO, EventKind.PAUSE_SUCCEEDED, outcome.detail)
        else:
            self._pause_failed = True
            self._log(LEVEL_WARN, EventKind.PAUSE_FAILED, outcome.detail)

    # ------------------------------------------------------------ 游戏保护
    def _maybe_auto_pause_on_game_exit(self, now: float) -> None:
        if not self.config.get("game_monitor.enabled", True):
            return
        g = self.games.poll()
        self._game = g
        if not (self._protect_enabled and g["ready_to_pause"]):
            return
        if now - self._last_auto_pause < 60:      # 限流：一分钟内不重复触发
            return
        self._last_auto_pause = now
        self.games.reset()
        self._log(LEVEL_INFO, EventKind.GAME_EXITED,
                  f"所有被监控游戏已退出 {g['exited_seconds']:.0f} 秒，执行自动保护")
        outcome = self.controller.pause(self.win)
        self.sink.on_pause(outcome)
        if outcome.success:
            self._pause_failed = False
            self._note_reading(Reading(state=DurationState.PAUSED, evidence=outcome.evidence,
                                       hwnd=self.win.hwnd, rect=self.win.rect))
            self._log(LEVEL_INFO, EventKind.PAUSE_SUCCEEDED, f"游戏退出自动保护：{outcome.detail}")
        else:
            self._pause_failed = True
            self._log(LEVEL_WARN, EventKind.PAUSE_FAILED,
                      f"游戏退出自动保护失败：{outcome.detail}")
            self._notice("自动暂停失败",
                         "游戏已退出但未能确认总时长已暂停。\n" + outcome.detail,
                         [("立即暂停", ACTION_PAUSE_NOW), ("知道了", ACTION_OK)], strong=True)

    # ------------------------------------------------------------ follow
    def _handle_follow_exit(self, now: float) -> None:
        if not self.config.get("general.follow_leigod", True):
            return
        if self.win is not None:
            self._gone_since = None
            return
        if self._gone_since is None:
            self._gone_since = now
            return
        limit = max(10, int(self.config.get("general.follow_exit_seconds", 90)))
        if now - self._gone_since >= limit:
            if self.log:
                self.log.info("跟随模式：雷神已退出 %.0f 秒，准备结束本程序", now - self._gone_since)
            self._quit_requested.set()

    # ------------------------------------------------------------ 上报
    def _emit_status(self) -> None:
        win = self.win
        status = {
            "ts": time.time(),
            "uptime": time.time() - self._started_ts,
            "protect_enabled": self._protect_enabled,
            "leigod_found": win is not None,
            "window": win.as_dict() if win else None,
            "state": self.sm.state.value,
            "state_detail": self.sm.current.summary(),
            # 最近一次 UIA 扫描耗时：性能问题必须可量化，
            # 否则「延迟降没降」只能靠感觉（本项目已经因此吃过一次亏）。
            "uia_ms": float(getattr(self.detector, "last_uia_ms", 0.0) or 0.0),
            # 有效识别节拍（毫秒）。面板要显示的是**这一刻真实的**节拍，
            # 而不是配置里的基准值 —— 自适应轮询下两者本来就不同。
            "poll_ms": int(round(self._poll_interval() * 1000)),
            "state_ts": self.sm.current.ts,
            "conflict": self.sm.current.conflict,
            "decision": self.close.last_report.decision.value,
            "sc_close_disabled": self.close.last_report.sc_close_disabled,
            "protection_detail": self.close.last_report.detail,
            # 输入层关闭保护（主路径）的真实状态，UI 必须能如实展示：
            # 「已开启」不等于「有效」——未提权时它对提权运行的雷神是无效的。
            "input_layer": {
                "method": self.config.get("close_protection.method", "input_swallow"),
                "enabled": self.guard.enabled,
                "hook_installed": self.guard.watcher.installed,
                "hook_error": self.guard.watcher.error,
                "elevated": self.guard.status()["elevated"],
                "swallowed": self.guard.stats["swallowed"],
                "replayed": self.guard.stats["replayed"],
                "blocked": self.guard.stats["blocked"],
                "false_swallow": self.guard.stats["false_swallow"],
                # 死手窗口与实测 tick 节拍。这两项是「吞点为什么忽然不吞了」
                # 的唯一线索：窗口若被顶穿，✕ 会在用户不知不觉间恢复可用。
                "deadman_ms": round(self.guard.deadman * 1000.0),
                "arm_ms": round(self.guard.status().get("arm_ms", 0.0) * 1000.0),
                "tick_ms": round(self.guard.status().get("tick_ms", 0.0) * 1000.0),
                "errors": list(self.guard.errors[-3:]),
            },
            # 关闭意图的消费情况（输入层吞下 ✕ 之后，消费端有没有及时接住）。
            # `expired` 持续增长就意味着「点了 ✕ 没反应」——这是本项目的头号
            # 故障模式，必须让它在状态里可见，而不是等用户来投诉。
            "close_intent": {
                "accepted": int(self.close.intent_stats.get("accepted", 0)),
                "expired": int(self.close.intent_stats.get("expired", 0)),
                "missed": int(self.close.intent_stats.get("missed", 0)),
                "max_age_ms": round(self.close._intent_max_age() * 1000.0),
                "interval_ms": round(self.close._intent_interval * 1000.0),
                "note": self.close.last_intent_note,
            },
            # 层级3（确认框监测）：与输入层**无关**的一条降级保护，
            # 兜住触摸/远程桌面/托盘菜单退出/钩子失效这些绕过输入层的情形。
            "confirm_layer": self._confirm_status(),
            "pause_failed": self._pause_failed,
            "pause_fails": self._pause_fails,
            # 退出前是否已具备「安全退出」条件。UI 的退出按钮据此决定
            # 是直接退，还是先暂停、并明确告知用户。
            "exit": self._exit_status(),
            "forced_allow": bool(self.close._forced_allow_hwnd),
            "game": getattr(self, "_game", None),
            "transitions": [(round(t, 3), a.value, b.value) for t, a, b in self.sm.transitions[-10:]],
        }
        self.sink.on_status(status)
        self._last_status = status

    def _exit_status(self) -> dict:
        safe, why = self.exit_readiness()
        return {"safe": safe, "why": why}

    def _notice(self, title: str, message: str, actions=None, strong: bool = False) -> None:
        if not self.config.get("general.notifications", True):
            return
        self.sink.on_notice(Notice(title=title, message=message,
                                   actions=actions or [("知道了", ACTION_OK)],
                                   strong=strong,
                                   timeout=0 if strong else 15))

    def _log(self, level: str, kind: EventKind, message: str, data: dict = None) -> None:
        ev = Event(kind=kind, message=message, level=level, data=data or {})
        if self.log:
            getattr(self.log, "error" if level == LEVEL_ERROR
                    else ("warning" if level == LEVEL_WARN else "info"))("%s %s", kind.value, message)
        self.sink.on_event(ev)
