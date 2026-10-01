"""面板用的自绘控件与图标（不依赖任何外部图片资源）。

## 为什么全部自己画

这个程序要**单文件分发 + 免安装**，多带一个图标资源目录就多一处「可能找不到」
的风险（本项目已经在 DLL/模型文件上吃过两次亏）。所有图形都用 QPainter 现画，
按 devicePixelRatio 放大绘制再缩放显示 —— 在 125% / 150% 缩放的屏幕上依旧锐利。

## 三个控件的职责

  · `brand_pixmap()`  —— 品牌标记：圆角方牌 + 渐变 + 盾牌里的暂停条
  · `glyph_pixmap()`  —— 按钮上的小图标（线性风格，统一线宽与圆头）
  · `StatusDot`       —— 状态圆点，RUNNING 时带一圈扩散脉冲（「它正在工作」的直观感）
  · `Switch`          —— 开关型的 QCheckBox（保留 isChecked/setChecked/toggled 语义）
"""
from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt,
                            QTimer, Property)
from PySide6.QtGui import (QColor, QFontMetrics, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QCheckBox, QWidget

from . import theme


# ------------------------------------------------------------------ 基础
def _canvas(size: int, dpr: float = 2.0) -> tuple:
    """建一张 (size×dpr) 的画布，返回 (pixmap, painter)。调用方负责 painter.end()。"""
    d = max(1.0, float(dpr))
    pm = QPixmap(max(1, int(round(size * d))), max(1, int(round(size * d))))
    pm.setDevicePixelRatio(d)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    return pm, p


def _pen(color, width: float, cap=Qt.PenCapStyle.RoundCap) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(cap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def screen_dpr() -> float:
    """当前屏幕的缩放比（拿不到就按 2 倍画，宁锐不糊）。"""
    try:
        from PySide6.QtGui import QGuiApplication
        s = QGuiApplication.primaryScreen()
        return float(s.devicePixelRatio() or 1.0) if s else 2.0
    except Exception:                                             # noqa: BLE001
        return 2.0


# ------------------------------------------------------------------ 品牌标记
def brand_pixmap(size: int = 26) -> QPixmap:
    """品牌标记：圆角方牌 + 强调色渐变 + 白色盾牌轮廓，盾内两道暂停竖条。

    语义对齐产品本身 —— 「保护」+「暂停」各占一半，不是随便一个装饰图形。
    """
    pm, p = _canvas(size, screen_dpr())
    s = float(size)
    r = s * 0.29
    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, s - 1.0, s - 1.0), r, r)
    g = QLinearGradient(0, 0, 0, s)
    g.setColorAt(0.0, QColor(theme.ACCENT_HI))
    g.setColorAt(1.0, QColor(theme.ACCENT_LO))
    p.fillPath(path, g)

    # 盾牌轮廓（高度占 62%，居中偏上）
    w = s * 0.40
    h = s * 0.50
    x = (s - w) / 2.0
    y = (s - h) / 2.0 + s * 0.01
    shield = QPainterPath()
    shield.moveTo(x + w / 2.0, y)
    shield.cubicTo(x + w * 0.88, y + h * 0.10, x + w, y + h * 0.26, x + w, y + h * 0.42)
    shield.cubicTo(x + w, y + h * 0.76, x + w * 0.62, y + h * 0.94, x + w / 2.0, y + h)
    shield.cubicTo(x + w * 0.38, y + h * 0.94, x, y + h * 0.76, x, y + h * 0.42)
    shield.cubicTo(x, y + h * 0.26, x + w * 0.12, y + h * 0.10, x + w / 2.0, y)
    p.setPen(_pen(QColor(255, 255, 255, 235), max(1.2, s * 0.062)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(shield)

    # 盾内两道暂停竖条
    bar_w = s * 0.055
    bar_h = s * 0.155
    gap = s * 0.075
    cy = y + h * 0.44
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 242))
    for dx in (-gap / 2.0, gap / 2.0):
        p.drawRoundedRect(QRectF(s / 2.0 + dx - bar_w / 2.0, cy - bar_h / 2.0,
                                 bar_w, bar_h), bar_w / 2.0, bar_w / 2.0)
    p.end()
    return pm


