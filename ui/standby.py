"""待命守望的托盘界面 —— 让「后台到底有没有在跑」变成看得见的东西。

背景（真机反馈 2026-09-29）：
  用户开启「开机待命」后的原话是「并没有在后台看到工具，打开加速器后也没看到」。
  查下来程序**确实**没被拉起（任务 onlogon 不回溯当前会话），但即便拉起了，
  旧方案只有托盘图标且常被 Windows 收进溢出区（^），表现和没启动一模一样。

  所以待命进程必须有**自己**的托盘图标，并且：
    · 启动时弹一次气泡，明确说"已在后台待命"；
    · 图标颜色区分「等待中 / 已唤起」；
    · 真的唤起守卫时再弹一次气泡，把"我做了什么"说出来
      （§三十三：不许做了不说，也不许没做说做了）。

设计约束：这里**只有托盘**，不建面板、不加载 OCR、不做任何时长判定。
整个进程的资源占用必须远小于完整守卫，它才配"常驻"。
"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from launcher.watch import ALREADY, FAILED, STANDBY, WAKE, Watcher
from ui import widgets

#: 角标色：待命（雷神未运行）—— 中性灰，含义是「安静等着，什么都没做」
COLOR_IDLE = "#8A93A2"
#: 角标色：已唤起（雷神在跑）—— 青绿，含义是「在工作」
COLOR_ACTIVE = "#2E7D5B"


def make_standby_icon(active: bool = False) -> QIcon:
    """待命进程的托盘图标：与守卫**共用同一个品牌方牌**，用右下角标区分状态。

    为什么不再单独画一个圆环（2026-10-01）：同一个产品在托盘里出现两种完全不同的
    图标，用户会以为是两个东西 —— 这正是「面板用方牌、托盘用圆环」犯过的同一个错。

    角标含义（与守卫图标的时长状态是**两个不同的轴**，别混）：
      灰   = 待命中（雷神还没来，什么都没做）
      蓝绿 = 已唤起守卫（正在工作）
    """
    color = COLOR_ACTIVE if active else COLOR_IDLE
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        icon.addPixmap(widgets.tray_pixmap(size, color, found=True))
    return icon


class StandbyApp(QObject):
    """待命守望的 Qt 外壳。run() 返回进程退出码。"""

    def __init__(self, config, logger=None, on_ready=None):
        super().__init__()
        self.config = config
        self.log = logger
        self.on_ready = on_ready
        self.app = None
        self.tray = None
        self.icon = None
        self.menu = None
        self.watcher = Watcher(config, logger=logger)
        self._active = False
        self._t = QTimer(self)
        #: 本进程是否以管理员运行。守望没提权时，唤起守卫要走 runas；
        #: 更糟的是万一提权失败，守卫的**关闭保护会静默失效**（UIPI）。
        #: 这种事必须当场说出来（§三十三），不能等用户自己发现「保护好像没工作」。
        try:
            from launcher.launcher import is_elevated
            self._elevated = bool(is_elevated())
        except Exception:
            self._elevated = True

    # ------------------------------------------------------------ 装配
    def build(self) -> None:
        if self.app is not None:
            return
        self.app = QApplication.instance() or QApplication(sys.argv[:1])
        self.app.setApplicationName("雷神总时长保护 —— 待命")
        self.app.setQuitOnLastWindowClosed(False)

        self.icon = QSystemTrayIcon(make_standby_icon(False), self)
        self.icon.setToolTip("雷神总时长保护 —— 待命中（未检测到雷神）")

        self.menu = QMenu()
        self.act_state = QAction("待命中：未检测到雷神", self.menu)
        self.act_state.setEnabled(False)
        self.menu.addAction(self.act_state)
        self.menu.addSeparator()

        self.act_wake = QAction("立刻唤起保护工具", self.menu)
        self.act_wake.triggered.connect(self._wake_now)
        self.menu.addAction(self.act_wake)
        self.menu.addSeparator()

        self.act_log = QAction("打开日志目录", self.menu)
        self.act_log.triggered.connect(self._open_logs)
        self.menu.addAction(self.act_log)
        self.menu.addSeparator()

        self.act_quit = QAction("退出待命", self.menu)
        self.act_quit.triggered.connect(self._quit)
        self.menu.addAction(self.act_quit)

        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)

        interval = max(500, int(self.watcher.poll * 1000))
        self._t.setInterval(interval)
        self._t.timeout.connect(self._tick)

        # 心跳：每 60 秒写一行「我还活着，检查了 N 次，雷神在不在」。
        # 为什么必须有心跳：守望平时**一个字都不输出**，于是"它还在跑"与
        # "它几分钟前就被外部杀掉了"在日志上长得一模一样 —— 用户报
        # 「待命没生效」时，我只能靠翻文件时间戳和猜。有了心跳，
        # 「最后一次心跳在几分钟前」就是"进程没了"的直接证据。
        self._t_beat = QTimer(self)
        self._t_beat.setInterval(60000)
        self._t_beat.timeout.connect(self._beat)
        self._started_at = 0.0
        self._ticks = 0

    def _beat(self) -> None:
        if self.log:
            self.log.info("待命心跳：已运行 %.0f 分钟，检查 %d 次，最近一次 %s",
                          (time.time() - self._started_at) / 60.0, self._ticks,
                          self.watcher.last_action)
        self._reflect()

    # ------------------------------------------------------------ 运行
    def run(self) -> int:
        self.build()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            if self.log:
                self.log.error("系统托盘不可用，待命守望无法提供可见入口")
        self.icon.show()
        self._started_at = time.time()
        self._t.start()
        self._t_beat.start()
        if self.log:
            self.log.info("待命守望已启动（每 %.1fs 检查一次雷神）", self.watcher.poll)
        # 退出必须留痕：否则「进程没了」永远分不清是正常退出还是被外部杀掉。
        try:
            self.app.aboutToQuit.connect(
                lambda: self.log and self.log.info("待命守望正常退出（存活 %.0f 分钟）",
                                                   (time.time() - self._started_at) / 60.0))
        except Exception:
            pass
        msg = ("图标在任务栏右下角（可能在「^」隐藏图标里）。\n"
               "你打开雷神加速器时，保护工具会自动弹出并开始保护。\n"
               "想现在就打开保护面板：右键这个图标 → 立刻唤起保护工具。")
        if not self._elevated:
            msg += ("\n\n⚠️ 本次待命**没有管理员权限**。唤起保护工具时可能要你再确认一次"
                    "（UAC），而且关闭保护有可能失效。\n"
                    "建议用「开机待命-开启.bat」（右键 → 以管理员身份运行）重新设置。")
            if self.log:
                self.log.warning("待命守望未提权：唤起守卫将走提权启动，关闭保护可能失效")
        self._notify("雷神总时长保护 —— 已在后台待命", msg)
        if self.on_ready is not None:
            self.on_ready(self)
        return self.app.exec()

    # ------------------------------------------------------------ 定时
    def _tick(self) -> None:
        try:
            action = self.watcher.tick()
        except Exception as e:
            if self.log:
                self.log.exception("守望周期异常：%s", e)
            return
        self._ticks += 1
        self._reflect()
        if action == WAKE:
            if self.log:
                self.log.info("守望：检测到雷神，已唤起保护工具")
            self._notify("雷神已启动", "保护工具已唤起，伴生面板已显示在雷神窗口旁。")
        elif action == FAILED:
            # 拉不起来必须说出来，不能静默吞掉（§三十三）
            self._notify("唤起保护工具失败",
                         "检测到雷神已启动，但没能拉起保护工具。\n"
                         "请手动双击 LeigodGuard.exe，或右键本图标再试一次。")
        elif action == STANDBY:
            pass      # 本轮唤起过、用户自己关掉了 —— 安静等待雷神下次启动
        elif action == ALREADY:
            pass      # 守卫在跑，无需打扰

    def _reflect(self) -> None:
        present = self.watcher.leigod_present()
        if present != self._active:
            self._active = present
            self.icon.setIcon(make_standby_icon(present))
        if present:
            tip = "雷神总时长保护 —— 雷神已运行"
            label = "雷神已运行"
        else:
            tip = "雷神总时长保护 —— 待命中（未检测到雷神）"
            label = "待命中：未检测到雷神"
        self.icon.setToolTip(tip)
        self.act_state.setText(label)
        self.act_wake.setEnabled(present)

    # ------------------------------------------------------------ 动作
    def _notify(self, title: str, message: str) -> None:
        try:
            self.icon.showMessage(title, message, make_standby_icon(self._active), 8000)
        except Exception as e:
            if self.log:
                self.log.warning("待命气泡失败：%s", e)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._wake_now()

    def _wake_now(self) -> None:
        if self.watcher.guard_running():
            self._notify("保护工具已在运行", "不需要再唤起一次。")
            return
        ok = self.watcher.launch_guard()
        self.watcher.armed = False      # 手动唤起同样去武装，避免和用户的关闭抢控制权
        self._notify("已唤起保护工具" if ok else "唤起失败",
                     "伴生面板会显示在雷神窗口旁。" if ok else
                     "请手动双击 LeigodGuard.exe 试试。")

    def _open_logs(self) -> None:
        try:
            from core.logging_setup import LOG_DIR
            from launcher import launcher
            launcher.reveal(LOG_DIR, self.log)
        except Exception as e:
            if self.log:
                self.log.warning("打开日志目录失败：%s", e)

    def _quit(self) -> None:
        if self.log:
            self.log.info("待命守望退出（共唤起 %d 次）", self.watcher.wake_count)
        try:
            self._t.stop()
        except Exception:
            pass
        self.icon.hide()
        self.app.quit()
