"""通知路由：引擎的 Notice → 面板横幅 + 托盘气泡（规格书 §十八）。

为什么要去重：识别抖动或周期性重申拦截状态时会重复上报同一条消息，
不去重的话用户会被同一条提示反复骚扰（旧实现就有这个问题）。
规则：同一条 (标题, 正文) 在冷却时间内只提示一次；强提醒（strong）不参与去重。
"""
from __future__ import annotations

import time

from core.events import Notice


class Notifier:
    def __init__(self, panel=None, tray=None, logger=None, cooldown: float = 25.0):
        self.panel = panel
        self.tray = tray
        self.log = logger
        self.cooldown = cooldown
        self._last = {}

    def handle(self, notice: Notice) -> None:
        if notice is None:
            return
        key = (notice.title or "", notice.message or "")
        now = time.time()
        if not notice.strong:
            prev = self._last.get(key)
            if prev is not None and (now - prev) < self.cooldown:
                if self.log:
                    self.log.info("通知去重（%.0fs 内已提示过）：%s", now - prev, notice.title)
                return
        self._last[key] = now

        if self.panel is not None:
            try:
                self.panel.show_notice(notice)
            except Exception as e:
                if self.log:
                    self.log.exception("面板通知失败: %s", e)
        if self.tray is not None:
            try:
                self.tray.notify(notice.title, notice.message)
            except Exception as e:
                if self.log:
                    self.log.exception("托盘通知失败: %s", e)

    def simple(self, title: str, message: str) -> None:
        self.handle(Notice(title=title, message=message))
