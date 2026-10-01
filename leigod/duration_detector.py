"""总时长状态识别（规格书 §七/§八/§九 的合成实现）。

优先级：UI Automation（结构化、可靠） → 截图 + OCR（兜底）
多路证据一致才下结论；冲突即 UNKNOWN；拿不到任何证据也是 UNKNOWN。

绝不做的事：把 UNKNOWN 当 PAUSED；用脏图（被遮挡/全黑）去猜。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core.state_machine import (DurationState, Evidence, Reading, combine_states)
from detection import coordinate_fallback as cf
from detection import image_detection as img
from detection import ocr as ocr_mod
from detection import ui_automation as uia


@dataclass
class CaptureResult:
    array: object = None
    method: str = ""
    detail: str = ""
    ok: bool = False


class DurationDetector:
    """总时长状态识别。

    ## 为什么除了「识别状态」还要负责「定位按钮」

    真机上 UI Automation 枚举到 **0 个控件**（Electron，见
    `docs/真机发现-关闭保护架构问题.md`），所以「按 §二十五 做坐标校准」这条路
    只能靠人工悬停取样。而 OCR 本来就已经识别出「暂停时长 / 开启时长」的文案，
    并且给出了它的**包围盒** —— 用这个框直接定位按钮，既省掉人工校准，
    又天然满足规格书「不保存绝对屏幕坐标」的要求（每次识别都是现算的）。
    """

    #: 按钮文字框宽度上限（相对窗口宽度）。OCR 有时会把整行合并成一个框，
    #: 那种框的中心不能代表按钮位置 —— 宁可放弃定位，也不能按错位置点击。
    BUTTON_MAX_W_RATIO = 0.26
    #: 参与定位所需的最低文字置信度
    BUTTON_MIN_CONFIDENCE = 0.5
    #: 「控件树变稀薄」的判据：真机正常时 UIA 能枚举到 ~476 个控件，
    #: 而雷神弹出自己的确认框（在主窗口内绘制的模态层）之后只剩 ~5 个 ——
    #: 此时「找不到暂停/开启按钮」**不是识别失败，而是被模态层挡住了**。
    #: 判据只用于**诊断文案**（告诉用户为什么读不到），不改变判定结果：
    #: 状态依旧是 UNKNOWN，依旧按 §六 不当 PAUSED。
    UIA_THIN_TREE_N = 10
    #: `ocr_mode="auto"` 下 OCR 几何的强制刷新间隔（秒）。
    #: OCR 的副产品是「按钮文字框 → 屏幕坐标」的换算信息，坐标回退点击要靠它。
    #: 若长期跳过 OCR，这份几何会过期（窗口移动/缩放后就不准了），所以即使 UIA
    #: 一直能定论，也要每隔一段时间老实跑一次把它刷新。
    OCR_REFRESH_S = 60.0

    def __init__(self, config, logger=None):
        self.config = config
        self.log = logger
        #: `detect()` 未显式传 `ocr_mode` 时使用的默认值。
        #:
        #: 为什么要做成**实例默认**而不是让每个调用点自己传：
        #: 上一轮把 `ocr_mode="auto"` 只接到了状态轮询那一条路径上
        #: （`protection_engine._poll_state`），**暂停链路全漏了** ——
        #: `duration_controller` 里 before / _verify / 重试前共 4~8 次 `detect()`
        #: 全都走函数默认值 `"always"`，每次白白多跑一遍整窗 OCR（本机 1.3s+）。
        #: 用户感受到的就是「点 ✕ 之后要等很久才自动暂停」。
        #: 教训：**性能开关不能靠调用方记得传**，默认值才是唯一可靠的地方。
        self.default_ocr_mode = str(config.get("detection.ocr_mode", "auto") or "auto").lower()
        self.last_capture = None          # CaptureResult
        self.last_uia = {"pause": [], "start": [], "all": [], "all_n": 0}
        #: 最近一次 UIA 扫描耗时（毫秒）。性能问题必须**可量化**，
        #: 否则「延迟降没降」只能靠感觉 —— 这个项目已经因此吃过一次亏。
        self.last_uia_ms = 0.0
        #: 最近一次 UIA 结论是否来自控件缓存（见 `detect_uia`）
        self.last_uia_from_cache = False
        self.last_ocr_lines = []
        #: 最近一次 OCR 的几何换算信息（把文字框换算回屏幕坐标要用它）
        self.last_ocr_geom = None
        #: 最近一次截图对应的窗口 frame（`capture()` 设置）
        self._frame_of_last_capture = None
        #: 最近一次**真的跑过** OCR 的时间戳（`ocr_mode="auto"` 的刷新判据）
        self._last_ocr_ts = 0.0

    # ------------------------------------------------------------------ UIA
    def detect_uia(self, win, fast: bool = False) -> list:
        """返回 Evidence 列表（可能有多条，用于暴露冲突）。

        `fast=True` 时优先走控件缓存（只重读 1~2 个控件的 Name，微秒级），
        缓存失效则自动退回全量扫描。高频的轮询/暂停后复核应该用它 ——
        真机上全量枚举一棵 ~476 个控件的 Electron 树要 0.3~2 秒，
        而「点 ✕ 后多久才暂停」几乎完全由这段开销决定。
        """
        out = []
        # 顶栏裁剪区过滤：雷神窗口里叫「开启时长」的控件不止顶栏那一个按钮
        # （真机实测 1 个「暂停时长」+ 20 个「开启时长」→ 两边同时成立 → 判定冲突
        # → 状态永久读不出来）。这里与 OCR 用同一套 `detection.topbar_crop` 收敛。
        frame = win.frame or win.rect
        crop = self.config.get("detection.topbar_crop")
        try:
            res = uia.find_duration_controls(win.hwnd, frame=frame, crop=crop, fast=fast)
        except TypeError:                       # 旧签名（无 frame/crop/fast 参数）
            res = uia.find_duration_controls(win.hwnd)
        self.last_uia = res
        self.last_uia_ms = float(res.get("scan_ms") or 0.0)
        #: 最近一次 UIA 结论是不是从**控件缓存**读来的。
        #: 缓存读可能拿到过期名字 → 不能直接当结论；而全量枚举是地面真相。
        #: 调用方（`_verify`）靠这一位决定"还要不要再复核一次"。
        self.last_uia_from_cache = bool(res.get("from_cache"))
        pause, start = res["pause"], res["start"]
        if pause and start:
            out.append(Evidence("ui_automation", DurationState.RUNNING,
                                f"同时存在「暂停时长」({len(pause)}) 与「开启时长」({len(start)})",
                                {"pause": [p.as_dict() for p in pause],
                                 "start": [s.as_dict() for s in start]}))
            out.append(Evidence("ui_automation", DurationState.PAUSED,
                                "同时存在两种按钮，证据冲突",
                                {"start": [s.as_dict() for s in start]}))
            return out
        if pause:
            out.append(Evidence("ui_automation", DurationState.RUNNING,
                                f"找到按钮「{pause[0].name}」",
                                {"ctrl": pause[0].as_dict()}))
        elif start:
            out.append(Evidence("ui_automation", DurationState.PAUSED,
                                f"找到按钮「{start[0].name}」",
                                {"ctrl": start[0].as_dict()}))
        else:
            # 控件总数改用 `all_n`（扫描时数出来的），不再为了拿长度而
            # 把 476 个控件全建成 Python 对象 —— 那正是旧实现慢的根源。
            n = int(res.get("all_n", len(res.get("all") or [])) or 0)
            # 真机第 4 轮暴露的现象：重放点击后雷神弹出确认框，UIA 从 ~476 个控件
            # 掉到 5 个、OCR 报「截屏区域被其它窗口遮挡」→ 状态恒 UNKNOWN，
            # 收尾复核与暂停都无从下手。判定不变（仍是 UNKNOWN、仍不放行），
            # 但必须把**为什么读不到**写清楚，否则用户只看到一句"状态未知"。
            thin = 0 < n < self.UIA_THIN_TREE_N
            detail = f"窗口内未找到目标按钮（共枚举 {n} 个控件）"
            if thin:
                detail += ("；控件数偏少，若雷神正弹着确认框/模态层则属正常遮挡"
                           "（此时状态不可读，按 §六 仍按 UNKNOWN 处理）")
            out.append(Evidence("ui_automation", DurationState.UNKNOWN, detail,
                                {"thin_tree": True, "controls": n} if thin
                                else {"controls": n}))
        return out

    # --------------------------------------------------------------- 截图
    def capture(self, win) -> CaptureResult:
        mode = self.config.get("detection.capture", "auto")
        w, h = win.size
        frame = win.frame or win.rect
        fw = frame[2] - frame[0]
        fh = frame[3] - frame[1]
        # 记下本次截图对应的窗口 frame：`_record_ocr_geom` 要拿它把 OCR 文字框
        # 换算回屏幕坐标。放在这里（而不是在成功分支）是为了覆盖所有 return 路径，
        # 否则某一分支漏写会让 `last_ocr_geom` 静默失效、按钮定位悄悄不工作。
        self._frame_of_last_capture = [int(v) for v in frame] if frame else None

        if mode in ("auto", "printwindow"):
            arr = cf.capture_window_printwindow(win.hwnd)
            ok, why = img.looks_like_real_capture(arr, (w, h))
            if ok:
                res = CaptureResult(arr, "printwindow", why, True)
                self.last_capture = res
                return res
            if mode == "printwindow":
                res = CaptureResult(None, "printwindow", why, False)
                self.last_capture = res
                return res

        # 屏幕截取：要求窗口在前台，且目标区域确实没被遮挡
        if self.config.get("detection.require_foreground_for_screen_grab", True):
            if cf.user32.GetForegroundWindow() != win.hwnd:
                res = CaptureResult(None, "screen", "雷神不在前台，无法安全截屏", False)
                self.last_capture = res
                return res
        probes = [(frame[0] + fw // 2, frame[1] + fh // 2),
                  (frame[0] + int(fw * 0.75), frame[1] + int(fh * 0.06))]
        for (px, py) in probes:
            if not cf.is_point_over_window(win.hwnd, px, py):
                res = CaptureResult(None, "screen", f"截屏区域被其它窗口遮挡（{px},{py}）", False)
                self.last_capture = res
                return res
        arr = cf.capture_region_screen(frame)
        ok, why = img.looks_like_real_capture(arr, (fw, fh))
        res = CaptureResult(arr if ok else None, "screen", why, ok)
        self.last_capture = res
        return res

    # ----------------------------------------------------------------- OCR
    def detect_ocr(self, win) -> list:
        if not self.config.get("detection.ocr_enabled", True):
            return [Evidence("ocr", DurationState.UNKNOWN, "OCR 已在配置中关闭", {})]
        if not ocr_mod.engine_available():
            # 把**真实原因**带出去。只说"未安装"会把「模型没打进包」这种
            # 打包事故误报成"用户没装依赖"，排查方向直接跑偏（真机 2026-09-30）。
            why = ocr_mod.last_error() or "未安装 rapidocr-onnxruntime"
            return [Evidence("ocr", DurationState.UNKNOWN, f"OCR 引擎不可用：{why}",
                             {"ocr_error": why})]
        cap = self.capture(win)
        if not cap.ok:
            return [Evidence("ocr", DurationState.UNKNOWN, f"截图不可用：{cap.detail}",
                             {"capture": cap.method})]
        crop_cfg = self.config.get("detection.topbar_crop",
                                   {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14})
        region = img.crop_relative(cap.array, win.rect, crop_cfg)
        lines = ocr_mod.read_text(region)
        self.last_ocr_lines = lines
        self._record_ocr_geom(cap.array, region, crop_cfg)
        self._last_ocr_ts = time.time()
        state_name, detail = ocr_mod.classify_text(lines)
        if self.config.get("detection.use_color_check", False):
            mean = img.region_mean_color(region)
            cstate, cdetail = img.classify_button_color(mean)
            detail += f"；颜色参考 {cdetail}"
            if state_name and cstate and cstate != state_name:
                return [Evidence("ocr", DurationState.UNKNOWN,
                                 f"文字与颜色冲突：文字={state_name}，颜色={cstate}", {}),
                        Evidence("image", DurationState.UNKNOWN, cdetail, {})]
        return [Evidence("ocr", ocr_mod.to_state_name(state_name),
                         detail, {"lines": lines, "capture": cap.method})]

    # -------------------------------------------------------------- 合成
    def detect(self, win, allow_ocr: bool = True, ocr_mode: str = None,
               uia_fast: bool = False) -> Reading:
        """合成一次状态识别。

        `ocr_mode=None`（默认）表示「用配置里的默认档」，见
        `self.default_ocr_mode` —— 这样新旧调用点都不必记得传参，
        不会出现「一条路径优化了、另一条还是慢的」这种半成品状态。

        `ocr_mode`（用来治「识别延迟高」）：

        - `"always"`：**每轮都跑** OCR（旧行为，也是最保守的）。
        - `"auto"`：UIA 已经给出**唯一且确定**的结论时**跳过 OCR**。

        为什么要加这个开关：整窗 OCR 在本机要 **1.3s 以上**，而 `detect()` 原先是
        「先 UIA 再 OCR」无条件串联的 —— 于是每一轮状态轮询都被 1.3s 拖住，引擎
        tick 名义 500ms、实际 1.3s+，用户感受到的就是「状态半天不变、点 ✕ 之后要等
        半天」。而 UIA 是结构化识别，能在几十毫秒内给出确定答案，此时再跑 OCR
        对结论没有任何贡献，纯粹是浪费 1.3s。

        安全性：跳过 OCR **不会**让判定变松 —— 只在 UIA 内部一致（无冲突、非 UNKNOWN）
        时才跳；UIA 拿不到或自相矛盾时照旧跑 OCR。
        """
        mode = str(ocr_mode or self.default_ocr_mode or "auto").lower()
        evidence = []
        evidence.extend(self.detect_uia(win, fast=uia_fast))
        if allow_ocr and mode != "off":
            if mode != "auto" or self._need_ocr(evidence):
                evidence.extend(self.detect_ocr(win))

        votes = [(e.method, e.state) for e in evidence]
        state, detail, conflict = combine_states(votes)
        reading = Reading(state=state, evidence=evidence, hwnd=win.hwnd,
                          rect=win.rect, conflict=conflict)
        if self.log:
            self.log.debug("状态识别: %s | %s | %s", state.value, detail, reading.summary())
        return reading

    def _need_ocr(self, evidence) -> bool:
        """`ocr_mode="auto"` 时，这一轮是否**真的**还需要跑 OCR。

        跳过的前提必须同时满足三条，缺一不可：
          ① UIA 说话了；② UIA 内部**没有**自相矛盾、也不是 UNKNOWN；
          ③ 按钮定位几何还没过期（否则坐标回退会用到陈旧换算）。
        任何一条不满足就老实跑 OCR —— 省时间不能以牺牲判定可靠性为代价。
        """
        uia_ev = [e for e in evidence if e.method == "ui_automation"]
        if not uia_ev:
            return True                       # UIA 没说话 → 只能靠 OCR
        states = {e.state for e in uia_ev}
        if len(states) != 1 or DurationState.UNKNOWN in states:
            return True                       # UIA 自相矛盾或没认出来
        if self.last_ocr_geom is None:
            return True                       # 从没 OCR 过 → 先攒一份几何备用
        return (time.time() - (self._last_ocr_ts or 0.0)) > self.OCR_REFRESH_S

    # ------------------------------------------------- 供控制器复用
    def duration_controls(self) -> dict:
        return self.last_uia

    # ------------------------------------------------------ 按钮定位（OCR 框）
    def _record_ocr_geom(self, arr, region, crop_cfg) -> None:
        """记下「文字框坐标 → 屏幕坐标」所需的几何。

        OCR 跑在**裁剪后**的小图上，所以要换算三次才能落到屏幕：
          文字框坐标  --(+裁剪偏移)-->  截图数组像素  --(× 数组→frame 比例)-->  屏幕
        中间那步不能省：`looks_like_real_capture` 允许截图尺寸与窗口尺寸有 25% 偏差，
        而 PrintWindow 在部分 DPI 下给出的确实是缩放过的图。直接相加会让
        每一次点击整体偏移，且偏移量随缩放比变化 —— 极难现场排查。
        """
        if arr is None or region is None:
            self.last_ocr_geom = None
            return
        try:
            ah, aw = arr.shape[:2]
            rh, rw = region.shape[:2]
            crop = crop_cfg or {}
            l = int(max(0.0, min(1.0, crop.get("left", 0.0))) * aw)
            t = int(max(0.0, min(1.0, crop.get("top", 0.0))) * ah)
            # 裁剪区域实际覆盖的像素范围（与 image_detection.crop_relative 的夹紧规则一致）
            l = min(max(0, l), max(0, aw - 1))
            t = min(max(0, t), max(0, ah - 1))
            r = min(max(int(max(0.0, min(1.0, crop.get("right", 1.0))) * aw), l + 1), aw)
            b = min(max(int(max(0.0, min(1.0, crop.get("bottom", 1.0))) * ah), t + 1), ah)
            frame = list(self._frame_of_last_capture or [])
            if len(frame) != 4:
                self.last_ocr_geom = None
                return
            self.last_ocr_geom = {
                "frame": frame,
                "frame_size": [max(1, frame[2] - frame[0]), max(1, frame[3] - frame[1])],
                "arr_size": [aw, ah],
                "crop_px": [l, t, r, b],
                "region_size": [rw, rh],
            }
        except Exception:
            self.last_ocr_geom = None

    def _match_button_line(self, want: str):
        """在最近一次 OCR 结果里找按钮文案那一行。`want` ∈ {running, paused}。

        只认关键词不够稳：同一屏里可能既有「暂停时长」又有别的零星「暂停」字样。
        所以排序时把「同时含『时长』」的行排在前面 —— 真按钮的文案是
        「暂停时长 / 开启时长」，含「时长」才像按钮。取不到就返回 None。
        """
        keys = ("暂停",) if want == "running" else ("开启", "开始", "恢复",
                                                   "并启", "开肩", "并起", "开起")
        best, best_score = None, None
        for line in (self.last_ocr_lines or []):
            try:
                conf = float(line.get("confidence", 0))
            except (TypeError, ValueError):
                continue
            if conf < self.BUTTON_MIN_CONFIDENCE:
                continue
            text = ocr_mod.normalize(line.get("text") or "")
            if not any(k in text for k in keys):
                continue
            bbox = line.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            score = (1 if "时长" in text else 0, conf)
            if best_score is None or score > best_score:
                best, best_score = line, score
        return best

    def button_rect_on_screen(self, state: DurationState):
        """用最近一次 OCR 的文字框推算按钮在**屏幕**上的矩形 `(l,t,r,b)`。

        找不到（未做 OCR / 文案没识别到 / 框太宽像整行合并 / 几何缺失）
        一律返回 `None` —— 绝不猜位置。规格书 §三十四：定位不了就不点。
        """
        if self.last_ocr_geom is None or not self.last_ocr_lines:
            return None
        want = "running" if state is DurationState.RUNNING else (
            "paused" if state is DurationState.PAUSED else None)
        if want is None:
            return None
        line = self._match_button_line(want)
        if not line or not line.get("bbox"):
            return None
        g = self.last_ocr_geom
        fl, ft, fr, fb = g["frame"]
        fw, fh = g["frame_size"]
        aw, ah = g.get("arr_size") or (fw, fh)
        rw, rh = g["region_size"]
        cl, ct, cr, cb = g["crop_px"]
        bx0, by0, bx1, by1 = [float(v) for v in line["bbox"]]
        # 第 1 步：裁剪图坐标 → 截图数组像素（region 就是数组的切片，比例通常为 1；
        # 留成比例是为了兼容「OCR 前又缩放过一次」的实现改动）
        ax = (cr - cl) / max(1, rw)
        ay = (cb - ct) / max(1, rh)
        px0, px1 = cl + bx0 * ax, cl + bx1 * ax
        py0, py1 = ct + by0 * ay, ct + by1 * ay
        # 第 2 步：截图数组像素 → 窗口 frame 像素（PrintWindow 可能给缩放图）
        kx = fw / max(1, aw)
        ky = fh / max(1, ah)
        w_px = (px1 - px0) * kx
        if w_px > fw * self.BUTTON_MAX_W_RATIO:
            # 框太宽 → 大概率是把整行（倒计时 + 按钮 + 分类标签）合并成了一个框
            if self.log:
                self.log.warning("OCR 按钮框过宽（%.0fpx > 窗口的 %.0f%%），放弃用它定位",
                                 w_px, self.BUTTON_MAX_W_RATIO * 100)
            return None
        # 第 3 步：加 frame 原点 → 屏幕坐标
        return (int(round(fl + px0 * kx)), int(round(ft + py0 * ky)),
                int(round(fl + px1 * kx)), int(round(ft + py1 * ky)))

    def button_center_on_screen(self, state: DurationState):
        rect = self.button_rect_on_screen(state)
        if not rect:
            return None
        return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)

    def save_debug_capture(self, path: str) -> bool:
        try:
            from PIL import Image
            cap = self.last_capture
            if not cap or cap.array is None:
                return False
            Image.fromarray(cap.array).save(path)
            return True
        except Exception:
            return False
