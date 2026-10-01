"""Qt 应用装配：把引擎、伴生面板、托盘、通知接到一起（规格书 §十八 / §十九）。

线程模型（重要）：
  保护引擎跑在自己的线程里；它的回调（sink）在**引擎线程**被调用。
  Qt 控件只能在主线程操作，因此这里用一个 QObject + Signal 做「跨线程搬运」，
  Qt 会自动用队列连接把回调投递到主线程。绝不在引擎线程里直接 setText()。

装配顺序也有讲究：
  1. 先建 QApplication —— Qt6 会自己声明 Per-Monitor-V2 DPI 感知；
  2. 再建引擎 —— 引擎内部的 cf.set_dpi_aware() 会因"已设置"而静默跳过，
     不会把 Qt 已经设好的 DPI 模式改坏；
  3. 面板/托盘的定位都基于引擎上报的物理像素矩形，除以屏幕 DPR 换算成逻辑像素。
"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from core.events import (ACTION_CANCEL_QUIT, ACTION_OK, ACTION_QUIT_ANYWAY, LEVEL_ERROR,
                         Event, EventKind, Notice)
from core.protection_engine import ProtectionEngine, Sink as EngineSink
from detection import coordinate_fallback as cf
from launcher import launcher
from ui import theme
from ui.guard_window import GuardWindow
from ui.notifications import Notifier
from ui.tray import Tray


class QtSink(QObject, EngineSink):
    """引擎 → Qt 主线程的信号桥。"""

    sig_status = Signal(dict)
    sig_notice = Signal(object)
    sig_pause = Signal(object)
    sig_event = Signal(object)

    def on_event(self, event):
        self.sig_event.emit(event)

    def on_status(self, status):
        self.sig_status.emit(status)

    def on_notice(self, notice):
        self.sig_notice.emit(notice)

    def on_pause(self, outcome):
        self.sig_pause.emit(outcome)


class GuardApp(QObject):
    """一次完整的图形化运行。run() 返回进程退出码。"""

    #: 退出前等待「自动暂停」成功的宽限期。超过它就不再无限等待，
    #: 而是把选择权明确交回用户（仍然退出 / 取消退出）——既不静默退出，
    #: 也不把用户锁在程序里。
    QUIT_GRACE_S = 10.0

    def __init__(self, config, logger=None, start_minimized: bool = False,
                 show_panel: bool = True):
        super().__init__()
        self.config = config
        self.log = logger
        self.start_minimized = start_minimized
        self.show_panel = show_panel
        self.app = None
        self.engine = None
        self.sink = None
        self.panel = None
        self.tray = None
        self.notifier = None
        self._last_status = {}
        self._quitting = False
        #: 退出流程已启动（正在等暂停确认）
        self._quit_pending = False
        self._quit_deadline = 0.0
        #: 已经问过用户「要不要就这样退出」，避免重复弹窗
        self._quit_asked = False

    # ------------------------------------------------------------ 装配
    def build(self) -> None:
        if self.app is not None:
            return
        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        self.app.setApplicationName("雷神总时长保护")
        self.app.setQuitOnLastWindowClosed(False)
        try:
            self.app.setStyleSheet(theme.BASE_QSS)
        except Exception:
            pass

        cb = {
            "pause": self._pause,
            "allow_close": self._allow_close,
            "toggle": self._set_protection,
            "toggle_protect": self._set_protection,
            "toggle_panel": self._toggle_panel,
            "switch_side": self._switch_side,
            "set_pinned": self._set_pinned,
            "recheck": self._recheck,
            "open_config": self._open_config,
            "open_logs": self._open_logs,
            "inspector": self._open_inspector,
            "calibrate": self._open_calibration,
            "quit": self._quit,
            "cancel_quit": self._cancel_quit,
            "quit_anyway": lambda: self._finish_quit(forced=True),
        }
        self.panel = GuardWindow(cb, logger=self.log,
                                 pinned=bool(self.config.get("ui.pin_side", True)))
        if Tray.available():
            self.tray = Tray(cb, logger=self.log)
            self.tray.show()
        elif self.log:
            self.log.warning("系统托盘不可用，只能通过面板操作")

        self.notifier = Notifier(panel=self.panel, tray=self.tray, logger=self.log)

        self.sink = QtSink()
        self.sink.sig_status.connect(self._on_status)
        self.sink.sig_notice.connect(self._on_notice)
        self.sink.sig_pause.connect(self._on_pause)
        self.sink.sig_event.connect(self._on_event)

        self.engine = ProtectionEngine(self.config, sink=self.sink, logger=self.log)

        interval = max(120, int(self.config.get("ui.refresh_ms", 500)))
        # 让面板能显示「状态每 x.xs 刷新一次」：识别延迟到底降没降，要看得见。
        self.panel.set_poll_interval(int(self.config.get("duration.poll_ms", 1000)))
        self._t_follow = QTimer(self)
        self._t_follow.setInterval(interval)
        self._t_follow.timeout.connect(self._tick)

        # 几何快跟：**只读一次 GetWindowRect 并把面板挪过去**，不查进程、不做识别。
        # 为什么必须有它：状态轮询（duration.poll_ms，默认 1000ms）太慢，面板位置
        # 跟着它走时，拖动雷神会出现「窗口走了、面板还在原地，半秒后啪地归位」——
        # 用户描述成「吸附度不高、会乱跳」。位置跟随和状态刷新是两件事，节拍不该共用。
        self._t_geom = QTimer(self)
        self._t_geom.setInterval(max(50, int(self.config.get("ui.follow_fast_ms", 100))))
        self._t_geom.timeout.connect(self._tick_geom)

        self._t_quit = QTimer(self)
        self._t_quit.setInterval(1000)
        self._t_quit.timeout.connect(self._check_quit)

    # ------------------------------------------------------------ 运行
    def run(self, on_ready=None) -> int:
        self.build()

        # 权限自检：不匹配时关闭保护会静默失效，必须当面告知
        priv = launcher.privilege_report(self.config)
        if not priv["ok"]:
            if self.log:
                self.log.error("权限不匹配：%s", priv["message"])
            self.notifier.handle(Notice(title="权限不足，关闭保护可能失效",
                                       message=priv["message"],
                                       actions=[("知道了", "ok")], strong=True, timeout=0))
        elif priv.get("leigod_elevated") and self.log:
            self.log.info("雷神与本程序权限一致（均为管理员）")

        self.engine.start()
        self._t_follow.start()
        self._t_geom.start()
        self._t_quit.start()

        if self.start_minimized and self.tray:
            self.panel.hide_panel()
        else:
            self.panel.show_panel()
            self.panel.apply_status({})

        if not self.show_panel and self.tray:
            self.panel.hide_panel()

        if on_ready is not None:
            # 自动化验收用：事件循环起来之前把后续动作挂上
            on_ready(self)

        return self.app.exec()

    # ------------------------------------------------------------ 引擎事件
    def _on_status(self, status: dict) -> None:
        self._last_status = status or {}
        try:
            self.panel.apply_status(status)
        except Exception as e:
            if self.log:
                self.log.exception("面板刷新失败: %s", e)
        if self.tray:
            try:
                self.tray.set_state(status)
            except Exception as e:
                if self.log:
                    self.log.exception("托盘刷新失败: %s", e)

    def _on_notice(self, notice) -> None:
        self.notifier.handle(notice)

    def _on_pause(self, outcome) -> None:
        if outcome is None:
            return
        # 失败一定已经由引擎发过通知；成功在这里补一条轻提示
        if outcome.success and self.log:
            self.log.info("暂停结果：%s", outcome.line())

    def _on_event(self, event) -> None:
        if event is None:
            return
        if event.level in ("WARN", "ERROR") and self.log:
            self.log.info("事件：%s", event.line())

    # ------------------------------------------------------------ 定时
    def _tick(self) -> None:
        # 还没有第一份状态时不要动面板：否则会在「尚未找到雷神」的瞬间把面板藏掉，
        # 用户看到的就是一闪一闪的窗口。
        if not self._last_status:
            return
        win = (self._last_status or {}).get("window")
        self.panel.follow((win or {}).get("frame") or (win or {}).get("rect"))

    def _tick_geom(self) -> None:
        """快速几何跟随：直接读雷神窗口矩形，把面板贴上去。

        刻意**不**在这里做任何识别、也不重新查找窗口（那要枚举进程，几百毫秒级）。
        hwnd 复用状态轮询给出的那个，只用它读一次矩形 —— 于是可以 10Hz 地跑。
        窗口被最小化到托盘（矩形跑到 -25600）时交给状态轮询去处理隐藏，
        这里直接不动，免得面板闪一下。
        """
        win = (self._last_status or {}).get("window") or {}
        hwnd = win.get("hwnd")
        if not hwnd or not self.panel.visible_wanted:
            return
        try:
            from leigod import window as win_mod
            if not win_mod.is_valid(hwnd):
                return
            rect = cf.get_frame_bounds(hwnd) or cf.get_window_rect(hwnd)
        except Exception:
            return
        if not rect or rect[0] < -20000 or rect[1] < -20000:
            return
        self.panel.follow(rect)

    # ------------------------------------------------------- 手动重新检测
    def _recheck(self) -> None:
        """「重新检测状态」：交给引擎做（重定位窗口 + 强制全量识别 + 如实汇报）。

        界面先把话说在前面（"正在检测…"），因为强制跑 OCR 需要一两秒 ——
        否则用户点完没反应会以为按钮坏了。
        """
        if not self.engine:
            return
        if self.log:
            self.log.info("用户请求重新检测时长状态")
        try:
            self.notifier.simple("正在重新检测…",
                                 "重新定位雷神窗口并完整识别一次时长状态，请稍候。")
        except Exception:
            pass
        try:
            self.engine.request_recheck()
        except Exception as e:
            if self.log:
                self.log.exception("触发重新检测失败: %s", e)

    # ------------------------------------------------------- 面板位置控制
    def _switch_side(self) -> None:
        """把面板换到雷神的另一侧（治「想固定在一角但选错了边」）。"""
        try:
            self.panel.switch_side()
            if self.log:
                self.log.info("面板已换到另一侧：%s", self.panel.docked_side)
        except Exception as e:
            if self.log:
                self.log.exception("换边失败: %s", e)

    def _set_pinned(self, on: bool) -> None:
        """钉住/取消钉住，并**写回配置** —— 这是用户的一次明确选择，下次启动该记住。"""
        on = bool(on)
        try:
            self.panel.set_pinned(on)
        except Exception as e:
            if self.log:
                self.log.exception("设置钉住失败: %s", e)
        try:
            self.config.set("ui.pin_side", on)
        except Exception as e:
            if self.log:
                self.log.warning("保存「钉住」设置失败（本次运行仍生效）：%s", e)

    def _check_quit(self) -> None:
        if self.engine and self.engine.quit_requested:
            if self.log:
                self.log.info("引擎请求退出（跟随模式）")
            self._quit()
            return
        # 退出流程在等「自动暂停」生效：只要变安全就立刻退，不白等满宽限期
        if self._quit_pending:
            safe, why = self.engine.exit_readiness() if self.engine else (True, "")
            if safe:
                if self.log:
                    self.log.info("退出前确认：%s", why)
                self._finish_quit(why=why)
                return
            if time.time() >= self._quit_deadline:
                self._ask_quit_anyway(why)

    # ------------------------------------------------------------ 面板/托盘动作
    def _pause(self) -> None:
        if self.engine:
            self.engine.request_pause()

    def _allow_close(self) -> None:
        if self.engine:
            self.engine.request_allow_close()

    def _set_protection(self, on: bool) -> None:
        if self.engine:
            self.engine.set_protection(bool(on))

    def _toggle_panel(self) -> None:
        self.panel.toggle_panel()
        if self.panel.visible_wanted:
            self._tick()

    def _open_config(self) -> None:
        launcher.reveal(self.config.path, self.log)

    def _open_logs(self) -> None:
        from core.logging_setup import LOG_DIR
        launcher.reveal(LOG_DIR, self.log)

    def _open_inspector(self) -> None:
        p = launcher.relaunch_tool(["diagnostics.ui_inspector"], ["--inspector"], logger=self.log)
        if p is None:
            self.notifier.simple("无法启动界面诊断", "请手动运行：python -m diagnostics.ui_inspector")
        else:
            self.notifier.simple("界面诊断已启动",
                                 "在新打开的控制台里查看控件树、截图与状态结论。\n"
                                 "产物目录：logs/inspector")

    def _open_calibration(self) -> None:
        p = launcher.relaunch_tool(["diagnostics.calibration", "--mode", "auto"],
                                   ["--calibrate", "--mode", "auto"], logger=self.log)
        if p is None:
            self.notifier.simple("无法启动坐标校准",
                                 "请手动运行：python -m diagnostics.calibration")
        else:
            self.notifier.simple(
                "坐标校准已启动",
                "注意：校准会**真的点一次**「暂停时长」并验证是否变成「开启时长」，"
                "只有验证通过才会写入配置。")

    # ------------------------------------------------------------ 退出
    def _quit(self, force: bool = False) -> None:
        """退出请求。**默认先确保雷神已暂停**，而不是只管把自己关掉。

        本程序存在的唯一目的就是别让总时长白白消耗。若它在雷神正在计时的时候
        悄悄退出，用户会以为"已经收拾干净了"，而计时器其实还在跑——那正是要防的事。
        所以退出自己的路径也必须走同一条纪律：读状态 → 确认已暂停 → 才真的退。

        流程：
          安全（已暂停 / 雷神没在运行 / 保护已关）→ 直接退；
          不安全（RUNNING 或 UNKNOWN）→ 发起一次暂停，给一个宽限期等它生效，
          期间绝不静默退出；宽限期满仍未确认，就把选择权明确交回用户。
        """
        if force:
            self._finish_quit(forced=True)
            return
        if self._quitting or self._quit_pending:
            return

        safe, why = self.engine.exit_readiness() if self.engine else (True, "引擎未启动")
        if safe:
            if self.log:
                self.log.info("退出前检查通过：%s", why)
            self._finish_quit(why=why)
            return

        self._quit_pending = True
        self._quit_deadline = time.time() + self.QUIT_GRACE_S
        if self.log:
            self.log.warning("退出前需先暂停雷神（%s），已发起暂停并等待确认", why)
        if self.engine:
            self.engine.request_pause()
        self.notifier.handle(Notice(
            title="退出前正在暂停雷神总时长",
            message=(f"{why}。已发起暂停，确认变成「开启时长」后再退出。\n"
                     f"最迟 {self.QUIT_GRACE_S:.0f} 秒；若届时仍无法确认，会再问你一次。"),
            actions=[("取消退出", ACTION_CANCEL_QUIT)],
            strong=True, timeout=0))

    def _cancel_quit(self) -> None:
        if not self._quit_pending:
            return
        self._quit_pending = False
        self._quit_asked = False
        if self.log:
            self.log.info("用户取消退出；程序继续保护")
        self.notifier.simple("已取消退出", "程序会继续盯着雷神的总时长。")

    def _ask_quit_anyway(self, why: str) -> None:
        """宽限期已过仍无法确认暂停 → 把决定权交给用户，不替用户决定。"""
        self._quit_asked = True
        self._quit_pending = False
        if self.log:
            self.log.warning("退出前未能确认雷神已暂停（%s），等待用户选择", why)
        self.notifier.handle(Notice(
            title="未能确认雷神已暂停",
            message=(f"{why}。\n"
                     "现在退出的话，雷神可能会继续消耗总时长。\n"
                     "建议先在雷神里点一次「暂停时长」，再退出本程序。"),
            actions=[("取消退出", ACTION_CANCEL_QUIT),
                     ("仍然退出（雷神会继续计时）", ACTION_QUIT_ANYWAY)],
            strong=True, timeout=0))

    def _finish_quit(self, forced: bool = False, why: str = "") -> None:
        if self._quitting:
            return
        self._quitting = True
        self._quit_pending = False
        if forced:
            # 这是唯一允许「把雷神留在计时状态」的出口，必须留下明确证据，
            # 不能只在界面上闪一下就算告知过（§三十三 不许假装成功）。
            if self.log:
                self.log.error("用户选择在未确认暂停的情况下退出本程序；"
                               "雷神可能仍在消耗总时长。")
            try:
                self.sink.on_event(Event(kind=EventKind.WINDOW_CLOSED_UNPROTECTED,
                                         level=LEVEL_ERROR,
                                         message="退出时未确认雷神已暂停（用户显式选择）"))
            except Exception:
                pass
        if self.log:
            self.log.info("正在退出……%s", f"（{why}）" if why else "")
        try:
            self._t_follow.stop()
            self._t_geom.stop()
            self._t_quit.stop()
        except Exception:
            pass
        if self.engine:
            self.engine.stop()          # 内部会把 ✕ 还给用户
        if self.tray:
            self.tray.hide()
        if self.panel:
            self.panel.hide_panel()
        if self.app:
            self.app.quit()
