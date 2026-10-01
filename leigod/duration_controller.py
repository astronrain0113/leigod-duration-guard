"""暂停控制（规格书 §十 / §十一 / §十五）。

铁律：**点击成功 ≠ 暂停成功**。
  读状态 → 确认 RUNNING → 执行暂停 → 等待 → 重新读状态 → 确认 PAUSED
严禁出现 `if click_success: return success` 这种写法。

另一个容易出人命的细节：每次重试前**必须重新读状态**。
若上一次其实已经点成功、只是验证环节抖动（OCR 偶发失败），
不看状态就再点一次，会把「开启时长」点回去，等于主动帮用户重新开始消耗时长。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.state_machine import DurationState
from detection import coordinate_fallback as cf
from detection import ocr as ocr_mod
from detection import ui_automation as uia


@dataclass
class PauseOutcome:
    success: bool = False
    strategy: str = ""
    detail: str = ""
    before: DurationState = DurationState.UNKNOWN
    after: DurationState = DurationState.UNKNOWN
    attempts: int = 0
    evidence: list = field(default_factory=list)

    def line(self) -> str:
        return (f"{'成功' if self.success else '失败'} | 策略={self.strategy} | "
                f"{self.before.value}→{self.after.value} | 尝试={self.attempts} | {self.detail}")


class DurationController:
    def __init__(self, config, detector, logger=None):
        self.config = config
        self.detector = detector
        self.log = logger

    # ------------------------------------------------------------ 工具
    def _log(self, level, msg, *a):
        if self.log:
            getattr(self.log, level, self.log.info)(msg, *a)

    def _coordinate(self):
        ratio = self.config.get("duration.coordinate.ratio")
        pos = self.config.get("duration.coordinate.pos")
        return ratio, pos

    def _strategy_available(self) -> tuple:
        """返回 (主策略名, 可用性说明)。

        真机上有个反直觉的组合（见 `docs/真机发现-关闭保护架构问题.md`）：
        `ui_automation.available()` 返回 True（模块能加载），但窗口内**枚举到 0 个控件**
        （Electron 自定义绘制）。所以「UIA 可用」不等于「UIA 这条路走得通」，
        真正的兜底必须是坐标点击。而坐标点击又要求先做过 §二十五 的校准。
        为了不在真机上卡死在「未校准」，这里承认第三条路：OCR 文字框实时定位。
        """
        if uia.available():
            return "ui_automation", "UIA 可用（真机上可能枚举到 0 个控件，届时自动回退）"
        ratio, pos = self._coordinate()
        if ratio or pos:
            return "mouse_click", "仅相对坐标可用（UIA 不可用）"
        if ocr_mod.engine_available():
            return "mouse_click_ocr", ("UIA 不可用且未校准坐标；"
                                       "将用 OCR 识别到的按钮文字框实时定位")
        return "", "既无 UIA 也未校准坐标，且未安装 OCR 引擎"

    #: 快相轮询间隔（秒）：只跑 UIA，所以可以很快
    FAST_POLL_S = 0.10
    #: 快相要连续读到几次 PAUSED 才认。为什么不是 1 次：点下去之后雷神有
    #: 1~2 秒的过渡态（按钮变灰、文案待变），单帧读数容易踩在过渡中间。
    #: 多要 0.1 秒，换掉「其实没停稳就放行」的风险 —— 这个方向的错绝对不能犯。
    FAST_CONFIRM_N = 2
    #: 快相窗口（秒）。窗口内 UIA 没定论就转慢相上 OCR。
    FAST_WINDOW_S = 1.2

    def _verify(self, win, deadline: float, settle: float = 0.2,
                fast_window: float = None):
        """在截止时间前反复识别，直到确认 PAUSED。返回 (state, 最后一次 Reading)。

        **两段式**（治用户反馈的「点完 ✕ 要等很久才自动暂停」）：

          快相：`allow_ocr=False`，只跑 UI Automation，每 0.1s 一次。
                按钮文案一变 UIA 立刻读得到，正常情况 0.2~0.6s 就能确认 PAUSED。
                旧写法这里每次迭代都跑一遍整窗 OCR（本机 1.3s+），
                一轮验证就要 2~3 秒，用户当然觉得慢。
          慢相：快相窗口内没抓到 → 再上 OCR 兜底（每 0.3s），直到 deadline。
                这段**不能砍**：UIA 认不出（Electron 改版 / 控件树被模态层遮挡）时，
                OCR 是唯一的证据来源，砍掉就等于在真机上退回「读不到状态」。

        安全性没有放松：快相只**接受** PAUSED，绝不因此把 UNKNOWN 当 PAUSED；
        而且要求连续两次读到才认。
        """
        if settle > 0:
            time.sleep(settle)
        win_limit = self.FAST_WINDOW_S if fast_window is None else float(fast_window)
        fast_deadline = min(deadline, time.time() + max(0.0, win_limit))
        last, streak = None, 0
        while time.time() < fast_deadline:
            # `uia_fast=True`：走控件缓存，只重读 1~2 个控件的 Name。
            # 真机上全量枚举要 0.3~2 秒，而这里本来打算每 0.1 秒采一次 ——
            # 用全量扫描等于每次采样都超时，于是「确认暂停」变成 2~4 秒。
            last = self.detector.detect(win, allow_ocr=False, uia_fast=True)
            # 这一读是**缓存的旧名字**还是**刚做的一次全量枚举**？两者可信度不同：
            # 缓存可能过期；全量枚举读的是活树上的真实名字，本身就是地面真相。
            # 真机实测（2026-09-30 19:45）：点 ✕ 一共跑了 3 次全量扫描，
            # 其中"收尾复核"那次是多余的 —— 前一次已经是全量枚举了。
            # 少跑一次 = 少 683ms，正好是把 2.5s 压进 1~2s 的关键。
            cached = bool(getattr(self.detector, "last_uia_from_cache", False))
            if last.state is DurationState.PAUSED:
                if not cached:
                    return DurationState.PAUSED, last
                streak += 1
                if streak >= self.FAST_CONFIRM_N:
                    # 连续两次**缓存**都说是 PAUSED，仍需一次全量扫描复核：
                    # 缓存元素万一还在返回旧名字（COM 代理被缓存），快路径就会
                    # 给出过期结论。快路径只负责"等得少"，**下结论必须用地面真相**。
                    ground = self.detector.detect(win, allow_ocr=False, uia_fast=False)
                    if ground.state is DurationState.PAUSED:
                        return DurationState.PAUSED, ground
                    streak = 0          # 与全量扫描不一致 → 以全量为准，继续等
                    last = ground
            else:
                streak = 0
            if time.time() >= fast_deadline:
                break
            time.sleep(self.FAST_POLL_S)

        # 慢相：UIA 给不出结论 → 老实跑 OCR 兜底
        while True:
            last = self.detector.detect(win, allow_ocr=True)
            if last.state is DurationState.PAUSED:
                return DurationState.PAUSED, last
            if time.time() >= deadline:
                return last.state, last
            time.sleep(0.3)

    # ------------------------------------------------------------ 主流程
    def pause(self, win, before_reading=None) -> PauseOutcome:
        """执行一次「确保暂停」。

        `before_reading`：调用方**刚刚**读到的状态。关闭保护链路（`_close_flow` /
        `_guard_ensure_paused`）在读完之后立刻调用本函数，若这里再读一次，就是
        白白多花一次识别时间（真机上是数百毫秒到 1.3 秒）。传进来即可省掉，
        判定规则不变 —— 它只是省一次**重复**读取，不是跳过读取。
        """
        cfg_timeout = max(500, int(self.config.get("duration.verify_timeout_ms", 3000))) / 1000.0
        max_retries = max(1, int(self.config.get("duration.max_retries", 3)))
        interval = max(0.0, int(self.config.get("duration.retry_interval_ms", 1500)) / 1000.0)

        out = PauseOutcome()
        before = before_reading if before_reading is not None else self.detector.detect(win)
        out.before = before.state
        out.evidence.append(before.summary())
        self._log("info", "暂停流程开始：当前状态=%s（%s）", before.state.value, before.summary())

        if before.state is DurationState.PAUSED:
            out.success, out.strategy, out.detail = True, "none", "时长已处于暂停状态，无需操作"
            out.after = DurationState.PAUSED
            return out
        if before.state is DurationState.UNKNOWN:
            out.detail = "无法确认雷神当前总时长状态，未执行任何点击（Fail Safe）"
            self._log("warning", out.detail)
            return out

        strategy, why = self._strategy_available()
        if not strategy:
            out.detail = f"没有可用的暂停方式：{why}"
            self._log("error", out.detail)
            return out

        for attempt in range(1, max_retries + 1):
            out.attempts = attempt
            # —— 关键：重试前重新读状态，避免把已暂停的点回去 ——
            if attempt > 1:
                if interval > 0:
                    time.sleep(interval)
                pre = self.detector.detect(win)
                if pre.state is DurationState.PAUSED:
                    out.success, out.strategy = True, "none"
                    out.after = DurationState.PAUSED
                    out.detail = f"第 {attempt - 1} 次点击实际已生效（本次验证确认）"
                    self._log("info", out.detail)
                    return out
                if pre.state is DurationState.UNKNOWN:
                    out.detail = "重试前无法确认状态，停止点击（Fail Safe）"
                    self._log("warning", out.detail)
                    return out

            ok, action_detail = self._do_pause(win, strategy)
            out.strategy = strategy
            self._log("info", "第 %d 次尝试（%s）：%s", attempt, strategy, action_detail)

            deadline = time.time() + cfg_timeout
            # settle 从 0.2s 降到 0.1s：快相现在是微秒级的缓存读取，
            # 不需要靠"睡一会儿等 UI 更新"来避免空转。
            state, reading = self._verify(win, deadline, settle=0.1)
            out.after = state
            out.evidence.append(reading.summary())
            if state is DurationState.PAUSED:
                out.success = True
                out.detail = (f"第 {attempt} 次点击后确认已暂停"
                              f"（{action_detail}；{reading.summary()}）")
                self._log("info", "暂停成功：%s", out.detail)
                return out
            out.detail = (f"第 {attempt} 次点击后仍未确认暂停"
                          f"（{action_detail}；当前识别为 {state.value}：{reading.summary()}）")
            self._log("warning", out.detail)

        out.detail += f"；已尝试 {out.attempts} 次仍无法确认暂停"
        self._log("error", "暂停失败：%s", out.detail)
        return out

    # ------------------------------------------------------------ 执行
    def _do_pause(self, win, strategy: str) -> tuple:
        """执行一次暂停动作。返回 (是否动作下发成功, 说明)。

        「动作下发成功」只代表**点下去了**，绝不代表暂停成功 —— 验证在 `pause()` 里。
        回退链是有意为之：UIA → 校准坐标 → OCR 实时定位。
        任何一级都不猜坐标；全都定位不到就明确失败，交给上层决定是否重试。
        """
        if strategy == "ui_automation":
            res = self.detector.duration_controls()
            pause_ctrls = res.get("pause") or []
            # 优先挑可 Invoke 的
            pause_ctrls = sorted(pause_ctrls, key=lambda c: (not c.supports_invoke, not c.is_enabled))
            for ctrl in pause_ctrls:
                ok, detail = uia.invoke(ctrl)
                if ok:
                    return True, f"UIA 调用按钮「{ctrl.name}」成功（{detail}）"
            if pause_ctrls:
                return False, "找到暂停按钮但调用失败"
            # 枚举到 0 个控件（真机常态）→ 退回坐标点击
            ratio, pos = self._coordinate()
            if ratio or pos:
                return self._mouse_pause(win, ratio, pos)
            return self._ocr_pause(win)

        ratio, pos = self._coordinate()
        if ratio or pos:
            return self._mouse_pause(win, ratio, pos)
        return self._ocr_pause(win)

    def _mouse_pause(self, win, ratio, pos) -> tuple:
        if not ratio and not pos:
            return False, "未校准暂停按钮坐标（请运行 diagnostics/calibration.py）"
        rect = cf.ensure_visible(win.hwnd)
        if rect[0] < -20000:
            return False, "窗口仍在屏幕外，无法点击"
        cf.activate_window(win.hwnd)
        x, y = cf.compute_click_point(rect, ratio=ratio, pos=pos)
        hold = int(self.config.get("duration.coordinate.hold_ms", 120))
        cf.press_click(x, y, hold)
        return True, (f"在相对坐标 ratio={ratio or '-'} pos={pos or '-'} 点击（屏幕 {x},{y}，"
                      f"保持 {hold}ms）")

    # ---------------------------------------------------- OCR 实时定位点击
    def _ocr_pause(self, win) -> tuple:
        """用最近一次 OCR 识别到的按钮文字框定位并点击。

        相比「校准相对坐标」，这条路有两个好处，正好对上真机的两个死结：
          1. 不需要人工校准（真机 UIA 枚举 0 个控件，§二十五 的悬停取样没有落点）；
          2. 坐标是每次识别现算的，**不落盘**，天然满足规格书「不保存绝对屏幕坐标」。

        代价是依赖一次新鲜截图，所以顺序很讲究：
          先恢复/激活窗口 → **刷新窗口几何** → 重新识别 → 再换算点击点。
        顺序错了会拿「移动前」的坐标去点「移动后」的位置 —— 差的就是整个窗口偏移。
        """
        loc = getattr(self.detector, "button_center_on_screen", None)
        if loc is None:
            return False, "当前检测器不支持 OCR 按钮定位"

        # 1) 先保证窗口在屏幕可见位置，否则截图拿到的是 (-25600,-25600) 的代次
        rect = cf.ensure_visible(win.hwnd)
        if rect[0] < -20000 or rect[1] < -20000:
            return False, "窗口仍在屏幕外，OCR 定位不可用"
        cf.activate_window(win.hwnd)

        # 2) 窗口可能刚被移动过，几何必须重新读，否则坐标换算用的是旧 frame
        try:
            from leigod import window as win_mod
            win_mod.refresh(win)
        except Exception as e:
            self._log("warning", "刷新窗口几何失败（继续按旧几何换算）：%s", e)

        # 3) 重新识别一次，把 last_ocr_lines / last_ocr_geom 刷到最新
        try:
            self.detector.detect_ocr(win)
        except Exception as e:
            return False, f"OCR 识别异常：{type(e).__name__}: {e}"

        # 4) 现在才换算点击点。定位不到就明确失败 —— 绝不退化成「点窗口中心」之类
        center = loc(DurationState.RUNNING)
        if not center:
            rect_try = None
            try:
                rect_try = getattr(self.detector, "last_ocr_geom", None)
            except Exception:
                pass
            return False, ("OCR 未能定位到「暂停时长」按钮文字框"
                           f"（几何信息={'有' if rect_try else '无'}）")

        hold = int(self.config.get("duration.coordinate.hold_ms", 120))
        cf.press_click(center[0], center[1], hold)
        g = getattr(self.detector, "last_ocr_geom", None) or {}
        frame = g.get("frame") or []
        rel = ""
        if len(frame) == 4:
            fw = max(1, frame[2] - frame[0])
            fh = max(1, frame[3] - frame[1])
            rel = (f"；相对窗口 ≈ ({center[0] - frame[0]}, {center[1] - frame[1]}) "
                   f"≈ ({(center[0] - frame[0]) / fw:.3f}, {(center[1] - frame[1]) / fh:.3f})")
        return True, (f"按 OCR 文字框定位点击（屏幕 {center[0]},{center[1]}，"
                      f"保持 {hold}ms{rel}）")

    def manual_pause(self) -> "PauseOutcome":
        """供 UI 的「立即保护 / 暂停时长」按钮使用。"""
        from leigod import window as win_mod
        win = win_mod.find_main_window(self.config)
        if not win:
            return PauseOutcome(False, "", "未找到雷神主窗口")
        return self.pause(win)
