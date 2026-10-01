"""三态状态机与关闭策略。

本文件是整个项目的「法律条文」，只依赖标准库，便于单元测试。

核心不变量（规格书 §十六 / §三十四）：
  1. 只有「重新检测到 PAUSED」才算暂停成功；点击成功 ≠ 暂停成功。
  2. UNKNOWN 永不等于 PAUSED。UNKNOWN 一律按不安全处理（Fail Safe）。
  3. 任何无法证明「已暂停」的情况都必须阻止关闭。
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field


class DurationState(enum.Enum):
    RUNNING = "RUNNING"    # 总时长正在消耗（按钮=「暂停时长」）
    PAUSED = "PAUSED"      # 总时长已暂停（按钮=「开启时长」）
    UNKNOWN = "UNKNOWN"    # 无法证明处于以上任一状态

    @property
    def safe_to_close(self) -> bool:
        """只有 PAUSED 允许关闭。"""
        return self is DurationState.PAUSED


class CloseDecision(enum.Enum):
    ALLOW = "ALLOW"                          # 允许关闭雷神
    BLOCK_RUNNING = "BLOCK_RUNNING"          # 时长在跑：先暂停（暂不放行）
    BLOCK_UNKNOWN = "BLOCK_UNKNOWN"          # 状态未知：直接禁止关闭
    BLOCK_PAUSE_FAILED = "BLOCK_PAUSE_FAILED"  # 暂停失败：禁止关闭
    DISABLED = "DISABLED"                    # 用户关闭了保护功能

    @property
    def blocked(self) -> bool:
        return self is not CloseDecision.ALLOW and self is not CloseDecision.DISABLED


#: 阻止关闭时给用户看的提示（直接取自规格书 §十四 情况3 的措辞要求）
BLOCK_MESSAGES = {
    CloseDecision.BLOCK_UNKNOWN: (
        "无法确认雷神总时长是否正在运行。\n"
        "为防止剩余时长被继续消耗，雷神暂时无法关闭。\n"
        "请确认雷神状态后重试。"
    ),
    CloseDecision.BLOCK_RUNNING: (
        "雷神总时长正在消耗，已阻止关闭。\n"
        "正在尝试暂停，暂停成功后会放行关闭。"
    ),
    CloseDecision.BLOCK_PAUSE_FAILED: (
        "自动暂停失败，无法确认总时长已停止。\n"
        "为防止剩余时长被继续消耗，雷神暂时无法关闭。\n"
        "请在雷神界面手动点击「暂停时长」。"
    ),
}


def decide_close_action(state: DurationState,
                        enabled: bool = True,
                        block_when_unknown: bool = True,
                        block_when_pause_failed: bool = True,
                        pause_failed: bool = False) -> CloseDecision:
    """关闭保护策略判定（纯函数，规格书 §十四 四种情况的完整实现）。

    情况1 PAUSED  → 直接允许关闭
    情况2 RUNNING → 拦截关闭（由调用方执行暂停，成功后再放行）
    情况3 UNKNOWN → 禁止关闭
    情况4 暂停失败 → 禁止关闭（仅当 block_when_pause_failed 打开）
    """
    if not enabled:
        return CloseDecision.DISABLED
    if state is DurationState.PAUSED:
        return CloseDecision.ALLOW
    if state is DurationState.RUNNING:
        # 暂停失败过仍要继续拦：交由调用方决定是否再试
        return CloseDecision.BLOCK_PAUSE_FAILED if (pause_failed and block_when_pause_failed) \
            else CloseDecision.BLOCK_RUNNING
    # UNKNOWN
    if pause_failed and block_when_pause_failed:
        return CloseDecision.BLOCK_PAUSE_FAILED
    return CloseDecision.BLOCK_UNKNOWN if block_when_unknown else CloseDecision.ALLOW


@dataclass
class Evidence:
    """单条识别证据。任何结论都必须能追溯到证据。"""
    method: str                 # ui_automation / ocr / image / none
    state: DurationState
    detail: str = ""
    raw: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.method}={self.state.value}({self.detail})"


@dataclass
class Reading:
    """一次完整的状态识别结果（多源证据的合成结论）。"""
    state: DurationState = DurationState.UNKNOWN
    evidence: list = field(default_factory=list)
    hwnd: int = 0
    rect: tuple = None
    ts: float = field(default_factory=time.time)
    conflict: bool = False

    @property
    def methods(self) -> list:
        return [e.method for e in self.evidence if e.state is not DurationState.UNKNOWN]

    def summary(self) -> str:
        return "; ".join(str(e) for e in self.evidence) or "无证据"

    def age_ms(self) -> float:
        return (time.time() - self.ts) * 1000.0


def combine_states(votes: list) -> tuple:
    """把多路证据合成一个结论。

    votes: [(method, DurationState|None), ...]
    返回 (state, detail, conflict)

    规则（规格书 §七/§九：结果冲突即为 UNKNOWN，不要强行猜测）：
      - 全都没有产出          -> UNKNOWN
      - 只有一路有产出        -> 采信该路
      - 多路一致              -> 采信
      - 多路冲突              -> UNKNOWN
    """
    valid = [(m, s) for m, s in votes if s is not None and s is not DurationState.UNKNOWN]
    if not valid:
        return DurationState.UNKNOWN, "所有识别方式都没有给出结论", False
    states = {s for _, s in valid}
    methods = [m for m, _ in valid]
    if len(states) == 1:
        s = states.pop()
        return s, f"由 {'+'.join(methods)} 判定为 {s.value}", False
    detail = "识别结果冲突: " + ", ".join(f"{m}={s.value}" for m, s in valid)
    return DurationState.UNKNOWN, detail, True


class StateMachine:
    """记录状态与流转历史；防止瞬时噪声导致误判。"""

    def __init__(self, history: int = 20):
        self._history: list = []
        self._max = history
        self._current = Reading()
        self._transitions: list = []

    @property
    def current(self) -> Reading:
        return self._current

    @property
    def state(self) -> DurationState:
        return self._current.state

    @property
    def history(self) -> list:
        return list(self._history)

    @property
    def transitions(self) -> list:
        return list(self._transitions)

    def note(self, reading: Reading) -> bool:
        """记录一次识别结果。返回是否发生了状态变化。"""
        prev = self._current.state
        self._history.append(reading)
        if len(self._history) > self._max:
            self._history.pop(0)
        self._current = reading
        if reading.state is not prev:
            self._transitions.append((time.time(), prev, reading.state))
            return True
        return False

    def consecutive(self, state: DurationState, n: int) -> bool:
        """最近 n 次识别是否都是该状态（用于确认稳定，例如确认已暂停）。"""
        tail = self._history[-n:]
        return len(tail) == n and all(r.state is state for r in tail)

    def reset(self) -> None:
        self._history.clear()
        self._current = Reading()
        self._transitions.clear()
