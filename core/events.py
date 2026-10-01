"""事件与通知载荷。

把「引擎产生什么」与「界面怎么展现」分开：引擎只产出这些数据结构，
UI 层（Qt）或测试用的记录型 sink 去消费它们。这样引擎可以脱离 Qt 测试。
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field


class EventKind(enum.Enum):
    STARTED = "STARTED"
    STOPPED = "STOPPED"
    LEIGOD_FOUND = "LEIGOD_FOUND"
    LEIGOD_GONE = "LEIGOD_GONE"
    STATE_CHANGED = "STATE_CHANGED"
    CLOSE_REQUESTED = "CLOSE_REQUESTED"
    CLOSE_BLOCKED = "CLOSE_BLOCKED"
    CLOSE_ALLOWED = "CLOSE_ALLOWED"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSE_SUCCEEDED = "PAUSE_SUCCEEDED"
    PAUSE_FAILED = "PAUSE_FAILED"
    GAME_EXITED = "GAME_EXITED"
    GAME_RESTARTED = "GAME_RESTARTED"
    WINDOW_CLOSED_UNPROTECTED = "WINDOW_CLOSED_UNPROTECTED"
    NOTICE = "NOTICE"
    STATUS = "STATUS"


LEVEL_INFO = "INFO"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"


@dataclass
class Event:
    kind: EventKind
    message: str = ""
    level: str = LEVEL_INFO
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def line(self) -> str:
        return f"[{self.level}] {self.kind.value} {self.message}".rstrip()


@dataclass
class Notice:
    """需要用户看到的提示（可带按钮）。"""
    title: str
    message: str
    actions: list = field(default_factory=list)   # [("按钮文本", "动作ID"), ...]
    strong: bool = False                          # 强提醒：不自动消失
    timeout: int = 15


#: 通知里可用的动作 ID
ACTION_PAUSE_NOW = "pause_now"
ACTION_ALLOW_CLOSE = "allow_close"
ACTION_SNOOZE = "snooze"
ACTION_OPEN_MAIN = "open_main"
ACTION_RUN_INSPECTOR = "run_inspector"
ACTION_OK = "ok"
#: 退出保护：确认暂停没成功时，用户仍可显式选择「就这样退出」。
#: 它是**唯一**允许把雷神留在计时状态的出口，必须由用户点，不能由代码替用户决定。
ACTION_QUIT_ANYWAY = "quit_anyway"
ACTION_CANCEL_QUIT = "cancel_quit"
