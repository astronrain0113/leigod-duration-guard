"""随雷神窗口移动的伴生面板（规格书 §十八）。

第一版按规格书要求做「独立窗口 + HWND 位置绑定」：
  · 无边框、圆角、始终置顶、**不抢焦点**（雷神的游戏内输入不受影响）；
  · 默认贴在雷神窗口左侧；左侧放不下（贴屏边）就自动改贴右侧；
  · 跟随周期由 config `ui.refresh_ms`（默认 500ms）驱动；
  · 位置换算全部走「物理像素 ÷ 该屏幕的 devicePixelRatio」，
    125% / 150% 缩放下都对得准，且不写死任何屏幕坐标。

面板只做显示器 + 遥控器：任何「放不放行」的判断都在 core 层，
这里绝不自行决定允许关闭。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from core.events import (ACTION_ALLOW_CLOSE, ACTION_CANCEL_QUIT, ACTION_OK,
                         ACTION_PAUSE_NOW, ACTION_QUIT_ANYWAY, ACTION_RUN_INSPECTOR,
                         Notice)
from ui import theme, widgets

PANEL_WIDTH = 300      # 逻辑像素（含投影留边）
GAP = 12               # 视觉间距：面板卡片与雷神窗口之间（逻辑像素）
MIN_HEIGHT = 240
#: 换边滞回余量（逻辑像素）。没有它时，换边判据是二值的：窗口一贴到左边缘，
#: 面板就"啪"地跳到右侧；拖回来又跳回左侧 —— 用户看到的「吸附度不高、乱跳」
#: 就是这个。换回左侧要多留出这么多余量，形成死区。
SWITCH_MARGIN = 24
#: 移动死区（逻辑像素）。雷神窗口在被拖动/动画期间矩形会有 1~2px 的抖动，
#: 逐帧照搬会让面板一直抖。位移不超过这个量就不动。
MOVE_TOLERANCE = 2


def choose_dock_side(cur_side, x_left, avail_left, switch_margin=SWITCH_MARGIN,
                     pinned=False):
    """换边决策（**纯函数**，不碰 Qt，所以能被单测逐条钉住）。

    为什么要抽出来：面板「乱跳」这个毛病在真机上靠肉眼很难复现（要把雷神慢慢
    拖到屏幕边缘才会触发），而它又是用户最直观的不满。所以决策必须与 Qt 解耦，
    好用断言覆盖「什么情况下换、什么情况下不换」。

    `pinned=True`（默认档）：一旦选定一侧就**永不自动换边**，只跟着雷神平移，
    超出屏幕就夹到屏幕边缘。这是用户要的「固定在加速器一角、拖雷神也不乱跑」：
    旧判据在雷神靠近屏幕边缘时会把面板甩到对面，拖动过程中看就是面板在两个
    位置之间跳。换边改由用户点「⇄ 换到另一侧」显式决定，位置因此可预测。

    未钉住时退回**滞回**判据：
      - 当前在左侧 → 只有左侧真的放不下（越界）才换右；
      - 当前在右侧 → 要左侧多让出 `switch_margin` 才换回左。
    没有这段时判据是二值的，窗口贴到边缘就会在左右两侧反复横跳。
    """
    if pinned and cur_side in ("left", "right"):
        return cur_side
    if cur_side not in ("left", "right"):
        # 首次定位：能放左边就放左边，没有偏好记忆可言
        return "left" if (avail_left is None or x_left >= avail_left) else "right"
    if cur_side == "left":
        if avail_left is not None and x_left < avail_left:
            return "right"
        return "left"
    # 当前在右侧：换回左侧的门槛更高（多要 switch_margin 的余量），形成死区
    if avail_left is None or x_left >= avail_left + switch_margin:
        return "left"
    return "right"


class GuardWindow(QWidget):
    """伴生面板。由 ui.app 驱动：apply_status / follow / show_notice。"""

    def __init__(self, callbacks: dict = None, logger=None, parent=None,
                 pinned: bool = True):
        super().__init__(parent)
        self.cb = callbacks or {}
        self.log = logger
        self._visible_wanted = True
        self._docked_side = None
        self._notice_key = None
        self._anchor = None
        #: 状态刷新节拍（秒），由 `set_poll_interval` 从配置灌进来
        self._poll_s = 1.0
        #: 是否钉住当前一侧（`ui.pin_side`）。钉住后拖动雷神不再自动换边。
        self._pinned = bool(pinned)
        #: 最近一次跟随用的物理矩形，供「换到另一侧」重新落位
        self._last_rect = None
        #: 本次落位强制使用的侧（用户点「换到另一侧」时短暂置位）
        self._force_side = None

        self.setObjectName("panel")
        self.setStyleSheet(theme.BASE_QSS)
        self.setWindowTitle("雷神时长保护")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        # 不抢焦点 + 半透明底（圆角外的部分要透出去）
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(PANEL_WIDTH)
        self.resize(PANEL_WIDTH, MIN_HEIGHT)

        self._build()

    # ------------------------------------------------------------ 构建
    def _build(self) -> None:
        # 外层留出投影用的透明边，内层 #card 才是真正的不透明底。
        # 见 ui/theme.py 里 QFrame#card 的注释 —— 顶层普通 QWidget 的样式表背景
        # 在 WA_TranslucentBackground 下不可靠地不绘制，实测 71% 像素 alpha=0。
        m = theme.SHADOW_MARGIN
        outer = QVBoxLayout(self)
        outer.setContentsMargins(m, m, m, m)
        outer.setSpacing(0)
        self.card = QFrame(self)
        self.card.setObjectName("card")
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.card)
        #: 投影缓存（见 `_render_shadow`）。不缓存的话每帧都要重画 10 圈描边。
        self._shadow_pm = None
        self._shadow_size = (0, 0)

        root = QVBoxLayout(self.card)
        root.setContentsMargins(*theme.PAD_CARD)
        root.setSpacing(10)
        self._root = root

        # ---------------- 头部：品牌标记 + 标题 + 状态药丸 ----------------
        head = QHBoxLayout()
        head.setSpacing(8)
        self.mark = QLabel()
        self.mark.setFixedSize(24, 24)
        self.mark.setPixmap(widgets.brand_pixmap(24))
        self.mark.setToolTip("雷神总时长保护")
        head.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_title = QLabel("雷神总时长保护")
        self.lbl_title.setObjectName("title")
        head.addWidget(self.lbl_title, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        self.chip = QLabel("—")
        self.chip.setObjectName("chip")
        self.chip.setStyleSheet(theme.chip_qss("UNKNOWN"))
        head.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)

        # ---------------- 状态主卡片（hero）----------------
        # 这是整个面板唯一的「主角」：状态文字最大、底色随状态走、左缘一道强调色。
        # 用户最需要一秒看懂的就是「现在时长在不在被消耗」。
        self.state_card = QFrame()
        self.state_card.setObjectName("stateCard")
        self.state_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.state_card.setStyleSheet(theme.state_card_qss("UNKNOWN"))
        sc = QVBoxLayout(self.state_card)
        sc.setContentsMargins(11, 9, 11, 10)
        sc.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(0)
        self.dot = widgets.StatusDot()
        top.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_state = QLabel("无法确认")
        self.lbl_state.setObjectName("state")
        top.addWidget(self.lbl_state, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        # 状态新鲜度：把「多久刷一次」写在界面上，别让人靠感觉猜延迟。
        self.lbl_fresh = QLabel("")
        self.lbl_fresh.setObjectName("meta")
        top.addWidget(self.lbl_fresh, 0, Qt.AlignmentFlag.AlignVCenter)
        sc.addLayout(top)
        # 大白话那一行用 `lbl_action`（上下文相关，信息量最大）。
        self.lbl_action = QLabel(theme.STATE_TIPS["unknown"])
        self.lbl_action.setObjectName("stateSub")
        self.lbl_action.setWordWrap(True)
        sc.addWidget(self.lbl_action)
        root.addWidget(self.state_card)

        # `lbl_hint` 是「通用解释」，与 hero 里那句高度重叠。两个都摆出来界面会变
        # 啰嗦，所以它保留控件（供无障碍/排障读取）但**不参与布局**，内容并入悬停提示。
        self.lbl_hint = QLabel(theme.STATE_TIPS["unknown"])
        self.lbl_hint.setObjectName("hint")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.hide()

        # ---------------- 保护状态（信任信号）----------------
        prow = QHBoxLayout()
        prow.setSpacing(6)
        self.icon_shield = QLabel()
        self.icon_shield.setFixedSize(13, 13)
        self.icon_shield.setPixmap(widgets.glyph_pixmap("shield", 13, theme.TEXT_MUTE))
        prow.addWidget(self.icon_shield, 0, Qt.AlignmentFlag.AlignTop)
        self.lbl_protect = QLabel(theme.protection_line({}))
        self.lbl_protect.setObjectName("trust")
        self.lbl_protect.setWordWrap(True)
        prow.addWidget(self.lbl_protect, 1)
        root.addLayout(prow)

        # ---------------- 元信息（弱化到几乎不抢注意力）----------------
        self.lbl_win = QLabel("未找到雷神窗口")
        self.lbl_win.setObjectName("meta")
        self.lbl_win.setWordWrap(True)
        root.addWidget(self.lbl_win)
        self._win_tip = ""

        # ---------------- 通知横幅 ----------------
        self.notice_frame = QFrame()
        self.notice_frame.setObjectName("notice")
        self.notice_frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        nf = QVBoxLayout(self.notice_frame)
        nf.setContentsMargins(10, 9, 10, 9)
        nf.setSpacing(6)
        self.notice_title = QLabel("")
        self.notice_title.setObjectName("noticeTitle")
        self.notice_title.setWordWrap(True)
        nf.addWidget(self.notice_title)
        self.notice_body = QLabel("")
        self.notice_body.setObjectName("noticeBody")
        self.notice_body.setWordWrap(True)
        nf.addWidget(self.notice_body)
        self.notice_actions = QHBoxLayout()
        self.notice_actions.setSpacing(6)
        self.notice_actions.addStretch(1)
        nf.addLayout(self.notice_actions)
        self.notice_frame.hide()
        root.addWidget(self.notice_frame)

        # ⚠️ 这里**不放伸缩项**。面板的高度是按内容贴合的（_refit 用 sizeHint），
        # 一旦多出一个伸缩项，只要窗口比内容高一丁点（实测多 28~38px），
        # 它就会把那点空间全吞掉，在两块内容**正中间**留出一条空档 ——
        # 看上去像"界面没画完"。让内容自己决定高度，就不会有这块空洞。
        #
        # ---------------- 主操作 ----------------
        self.btn_pause = QPushButton("立即暂停时长")
        self.btn_pause.setObjectName("primary")
        self.btn_pause.setMinimumHeight(36)
        self.btn_pause.setIcon(QIcon(widgets.glyph_pixmap("pause", 13, "#FFFFFF")))
        self.btn_pause.setIconSize(QSize(13, 13))
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.clicked.connect(lambda: self._call("pause"))
        root.addWidget(self.btn_pause)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_allow = QPushButton("允许本次关闭")
        self.btn_allow.setObjectName("danger")
        self.btn_allow.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_allow.clicked.connect(lambda: self._call("allow_close"))
        row.addWidget(self.btn_allow, 1)
        # 「重新检测状态」：状态读不出来时用户的第一反应按钮。
        # 它**永远可用**（连"没找到雷神"时也能按，按了会明确告诉你去开雷神），
        # 因为它的价值恰恰在于状态异常的那一刻。它做的事是
        # 「重新定位窗口 + 强制全量识别（不省 OCR）+ 如实说明结论与原因」，
        # 由引擎执行，见 ProtectionEngine.request_recheck。
        self.btn_recheck = QPushButton("重新检测状态")
        self.btn_recheck.setIcon(QIcon(widgets.glyph_pixmap("refresh", 13)))
        self.btn_recheck.setIconSize(QSize(13, 13))
        self.btn_recheck.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_recheck.setToolTip("重新定位雷神窗口并强制完整识别一次时长状态")
        self.btn_recheck.clicked.connect(lambda: self._call("recheck"))
        row.addWidget(self.btn_recheck, 1)
        root.addLayout(row)

        # ---------------- 次级操作区 ----------------
        # 整块收在一个容器里：出现**强提醒**时整块隐藏，把空间让给提示本身
        # （见 `_apply_trim`）。用户那一刻要读的是"为什么被拦"，不是这些开关。
        self.tools = QWidget()
        tl = QVBoxLayout(self.tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        sep = QFrame()
        sep.setObjectName("sep")
        sep.setFixedHeight(1)
        tl.addWidget(sep)

        frow = QHBoxLayout()
        frow.setSpacing(4)
        self.btn_inspector = QPushButton("界面诊断")
        self.btn_inspector.setObjectName("tool")
        self.btn_inspector.setIcon(QIcon(widgets.glyph_pixmap("wave", 12)))
        self.btn_inspector.setIconSize(QSize(12, 12))
        self.btn_inspector.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_inspector.setToolTip("生成一份完整诊断（窗口 / 控件树 / 截图 / OCR）")
        self.btn_inspector.clicked.connect(lambda: self._call("inspector"))
        frow.addWidget(self.btn_inspector)
        self.btn_side = QPushButton("换到另一侧")
        self.btn_side.setObjectName("tool")
        self.btn_side.setIcon(QIcon(widgets.glyph_pixmap("swap", 12)))
        self.btn_side.setIconSize(QSize(12, 12))
        self.btn_side.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_side.setToolTip("把面板换到雷神的另一边")
        self.btn_side.clicked.connect(lambda: self._call("switch_side"))
        frow.addWidget(self.btn_side)
        frow.addStretch(1)

        # 位置控制。用户反馈「固定在加速器一角、拖雷神也别乱跑」，
        # 所以默认钉住；想换边由这里显式点，而不是靠系统猜。
        self.chk_pin = widgets.Switch("钉住")
        self.chk_pin.setToolTip("钉住这一侧：拖动雷神时面板不换边、不乱跑")
        self.chk_pin.setChecked(self._pinned)
        self.chk_pin.toggled.connect(lambda on: self._call("set_pinned", bool(on)))
        frow.addWidget(self.chk_pin, 0, Qt.AlignmentFlag.AlignVCenter)
        tl.addLayout(frow)

        self.chk_disable = widgets.Switch("关闭保护")
        self.chk_disable.setToolTip("关闭后本工具不再拦截 ✕，雷神会直接关闭（不推荐）")
        self.chk_disable.toggled.connect(self._on_toggle)
        tl.addWidget(self.chk_disable)
        root.addWidget(self.tools)

        # 所有内容块纵向固定：面板高度是按内容算的（`_refit` 用 sizeHint），
        # 万一算出比内容略高的值，Qt 会把多出来的空间**按策略分配**给能长的控件 ——
        # 结果是状态卡片或按钮被莫名拉高。固定住它们，多出的空间就只会留在底部，
        # 表现为一点点下边距，不会破坏内部排版。
        for _w in (self.state_card, self.lbl_action, self.lbl_protect, self.lbl_win,
                   self.notice_frame, self.btn_pause, self.tools, self.chip,
                   self.mark, self.dot, self.icon_shield):
            try:
                _w.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Fixed)
            except Exception:                                     # noqa: BLE001
                pass

    def _set_pause_icon(self, on: bool) -> None:
        """主按钮的图标只在可用时挂上（禁用态浅底 + 白图标 = 糊一块白）。"""
        if getattr(self, "_pause_icon_on", None) == on:
            return
        self._pause_icon_on = on
        self.btn_pause.setIcon(QIcon(widgets.glyph_pixmap("pause", 13, "#FFFFFF"))
                               if on else QIcon())

    # ------------------------------------------------------------ 投影
    def _render_shadow(self) -> QPixmap:
        """把投影一次性画进缓存位图。

        为什么不用 QGraphicsDropShadowEffect：图形效果会让**整张卡片**（含所有子控件）
        每帧离屏渲染再模糊一遍，而状态点上的脉冲是 16fps 的 —— 那等于每秒做 16 次
        22px 高斯模糊，纯属白烧 CPU（用户明确提过后台占用）。这里改成"画一圈由内向外
        渐隐的描边"并缓存，只有面板尺寸变化时才重画。
        """
        m = theme.SHADOW_MARGIN
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r, g, b, a = theme.SHADOW_COLOR
        dy = theme.SHADOW_DY
        base = QRectF(m, m, w - 2 * m, h - 2 * m)
        for k in range(m, 0, -1):
            f = 1.0 - (k - 1) / float(m)
            alpha = int(round(a * f * f))
            if alpha <= 0:
                continue
            pen = QPen(QColor(r, g, b, alpha))
            pen.setWidth(1)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(
                QRectF(base.left() - k, base.top() - k + dy,
                       base.width() + 2 * k, base.height() + 2 * k),
                theme.R_CARD + k, theme.R_CARD + k)
        p.end()
        self._shadow_size = (w, h)
        return pm

    def paintEvent(self, ev) -> None:                             # noqa: N802
        """只画缓存下来的投影；卡片本身由子控件自绘。"""
        if (self._shadow_pm is None
                or self._shadow_size != (self.width(), self.height())):
            self._shadow_pm = self._render_shadow()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._shadow_pm)
        p.end()

    def _apply_trim(self, strong: bool, hide_primary: bool = False) -> None:
        """强提醒出现时收起次级区块，把空间让给提示本身。

        用户点 ✕ 被拦下那一刻，要读的是「为什么被拦、我该怎么办」；
        诊断入口、换边、开关这些都可以等。收起它们还能避免面板被撑得过高
        （面板高度上限受雷神窗口高度约束，撑太高会被裁）。

        `hide_primary`：当横幅自己就带「立即暂停」时，把页面级那颗主按钮也收起来 ——
        同一屏摆两个「立即暂停」会让人不知道点哪个。
        """
        for w in (self.tools, self.lbl_win):
            try:
                w.setVisible(not strong)
            except Exception:                                     # noqa: BLE001
                pass
        try:
            self.btn_pause.setVisible(not (strong and hide_primary))
        except Exception:                                         # noqa: BLE001
            pass

    # ------------------------------------------------------------ 事件
    def _call(self, name: str, *a) -> None:
        fn = self.cb.get(name)
        if fn:
            try:
                fn(*a)
            except Exception as e:
                if self.log:
                    self.log.exception("面板回调 %s 失败: %s", name, e)

    def _on_toggle(self, checked: bool) -> None:
        self._call("toggle", not checked)

    # ------------------------------------------------------------ 状态
    def apply_status(self, st: dict) -> None:
        """把引擎的 status 字典渲染到面板上（可能是 500ms 一次，必须轻量）。

        性能约束：这个函数每轮 tick 都会跑。凡是**会触发样式重算或重排**的动作
        （setStyleSheet / setPixmap / setVisible）都必须先判断「值真的变了吗」——
        否则一次 500ms 的刷新会变成整棵子树的 polish + repaint。
        """
        st = st or {}
        state = str(st.get("state") or "UNKNOWN")
        label, fg, _ = theme.state_style(state)

        # ---- 时长状态：主卡片 + 状态点（只在状态变化时重写样式）----
        if state != getattr(self, "_styled_state", None):
            self._styled_state = state
            self.state_card.setStyleSheet(theme.state_card_qss(state))
            self.lbl_state.setText(label)
            self.lbl_state.setStyleSheet(f"color: {fg};")
        # ---- 标题右侧那颗药丸说的是**另一件事**：关闭保护起没起作用。
        #      原先它也显示时长状态，于是「已暂停」在同一屏出现两次 —— 既冗余，
        #      又让人以为保护生效与否和时长状态是同一件事。见 theme.protection_chip。
        chip_text, chip_key = theme.protection_chip(st)
        if chip_key != getattr(self, "_chip_key", None) or chip_text != self.chip.text():
            self._chip_key = chip_key
            self.chip.setText(chip_text)
            self.chip.setStyleSheet(theme.chip_qss(chip_key))
        self.dot.set_state(state, active=bool(st.get("leigod_found", True)))
        self.lbl_hint.setText(theme.STATE_TIPS.get(state.lower(),
                                                   theme.STATE_TIPS["unknown"]))
        self.lbl_action.setText(theme.status_line(state, st))

        # ---- 窗口信息：只留人看得懂的，HWND/DPI/类名收进悬停提示 ----
        win = st.get("window")
        if win:
            size = win.get("size") or (0, 0)
            tray = " · 已最小化到托盘" if win.get("minimized_to_tray") else ""
            line = f"雷神窗口 {size[0]}×{size[1]}{tray}"
            self._win_tip = (f"HWND {win.get('hwnd_hex')} · {win.get('dpi')}dpi"
                             f" · class {win.get('class_name')}"
                             f" · 识别方式 {win.get('match')}")
            self.btn_pause.setEnabled(state == "RUNNING" and bool(st.get("leigod_found")))
        else:
            line = "未找到雷神窗口"
            self._win_tip = ""
            self.btn_pause.setEnabled(False)
        # 只有可用时才挂图标：禁用态是浅底，白色图标在上面等于糊了一块白，
        # 反而比"没有图标"更难看（第一版就踩了这个）。
        self._set_pause_icon(self.btn_pause.isEnabled())
        if self.lbl_win.text() != line:
            self.lbl_win.setText(line)
            self.lbl_win.setToolTip(self._win_tip)

        # ---- 保护状态：颜色按「真的锁住了吗」而不是按「开关打开了吗」 ----
        text = theme.protection_line(st)
        if st.get("pause_failed"):
            text += f"（已尝试 {st.get('pause_fails', 0)} 次）"
        tone = theme.protection_tone(st)
        color = theme.TONE_COLOR.get(tone, theme.TEXT_MUTE)
        if self.lbl_protect.text() != text:
            self.lbl_protect.setText(text)
            self.lbl_protect.setStyleSheet(f"color: {color};")
        if tone != getattr(self, "_tone_shown", None):
            self._tone_shown = tone
            self.icon_shield.setPixmap(widgets.glyph_pixmap(
                "shield-off" if tone in ("off", "warn") else "shield", 13, color))

        # ---- 悬停提示：技术细节集中到一处，界面上不摆 ----
        detail = st.get("state_detail") or ""
        evidence = st.get("protection_detail") or ""
        self.lbl_action.setToolTip(detail)
        self.setToolTip("\n".join(x for x in (self.lbl_hint.text(), self._win_tip,
                                             detail, evidence) if x))

        # 保护开关状态与界面保持同步（用户可能在托盘里切过）
        want = not bool(st.get("protect_enabled", True))
        if self.chk_disable.isChecked() != want:
            self.chk_disable.blockSignals(True)
            self.chk_disable.setChecked(want)
            self.chk_disable.blockSignals(False)
        self.btn_allow.setEnabled(bool(st.get("leigod_found")))

        # 状态新鲜度：只在状态真的变了才重置计时，否则会一直显示「刚刚」而失去意义。
        try:
            if state != getattr(self, "_last_shown_state", None):
                self._last_shown_state = state
                self._state_since = time.time()
            since = time.time() - getattr(self, "_state_since", time.time())
            # 节拍已经压到 0.5s，此时再显示「0s 前确认」既没信息量又显得卡：
            # 一秒以内统一说「刚刚确认」，超过一秒才报具体秒数。
            fresh = "刚刚确认" if since < 1.0 else f"{int(since)}s 前确认"
            if self.lbl_fresh.text() != fresh:
                self.lbl_fresh.setText(fresh)
            # 节拍用**实时有效值**（status 里的 poll_ms），不是配置基准值 ——
            # 自适应轮询下两者本来就不同，显示基准值等于骗人。
            eff_ms = st.get("poll_ms")
            eff = (max(0.1, float(eff_ms) / 1000.0) if eff_ms
                   else max(0.1, float(self._poll_s)))
            tip = f"每 {eff:.1f} 秒重新识别一次状态"
            if self.lbl_fresh.toolTip() != tip:
                self.lbl_fresh.setToolTip(tip)
        except Exception:                                      # noqa: BLE001
            pass

    # ------------------------------------------------------------ 通知
    def show_notice(self, notice: Notice) -> None:
        key = (notice.title, notice.message)
        self._notice_key = key
        self.notice_frame.setObjectName("notice" if notice.strong else "noticeInfo")
        self.notice_frame.setStyleSheet(theme.notice_qss(bool(notice.strong)))
        self.notice_title.setText(notice.title)
        self.notice_title.setStyleSheet(
            f"color: {theme.C_DANGER};" if notice.strong else f"color: {theme.TEXT};")
        self.notice_body.setText(notice.message)

        while self.notice_actions.count() > 1:
            item = self.notice_actions.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for text, action in (notice.actions or []):
            b = QPushButton(text)
            b.setObjectName("primary" if action != ACTION_OK else "noticeGhost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if action != ACTION_OK:
                # 横幅里的主动作带图标，让「立即暂停」一眼可辨
                b.setIcon(QIcon(widgets.glyph_pixmap("pause", 12, "#FFFFFF")))
                b.setIconSize(QSize(12, 12))
            b.clicked.connect(lambda _=False, a=action: self._on_notice_action(a))
            self.notice_actions.insertWidget(self.notice_actions.count() - 1, b)

        self.notice_frame.show()
        # 横幅自带「立即暂停」时，页面级主按钮收起（避免两个同名按钮同时出现）
        self._apply_trim(
            bool(notice.strong),
            hide_primary=any(str(a[1]) == ACTION_PAUSE_NOW
                             for a in (notice.actions or [])))
        if not self._visible_wanted:
            self.show_panel()
        QTimer.singleShot(0, self._refit)      # 内容变高了，重新贴合
        if notice.timeout and notice.timeout > 0:
            QTimer.singleShot(int(notice.timeout * 1000),
                              lambda k=key: self._auto_clear(k))

    def _auto_clear(self, key) -> None:
        if self._notice_key == key:
            self.clear_notice()

    def clear_notice(self) -> None:
        self._notice_key = None
        self.notice_frame.hide()
        self._apply_trim(False)
        QTimer.singleShot(0, self._refit)

    def _on_notice_action(self, action: str) -> None:
        mapping = {ACTION_PAUSE_NOW: "pause", ACTION_ALLOW_CLOSE: "allow_close",
                   ACTION_RUN_INSPECTOR: "inspector", ACTION_OK: None,
                   ACTION_CANCEL_QUIT: "cancel_quit",
                   ACTION_QUIT_ANYWAY: "quit_anyway"}
        name = mapping.get(action)
        if name:
            self._call(name)
        self.clear_notice()

    def set_poll_interval(self, ms) -> None:
        """告诉面板状态刷新节拍（毫秒），用于显示「每 x.xs 刷新一次」。"""
        try:
            self._poll_s = max(0.1, int(ms) / 1000.0)
        except (TypeError, ValueError):
            self._poll_s = 1.0

    # ------------------------------------------------------------ 跟随
    def _screen_and_dpr(self, px: int, py: int):
        """按物理像素点找它所在的屏幕，并返回该屏的 devicePixelRatio。"""
        for s in QGuiApplication.screens():
            dpr = float(s.devicePixelRatio() or 1.0)
            g = s.geometry()
            lx, ly = px / dpr, py / dpr
            if (g.left() - 4 <= lx <= g.right() + 4) and (g.top() - 4 <= ly <= g.bottom() + 4):
                return s, dpr
        s = QGuiApplication.primaryScreen()
        return s, (float(s.devicePixelRatio() or 1.0) if s else 1.0)

    def follow(self, phys_rect) -> None:
        """按雷神窗口的**物理像素**矩形重新定位面板。

        phys_rect 为 None / 非法时把面板藏起来（不能让用户对着空气点）。

        高度不跟着雷神窗口拉满：那样面板中间会空一大片。
        改为「内容需要多高就多高」，上限不超过雷神窗口高度。
        """
        try:
            if not phys_rect:
                if self._visible_wanted:
                    self.hide()
                    self._docked_side = None
                self._anchor = None
                return
            l, t, r, b = [int(v) for v in phys_rect]
            if r <= l or b <= t or l < -20000:
                if self._visible_wanted:
                    self.hide()
                return

            scr, dpr = self._screen_and_dpr(l, t)
            avail = scr.availableGeometry() if scr else None
            gl, gt, gr, gb = l / dpr, t / dpr, r / dpr, b / dpr
            # 投影留边补偿：窗口比卡片大 SHADOW_MARGIN 一圈，落位时必须减掉，
            # 否则「卡片到雷神窗口」的视觉间距会凭空多出 M 像素（改留边就会漂）。
            m = theme.SHADOW_MARGIN
            max_h = int(gb - gt)
            y = int(gt) - m
            x_left = int(gl - GAP - PANEL_WIDTH + m)
            x_right = int(gr + GAP - m)
            # 换边走纯函数（默认「钉住」不换边），逻辑与 Qt 解耦，便于单测钉住
            # 「不乱跑」。`_force_side` 只在用户点「换到另一侧」的瞬间生效。
            side = self._force_side or choose_dock_side(
                self._docked_side, x_left,
                avail.left() if avail is not None else None,
                pinned=self._pinned)
            x = x_left if side == "left" else x_right
            if avail is not None:
                # 夹的是**卡片**（窗口向内缩了 m），所以边界也要跟着缩
                lo_x = avail.left() - m
                hi_x = max(lo_x, avail.right() - PANEL_WIDTH + m)
                x = min(max(x, lo_x), hi_x)
                lo_y = avail.top() - m
                hi_y = max(lo_y, avail.bottom() - MIN_HEIGHT + m)
                y = min(max(y, lo_y), hi_y)

            self._docked_side = side
            self._anchor = (x, y, max_h)
            self._last_rect = (l, t, r, b)
            self._refit()
        except Exception as e:
            if self.log:
                self.log.exception("面板定位失败: %s", e)

    # ------------------------------------------------------------ 位置控制
    def set_pinned(self, on: bool) -> None:
        """切换「钉住这一侧」。钉住后拖动雷神不再自动换边。"""
        self._pinned = bool(on)
        if self.log:
            self.log.info("面板停靠：%s", "钉住当前一侧" if self._pinned else "自动换边")
        if self._last_rect:
            self.follow(self._last_rect)

    def switch_side(self) -> None:
        """把面板换到雷神的另一侧（用户显式要求，不受「钉住」限制）。

        这条路径是「钉住」的必要配套：钉住拿掉了系统自动换边，就必须给用户
        一个手动出口，否则雷神挪到屏幕另一头时面板会一直被夹在屏幕边缘。
        """
        cur = self._docked_side or "left"
        self._force_side = "right" if cur == "left" else "left"
        try:
            self.follow(self._last_rect or self._phys_rect_from_anchor())
        finally:
            self._force_side = None

    def _phys_rect_from_anchor(self):
        """没有记录过物理矩形时，用当前几何反推一个，保证换边至少能动。"""
        g = self.geometry().getRect()
        dpr = 1.0
        try:
            for s in QGuiApplication.screens():
                if s.geometry().contains(self.pos()):
                    dpr = float(s.devicePixelRatio() or 1.0)
                    break
        except Exception:
            pass
        return (int(g[0] * dpr), int(g[1] * dpr),
                int((g[0] + g[2]) * dpr), int((g[1] + g[3]) * dpr))

    @property
    def pinned(self) -> bool:
        return self._pinned

    @property
    def docked_side(self) -> str:
        return self._docked_side

    def _refit(self) -> None:
        """按当前内容重新计算高度并落位（通知出现/消失时会变高变矮）。"""
        if not self._anchor:
            return
        x, y, max_h = self._anchor
        want_h = max(MIN_HEIGHT, int(self.sizeHint().height()))
        h = int(min(want_h, max(MIN_HEIGHT, max_h)))
        cur = self.geometry().getRect()
        # 移动死区：1~2px 的抖动照搬会让面板看起来一直在跳，位移太小就不动。
        # 高度必须严格相等才允许跳过（内容变高变矮一定要生效）。
        if not (cur[2] == PANEL_WIDTH and cur[3] == h
                and abs(cur[0] - x) <= MOVE_TOLERANCE
                and abs(cur[1] - y) <= MOVE_TOLERANCE):
            self.setGeometry(x, y, PANEL_WIDTH, h)
        if self._visible_wanted and not self.isVisible():
            self.show()

    # ------------------------------------------------------------ 显隐
    def show_panel(self) -> None:
        self._visible_wanted = True
        self._refit()
        self.show()
        self.raise_()

    def hide_panel(self) -> None:
        self._visible_wanted = False
        self.hide()

    def toggle_panel(self) -> None:
        if self._visible_wanted and self.isVisible():
            self.hide_panel()
        else:
            self.show_panel()

    @property
    def visible_wanted(self) -> bool:
        return self._visible_wanted

    def closeEvent(self, ev) -> None:
        """关掉面板 ≠ 退出程序（退出只能走托盘菜单），否则用户会以为保护没了。"""
        ev.ignore()
        self.hide_panel()