# ------------------------------------------------------------------ 图标
def glyph_pixmap(kind: str, size: int = 14, color: str = theme.TEXT_SUB) -> QPixmap:
    """线性风格小图标。统一线宽 = size*0.11、圆头、圆角连接。

    只做界面真正用得到的那几个；不做通用图标库（每个都必须画得对，
    宁可少而准，也不要多而糙）。
    """
    pm, p = _canvas(size, screen_dpr())
    s = float(size)
    lw = max(1.1, s * 0.115)
    p.setPen(_pen(color, lw))
    p.setBrush(Qt.BrushStyle.NoBrush)

    if kind == "pause":                      # 两道竖条
        bw = s * 0.16
        bh = s * 0.44
        y = (s - bh) / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        for cx in (s * 0.36, s * 0.64):
            p.drawRoundedRect(QRectF(cx - bw / 2.0, y, bw, bh), bw / 2.0, bw / 2.0)

    elif kind == "refresh":                  # 圆弧 + 箭头
        m = s * 0.18
        rect = QRectF(m, m, s - 2 * m, s - 2 * m)
        p.drawArc(rect, int(58 * 16), int(252 * 16))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        head = QPainterPath()
        cx, cy = s * 0.80, s * 0.30
        head.moveTo(cx + s * 0.10, cy - s * 0.02)
        head.lineTo(cx - s * 0.06, cy - s * 0.10)
        head.lineTo(cx - s * 0.01, cy + s * 0.11)
        head.closeSubpath()
        p.drawPath(head)

    elif kind == "wave":                     # 三根高低柱（诊断/波形）
        p.setPen(_pen(color, lw * 0.9))
        for i, hh in enumerate((0.26, 0.52, 0.36)):
            x = s * (0.24 + i * 0.26)
            p.drawLine(int(x), int(s * (0.5 + hh / 2.0)), int(x), int(s * (0.5 - hh / 2.0)))

    elif kind == "swap":                     # 上下两条反向箭头（换边）
        m = s * 0.16
        p.setPen(_pen(color, lw))
        y1, y2 = s * 0.36, s * 0.64
        p.drawLine(int(m), int(y1), int(s - m), int(y1))
        p.drawLine(int(s - m), int(y2), int(m), int(y2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        for (ax, ay, sx, sy) in ((s - m, y1, -1, 0), (m, y2, 1, 0)):
            tri = QPainterPath()
            tri.moveTo(ax + sx * s * 0.09, ay)
            tri.lineTo(ax - sx * s * 0.03, ay - s * 0.10)
            tri.lineTo(ax - sx * s * 0.03, ay + s * 0.10)
            tri.closeSubpath()
            p.drawPath(tri)

    elif kind in ("shield", "shield-off"):   # 保护状态用
        # 占满画框而不是留一大圈空白：13px 下如果图形只占 60%，它与右边文字之间
        # 会出现一段"看起来像多余间距"的空白（第一版就是这样，视觉上很松）。
        w, h = s * 0.74, s * 0.88
        x, y = (s - w) / 2.0, (s - h) / 2.0
        sh = QPainterPath()
        sh.moveTo(x + w / 2.0, y)
        sh.cubicTo(x + w * 0.90, y + h * 0.10, x + w, y + h * 0.24, x + w, y + h * 0.42)
        sh.cubicTo(x + w, y + h * 0.76, x + w * 0.62, y + h * 0.94, x + w / 2.0, y + h)
        sh.cubicTo(x + w * 0.38, y + h * 0.94, x, y + h * 0.76, x, y + h * 0.42)
        sh.cubicTo(x, y + h * 0.24, x + w * 0.10, y + h * 0.10, x + w / 2.0, y)
        p.drawPath(sh)
        if kind == "shield-off":             # 一道斜杠表示「没在保护」
            p.drawLine(int(x + w * 0.12), int(y + h * 0.86),
                       int(x + w * 0.88), int(y + h * 0.14))

    p.end()
    return pm


def tray_pixmap(size: int, state_color: str, found: bool = True) -> QPixmap:
    """托盘图标：与面板顶部同一个「圆角方牌」，右下角加一颗状态角标。

    为什么要统一：托盘图标和面板是同一个产品的两个入口，长得不一样会让人觉得
    是两套东西。角标用状态色（红=计时中/绿=已暂停/琥珀=无法确认），
    外面套一圈白，保证在 16px 的托盘里也能从深色任务栏上分辨出来。
    """
    d = screen_dpr()
    pm, p = _canvas(size, max(1.0, d))
    s0 = float(size)
    r = s0 * 0.30
    path = QPainterPath()
    path.addRoundedRect(QRectF(0.5, 0.5, s0 - 1.0, s0 - 1.0), r, r)
    g = QLinearGradient(0, 0, 0, s0)
    if found:
        g.setColorAt(0.0, QColor(theme.ACCENT_HI))
        g.setColorAt(1.0, QColor(theme.ACCENT_LO))
    else:                                   # 没找到雷神 → 灰牌，一眼看出"没接上"
        g.setColorAt(0.0, QColor("#B7BFCC"))
        g.setColorAt(1.0, QColor("#98A2B3"))
    p.fillPath(path, g)

    # 大尺寸才画盾牌：16px 下它会糊成一团，反而是噪声
    if size >= 24:
        w, h = s0 * 0.36, s0 * 0.46
        x, y = (s0 - w) / 2.0, (s0 - h) / 2.0 - s0 * 0.03
        sh = QPainterPath()
        sh.moveTo(x + w / 2.0, y)
        sh.cubicTo(x + w * 0.88, y + h * 0.10, x + w, y + h * 0.26, x + w, y + h * 0.42)
        sh.cubicTo(x + w, y + h * 0.76, x + w * 0.62, y + h * 0.94, x + w / 2.0, y + h)
        sh.cubicTo(x + w * 0.38, y + h * 0.94, x, y + h * 0.76, x, y + h * 0.42)
        sh.cubicTo(x, y + h * 0.26, x + w * 0.12, y + h * 0.10, x + w / 2.0, y)
        p.setPen(_pen(QColor(255, 255, 255, 230), max(1.0, s0 * 0.055)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(sh)

    # 状态角标
    dot = s0 * 0.42
    cx, cy = s0 - dot / 2.0 - s0 * 0.02, s0 - dot / 2.0 - s0 * 0.02
    p.setPen(QPen(QColor(255, 255, 255, 235), max(1.0, s0 * 0.055)))
    p.setBrush(QColor(state_color))
    p.drawEllipse(QRectF(cx - dot / 2.0, cy - dot / 2.0, dot, dot))
    p.end()
    return pm


# ------------------------------------------------------------------ 状态点
class StatusDot(QWidget):
    """状态圆点。RUNNING 时外面扩散一圈脉冲，表示「正在持续监视」。

    脉冲只在**可见且运行中**时才跑（60ms 一帧，控件只有 18×18 像素 ——
    对 CPU 的影响可以忽略；用户很在意后台占用，所以定时器必须随可见性开关）。
    """

    SIZE = 20
    CORE = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._color = QColor(theme.C_UNKNOWN)
        self._active = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

    def set_state(self, state: str, active: bool = True) -> None:
        t = theme.state_tokens(state)
        new_color = QColor(t["dot"])
        # 只在真的需要时才启用脉冲，避免「暂停了还在闪」这种噪声
        self._active = bool(active) and str(state).upper() == "RUNNING"
        if new_color != self._color:
            self._color = new_color
        self._sync_timer()
        self.update()

    def _sync_timer(self) -> None:
        want = self._active and self.isVisible()
        if want and not self._timer.isActive():
            self._timer.start()
        elif not want and self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.09) % 1.0
        self.update()

    def showEvent(self, ev) -> None:                              # noqa: N802
        super().showEvent(ev)
        self._sync_timer()

    def hideEvent(self, ev) -> None:                              # noqa: N802
        super().hideEvent(ev)
        self._sync_timer()

    def paintEvent(self, ev) -> None:                             # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = self.SIZE / 2.0
        if self._active:
            r = self.CORE / 2.0 + self._phase * (self.SIZE / 2.0 - self.CORE / 2.0 - 0.5)
            alpha = int(120 * (1.0 - self._phase))
            col = QColor(self._color)
            col.setAlpha(max(0, alpha))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QRectF(c - r, c - r, r * 2, r * 2))
        p.setBrush(self._color)
        p.setPen(Qt.PenStyle.NoPen)
        r0 = self.CORE / 2.0
        p.drawEllipse(QRectF(c - r0, c - r0, r0 * 2, r0 * 2))
        p.end()


# ------------------------------------------------------------------ 开关
class Switch(QCheckBox):
    """开关样式的复选框（药丸 + 滑块 + 位置动画）。

    为什么继承 QCheckBox 而不是自己写一个：`isChecked/setChecked/toggled/click`
    这套语义已经被界面与测试依赖，换成自定义控件会连带改掉所有调用点与用例。
    这里只接管**绘制**，行为完全沿用 QCheckBox。
    """

    W = 34
    H = 20
    KNOB = 15
    GAP = 9

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 1.0 if self.isChecked() else 0.0
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    # 供 QPropertyAnimation 驱动的属性
    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, v: float) -> None:
        self._pos = float(v)
        self.update()

    knobPos = Property(float, _get_knob, _set_knob)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:                       # noqa: N802
        super().setChecked(on)
        # 程序化设置时不要动画（避免"面板刚出来开关自己滑过去"的怪异感）
        self._anim.stop()
        self._set_knob(1.0 if on else 0.0)

    def sizeHint(self) -> QSize:                                  # noqa: N802
        fm = QFontMetrics(self.font())
        w = self.W + self.GAP + (fm.horizontalAdvance(self.text()) if self.text() else 0)
        return QSize(w + 2, max(self.H, fm.height()))

    def minimumSizeHint(self) -> QSize:                           # noqa: N802
        return self.sizeHint()

    def paintEvent(self, ev) -> None:                             # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        on = self.isChecked()
        enabled = self.isEnabled()
        y = (self.height() - self.H) / 2.0
        track = QRectF(0.5, y + 0.5, self.W - 1.0, self.H - 1.0)
        radius = track.height() / 2.0

        # 轨道：开 = 强调色渐变；关 = 中性灰；禁用 = 整体降饱和
        if on:
            g = QLinearGradient(0, track.top(), 0, track.bottom())
            if enabled:
                g.setColorAt(0.0, QColor(theme.ACCENT_HI))
                g.setColorAt(1.0, QColor(theme.ACCENT_LO))
            else:
                g.setColorAt(0.0, QColor("#C9D6F2"))
                g.setColorAt(1.0, QColor("#BFCEF0"))
            p.setBrush(g)
            p.setPen(Qt.PenStyle.NoPen)
        else:
            p.setBrush(QColor("#E4E8EF" if enabled else "#EFF2F6"))
            p.setPen(QPen(QColor("#D5DBE4" if enabled else "#E7EBF1"), 1))
        p.drawRoundedRect(track, radius, radius)

        # 滑块：带一圈极浅描边，避免白色滑块在浅底上「糊掉」
        pad = 2.5
        kd = self.H - pad * 2
        kx = track.left() + pad + self._pos * (self.W - kd - pad * 2)
        p.setPen(QPen(QColor(0, 0, 0, 22), 1))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(kx, track.top() + pad, kd, kd))

        if self.text():
            fm = QFontMetrics(self.font())
            c = QColor(theme.TEXT_SUB if enabled else theme.TEXT_MUTE)
            p.setPen(c)
            p.drawText(self.W + self.GAP, 0, self.width() - self.W - self.GAP,
                       self.height(), int(Qt.AlignmentFlag.AlignVCenter
                                          | Qt.AlignmentFlag.AlignLeft),
                       fm.elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                     max(10, self.width() - self.W - self.GAP)))
        p.end()
