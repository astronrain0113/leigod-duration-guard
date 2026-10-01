"""托盘图标与菜单（Qt 原生 QSystemTrayIcon，不额外引入 pystray）。

图标是**代码画出来的**（QPixmap 上画一个圆角方牌 + 右下角状态色角标），
所以不需要任何外部位图资源，颜色的含义与面板完全一致：
红=计时中（危险）、绿=已暂停、琥珀=无法确认、灰=未找到雷神。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QObject
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ui import theme, widgets


def make_icon(state: str, found: bool = True) -> QIcon:
    """按状态生成托盘图标（16/24/32/48/64，任务栏与列表都清晰）。

    图形来自 `ui.widgets.tray_pixmap` —— 与面板顶部的品牌标记同一套画法，
    颜色语义也一致：红=计时中（危险）、绿=已暂停、琥珀=无法确认、灰=未找到雷神。
    """
    if not found:
        color = "#9AA3B2"
    else:
        _, color, _ = theme.state_style(state)
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        icon.addPixmap(widgets.tray_pixmap(size, color, found=found))
    return icon


class Tray(QObject):
    """托盘控制器。所有动作都通过 callbacks 交回 ui.app，自己不碰引擎。"""

    def __init__(self, callbacks: dict, logger=None, parent=None):
        super().__init__(parent)
        self.cb = callbacks or {}
        self.log = logger
        self.state = "UNKNOWN"
        self.found = False

        self.icon = QSystemTrayIcon(make_icon("UNKNOWN", False), self)
        self.icon.setToolTip("雷神总时长保护 —— 未找到雷神")
        self.menu = QMenu()

        self.act_show = QAction("显示 / 隐藏面板", self.menu)
        self.act_show.triggered.connect(lambda: self._call("toggle_panel"))
        self.menu.addAction(self.act_show)

        self.act_state = QAction("状态：—", self.menu)
        self.act_state.setEnabled(False)
        self.menu.addAction(self.act_state)
        self.menu.addSeparator()

        self.act_pause = QAction("立即暂停时长", self.menu)
        self.act_pause.triggered.connect(lambda: self._call("pause"))
        self.menu.addAction(self.act_pause)

        # 状态读不出来时用户的第一反应按钮。**永远可用** —— 连"未找到雷神"
        # 时也让它可按，按了会明确告诉用户去把雷神打开。
        self.act_recheck = QAction("重新检测时长状态", self.menu)
        self.act_recheck.setToolTip("重新定位雷神窗口并强制完整识别一次时长状态")
        self.act_recheck.triggered.connect(lambda: self._call("recheck"))
        self.menu.addAction(self.act_recheck)

        self.act_allow = QAction("允许本次关闭雷神", self.menu)
        self.act_allow.triggered.connect(lambda: self._call("allow_close"))
        self.menu.addAction(self.act_allow)

        self.act_protect = QAction("关闭保护", self.menu)
        self.act_protect.setCheckable(True)
        self.act_protect.setChecked(True)
        self.act_protect.toggled.connect(lambda on: self._call("toggle_protect", on))
        self.menu.addAction(self.act_protect)
        self.menu.addSeparator()

        self.act_cfg = QAction("打开配置文件", self.menu)
        self.act_cfg.triggered.connect(lambda: self._call("open_config"))
        self.menu.addAction(self.act_cfg)

        self.act_log = QAction("打开日志目录", self.menu)
        self.act_log.triggered.connect(lambda: self._call("open_logs"))
        self.menu.addAction(self.act_log)

        self.act_insp = QAction("运行界面诊断（UI Inspector）", self.menu)
        self.act_insp.triggered.connect(lambda: self._call("inspector"))
        self.menu.addAction(self.act_insp)

        self.act_cal = QAction("坐标校准", self.menu)
        self.act_cal.triggered.connect(lambda: self._call("calibrate"))
        self.menu.addAction(self.act_cal)
        self.menu.addSeparator()

        self.act_quit = QAction("退出", self.menu)
        self.act_quit.triggered.connect(lambda: self._call("quit"))
        self.menu.addAction(self.act_quit)

        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)

    # ------------------------------------------------------------ 生命周期
    @staticmethod
    def available() -> bool:
        return bool(QSystemTrayIcon.isSystemTrayAvailable())

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def _call(self, name: str, *a) -> None:
        fn = self.cb.get(name)
        if fn:
            try:
                fn(*a)
            except Exception as e:
                if self.log:
                    self.log.exception("托盘动作 %s 失败: %s", name, e)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._call("toggle_panel")

    # ------------------------------------------------------------ 状态同步
    def set_state(self, status: dict) -> None:
        st = status or {}
        state = str(st.get("state") or "UNKNOWN")
        found = bool(st.get("leigod_found"))
        protect = bool(st.get("protect_enabled", True))
        if state != self.state or found != self.found:
            self.state, self.found = state, found
            self.icon.setIcon(make_icon(state, found))
        label, _, _ = theme.state_style(state)
        if not found:
            tip = "雷神总时长保护 —— 未找到雷神窗口"
        else:
            tip = (f"雷神总时长保护 —— {label}"
                   f"{'（保护已关闭）' if not protect else ''}")
        self.icon.setToolTip(tip)
        self.act_state.setText(f"状态：{label}" + ("" if found else "（未找到雷神）"))
        self.act_pause.setEnabled(found and state == "RUNNING")
        self.act_allow.setEnabled(found)
        # 退出前如果不能确认已暂停，就在菜单上直接说清楚 —— 不让用户
        # 以为「点了退出就万事大吉」。点击后仍会走「先暂停再退出」的流程。
        ex = st.get("exit") or {}
        safe = bool(ex.get("safe", True))
        self.act_quit.setText("退出" if safe else "退出（雷神仍在计时）")
        self.act_quit.setToolTip((ex.get("why") or "")
                                 if safe else f"{ex.get('why', '')}；退出前会先尝试暂停")
        if self.act_protect.isChecked() != protect:
            self.act_protect.blockSignals(True)
            self.act_protect.setChecked(protect)
            self.act_protect.blockSignals(False)

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.showMessage(title, message, make_icon(self.state, self.found), 8000)
        except Exception as e:
            if self.log:
                self.log.exception("托盘气泡失败: %s", e)
