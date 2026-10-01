"""坐标校准（规格书 §二十五）。

这是整个项目里**唯一被允许写坐标进配置**的地方。

规格书对它的硬性要求（原文照抄，不许打折）：

  「校准完成后必须立即执行一次真实测试。
    测试：当前 RUNNING → 校准按钮 → 执行点击 → 重新检测 → 是否变成 PAUSED？
    如果没有：Calibration FAILED，绝对不能显示 Calibration SUCCESS。」

因此本工具的结构是「求候选点 → 真实点击 → 重新识别 → 只有识别到 PAUSED 才算成功」。
只算出坐标就报成功是绝对不允许的：旧实现（ThunderGuard/calibrate.pyw）就是把
`robust_click(..., verify=False)` 的验证关掉，导致校准「永远成功」，而主程序
按它点下去毫无效果。

校准产物必须包含（规格书 §二十五）：
  HWND / 窗口身份 / 窗口宽度 / 窗口高度 / DPI / 相对坐标 / 目标区域
全部写进 config.json 的 `duration.coordinate`，其中 `calibration` 是元数据。

候选点来源三种，优先级从高到低：
  ui_automation  由 UI Automation 找到的「暂停时长」按钮包围盒反推（最可靠）
  manual         人在按钮上停住鼠标，倒计时取样（客户端改版后 UIA 不可用时）
  screen_point   显式给一个屏幕点（仅供排障/自动化）

一条纪律：本工具在「无法确认状态」时**不做任何点击**，并且退出码为 3。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core.config import load_config                                   # noqa: E402
from core.logging_setup import setup_logging                          # noqa: E402
from core.paths import base_dir                                       # noqa: E402
from core.state_machine import DurationState                          # noqa: E402
from detection import coordinate_fallback as cf                       # noqa: E402
from detection import ui_automation as uia                            # noqa: E402
from leigod import window as win_mod                                  # noqa: E402
from leigod.duration_detector import DurationDetector                 # noqa: E402

user32 = ctypes.windll.user32

#: 退出码
EXIT_OK = 0
EXIT_FAILED = 2          # 校准未通过（绝不显示 SUCCESS）
EXIT_CANNOT_RUN = 3      # 前置条件不满足（没窗口 / 状态不可确认 / 已在暂停）

DEFAULT_REGION_SCALE = 0.035    # manual / screen_point 模式下的目标区域半径（相对窗口宽高）


# ====================================================================== 窗口身份
def window_identity(win) -> dict:
    """规格书要求的那一组元数据：HWND / 身份 / 宽高 / DPI。"""
    w, h = win.size
    return {
        "hwnd": int(win.hwnd),
        "hwnd_hex": hex(int(win.hwnd)),
        "title": win.title,
        "class_name": win.class_name,
        "pid": win.pid,
        "process_name": win.process_name,
        "exe_path": win.exe_path,
        "client_version": win.version,
        "window_width": w,
        "window_height": h,
        "window_rect": list(win.rect) if win.rect else None,
        "dpi": win.dpi,
        "dpi_scale": round(win.dpi / 96.0, 3),
    }


# ====================================================================== 候选点
def _relative_from_abs(win, rect_abs: tuple) -> dict:
    """把屏幕绝对矩形换算成「相对窗口」的 pos / ratio（点与区域都给）。"""
    wl, wt_, wr, wb = win.rect
    w, h = max(1, wr - wl), max(1, wb - wt_)
    l, t, r, b = rect_abs
    cx, cy = (l + r) // 2, (t + b) // 2
    return {
        "pos": [int(l - wl), int(t - wt_), int(r - wl), int(b - wt_)],
        "ratio": [round((l - wl) / w, 6), round((t - wt_) / h, 6),
                  round((r - wl) / w, 6), round((b - wt_) / h, 6)],
        "center_pos": [int(cx - wl), int(cy - wt_)],
        "center_ratio": [round((cx - wl) / w, 6), round((cy - wt_) / h, 6)],
    }


def candidate_from_uia(win) -> dict | None:
    """由 UIA 的按钮包围盒反推坐标。找不到返回 None。"""
    res = uia.find_duration_controls(win.hwnd)
    pause, start = res["pause"], res["start"]
    if pause and start:
        # 两个按钮同时存在本来就不正常，宁可放弃（上层会判 UNKNOWN）
        return None
    ctrl = (pause or start or [None])[0]
    if ctrl is None or not ctrl.rect:
        return None
    rel = _relative_from_abs(win, tuple(ctrl.rect))
    rel.update({"source": "ui_automation", "control_name": ctrl.name,
                "control_type": ctrl.control_type,
                "control_rect_abs": list(ctrl.rect),
                "control_supports_invoke": bool(ctrl.supports_invoke)})
    return rel


def candidate_from_screen_point(win, sx: int, sy: int,
                                region_scale: float = DEFAULT_REGION_SCALE) -> dict:
    """由显式屏幕点构造候选（目标区域取点周围的一个小方框）。

    若该点下正好压着一个属于雷神窗口的子窗口（原生控件界面，例如
    仿雷神靶机的 BUTTON），就用那个子窗口的真实矩形当目标区域——
    比「点周围一个方框」准确得多。Electron 版雷神只有一个大 HWND，
    此时自动退回方框估算。
    """
    abs_rect = (sx - max(4, int(win.size[0] * region_scale)),
                sy - max(4, int(win.size[1] * region_scale)),
                sx + max(4, int(win.size[0] * region_scale)),
                sy + max(4, int(win.size[1] * region_scale)))
    h = cf.window_from_point(sx, sy)
    if h:
        parent = user32.GetParent(wt.HWND(int(h)))
        if int(parent or 0) == int(win.hwnd):
            abs_rect = cf.get_window_rect(h)
    rel = _relative_from_abs(win, abs_rect)
    rel.update({"source": "screen_point", "sampled_screen_point": [int(sx), int(sy)]})
    return rel


def candidate_from_manual(win, delay: float = 5.0, verbose: bool = True) -> dict | None:
    """倒计时取样鼠标位置。

    不做「按键触发」是因为本工具常在无控制台的上下文里跑（计划任务/自动化）。
    倒计时对两种情形都成立，且逼着用户把鼠标真的停在按钮上。
    """
    if verbose:
        print(f"\n== 手动取样 ==\n  请把鼠标移到雷神顶栏「暂停时长」按钮的**正中央**，"
              f"保持不动…", flush=True)
    end = time.time() + max(1.0, delay)
    while time.time() < end:
        left = end - time.time()
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        if verbose:
            sys.stdout.write(f"\r  {left:4.1f}s  当前鼠标 ({pt.x}, {pt.y})   ")
            sys.stdout.flush()
        time.sleep(0.2)
    if verbose:
        sys.stdout.write("\n")
    pt = wt.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    if not cf.is_point_over_window(win.hwnd, pt.x, pt.y):
        if verbose:
            print(f"  ✘ 取样点 ({pt.x}, {pt.y}) 不在雷神窗口上，放弃", flush=True)
        return None
    return candidate_from_screen_point(win, pt.x, pt.y)


# ====================================================================== 点击点
def click_point_of(win, cand: dict, rect: tuple = None) -> tuple:
    """按**当前**窗口矩形把相对坐标换算成屏幕点。

    每一轮尝试都重新调用：窗口一旦被移动/缩放，绝对值立刻失效，
    而 ratio 仍然对准——这正是「禁止保存绝对屏幕坐标」的意义。
    """
    rect = rect or cf.get_window_rect(win.hwnd)
    # 优先用 ratio（窗口缩放后仍对准）；没有 ratio 才退回 pos
    return cf.compute_click_point(rect, ratio=cand.get("center_ratio"),
                                  pos=cand.get("center_pos"))


def region_px_now(win, cand: dict, rect: tuple = None) -> tuple:
    """按当前矩形算出目标区域的屏幕像素范围（用于画标记/汇报）。"""
    rect = rect or cf.get_window_rect(win.hwnd)
    l, t, r, b = rect
    w, h = max(1, r - l), max(1, b - t)
    rl, rt, rr, rb = cand["ratio"]
    return (int(l + w * rl), int(t + h * rt), int(l + w * rr), int(t + h * rb))


# ====================================================================== 验证
def _wait_until_paused(detector, win, deadline: float, settle: float = 0.4):
    """点击后反复识别，直到 PAUSED 或超时。返回最后一次 Reading。"""
    if settle > 0:
        time.sleep(settle)
    last = None
    while True:
        last = detector.detect(win, allow_ocr=True)
        if last.state is DurationState.PAUSED:
            return last
        if time.time() >= deadline:
            return last
        time.sleep(0.3)


def verify(win, cfg, cand: dict, detector, clicker, logger=None,
           max_attempts: int = 3, interval: float = 1.2, timeout: float = 3.0,
           verbose: bool = True) -> dict:
    """真实测试：点击校准出的相对坐标 → 重新检测 → 是否变成 PAUSED。

    返回 dict：{verified, attempts, reason, records[], point, region, hit}
    绝不出现「点击成功即成功」这种判断——只有识别到 PAUSED 才算 verified。
    """
    out = {"verified": False, "attempts": 0, "reason": "", "records": [],
           "point": None, "region": None, "hit": None}
    hold = int(cfg.get("duration.coordinate.hold_ms", 120))

    for attempt in range(1, max(1, max_attempts) + 1):
        out["attempts"] = attempt

        # —— 重试前重新读状态：上一次可能已经点成功了，只是验证抖动 ——
        if attempt > 1:
            time.sleep(max(0.0, interval))
            pre = detector.detect(win, allow_ocr=True)
            if pre.state is DurationState.PAUSED:
                out.update({"verified": True,
                            "reason": f"第 {attempt - 1} 次点击实际已生效（本次重新读状态确认）"})
                return out
            if pre.state is not DurationState.RUNNING:
                out["reason"] = (f"重试前无法确认状态（识别为 {pre.state.value}），"
                                 f"停止点击（Fail Safe）")
                return out

        rect = cf.ensure_visible(win.hwnd)
        if rect[0] < -20000:
            out["reason"] = "雷神窗口仍在屏幕外（托盘态），无法点击"
            return out
        cf.activate_window(win.hwnd)
        rect = cf.get_window_rect(win.hwnd)

        x, y = click_point_of(win, cand, rect)
        region = region_px_now(win, cand, rect)
        hit = cf.hit_test(win.hwnd, x, y)
        over = cf.is_point_over_window(win.hwnd, x, y)
        out.update({"point": [x, y], "region": list(region), "hit": hit})

        rec = {"attempt": attempt, "screen_point": [x, y], "rect": list(rect),
               "hit_test": hit, "over_window": bool(over)}
        if verbose:
            print(f"  第 {attempt} 次：点击屏幕 ({x}, {y})  "
                  f"窗口矩形={tuple(rect)}  命中码={hit}  压在本窗口上={over}", flush=True)
        if logger:
            logger.info("校准测试第 %d 次点击 (%d,%d) rect=%s hit=%d over=%s",
                        attempt, x, y, tuple(rect), hit, over)

        clicker(x, y)

        deadline = time.time() + max(0.5, timeout)
        reading = _wait_until_paused(detector, win, deadline, settle=0.4)
        rec["state_after"] = reading.state.value
        rec["detail"] = reading.summary()
        out["records"].append(rec)
        if verbose:
            print(f"            点击后识别 → {reading.state.value}（{reading.summary()}）", flush=True)

        if reading.state is DurationState.PAUSED:
            out.update({"verified": True,
                        "reason": f"第 {attempt} 次点击后识别到「开启时长」，已确认为 PAUSED"})
            return out
        if reading.state is DurationState.UNKNOWN:
            out["reason"] = ("点击后无法确认状态（识别为 UNKNOWN），"
                             "停止继续点击（Fail Safe：不确定时再点是危险的）")
            return out

    out["reason"] = f"已尝试 {out['attempts']} 次，点击后始终未能确认为 PAUSED"
    return out


# ====================================================================== 标记图
def save_marked_png(win, region: tuple, point: tuple, path: str) -> bool:
    """把窗口截图 + 目标区域框 + 实际点击点存成图片，失败时最有用的一张证据。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return False
    cap = cf.capture_window_printwindow(win.hwnd)
    if cap is None:
        return False
    try:
        img = Image.fromarray(cap)
        d = ImageDraw.Draw(img)
        l, t, r, b = win.rect
        if region:
            d.rectangle([region[0] - l, region[1] - t, region[2] - l, region[3] - t],
                        outline=(0, 200, 255), width=2)
        if point:
            px, py = point[0] - l, point[1] - t
            d.line([px - 12, py, px + 12, py], fill=(255, 60, 60), width=2)
            d.line([px, py - 12, px, py + 12], fill=(255, 60, 60), width=2)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        img.save(path)
        return True
    except Exception:
        return False


# ====================================================================== 主流程
def _default_clicker(hold_ms: int):
    def _click(x, y):
        cf.press_click(x, y, hold_ms)
    return _click


def run(out_dir: str, config=None, mode: str = "auto", point=None,
        delay: float = 5.0, clicker=None, logger=None, verbose: bool = True,
        restore: bool = False) -> dict:
    """执行一次完整校准。返回报告 dict（同时写入 out_dir/calibration.json）。"""
    cf.set_dpi_aware()
    cfg = config or load_config()
    os.makedirs(out_dir, exist_ok=True)

    report = {"ts": time.time(),
              "ts_str": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "mode": mode, "out_dir": out_dir, "status": "CANNOT_RUN"}

    win = win_mod.find_main_window(cfg)
    if win is None:
        report["reason"] = "未找到雷神主窗口：请先启动雷神客户端再重试"
        _finish(report, out_dir, verbose)
        return report

    ident = window_identity(win)
    report["window"] = ident
    if verbose:
        print("== 目标窗口 ==", flush=True)
        for k, label in (("hwnd_hex", "HWND"), ("title", "标题"), ("class_name", "类名"),
                         ("pid", "PID"), ("process_name", "进程"), ("exe_path", "路径"),
                         ("client_version", "客户端版本")):
            print(f"  {label:<12}: {ident[k]}", flush=True)
        print(f"  宽 x 高     : {ident['window_width']} x {ident['window_height']}", flush=True)
        print(f"  DPI         : {ident['dpi']}（缩放 {ident['dpi_scale']:.0%}）", flush=True)
        print(f"  WindowRect  : {ident['window_rect']}", flush=True)

    detector = DurationDetector(cfg, logger)

    # ---------- 前置条件：必须是 RUNNING，否则测试无法成立 ----------
    before = detector.detect(win, allow_ocr=True)
    report["state_before"] = {"state": before.state.value, "detail": before.summary()}
    if verbose:
        print(f"\n== 当前状态 ==\n  {before.state.value} — {before.summary()}", flush=True)
    if before.state is DurationState.UNKNOWN:
        report["reason"] = ("当前无法确认雷神总时长状态，校准测试无法进行。"
                            "请先运行 diagnostics/ui_inspector 排障（此状态下本工具不做任何点击）")
        _finish(report, out_dir, verbose)
        return report
    if before.state is DurationState.PAUSED:
        report["reason"] = ("雷神总时长当前处于 PAUSED，无法验证「点击后变成 PAUSED」。"
                            "请先在雷神里点一次「开启时长」让计时开始，再重新校准")
        _finish(report, out_dir, verbose)
        return report

    # ---------- 求候选点 ----------
    cand = None
    if mode == "auto":
        cand = candidate_from_uia(win)
        if cand is None and verbose:
            print("\n  （UIA 找不到目标按钮，退化到手动取样）", flush=True)
        if cand is None:
            cand = candidate_from_manual(win, delay=delay, verbose=verbose)
    elif mode == "manual":
        cand = candidate_from_manual(win, delay=delay, verbose=verbose)
    elif mode == "point":
        if not point or len(point) != 2:
            report["reason"] = "mode=point 需要 --point X,Y"
            _finish(report, out_dir, verbose)
            return report
        cand = candidate_from_screen_point(win, int(point[0]), int(point[1]))
    else:
        report["reason"] = f"未知模式: {mode}"
        _finish(report, out_dir, verbose)
        return report

    if cand is None:
        report["reason"] = ("无法确定暂停按钮位置（UIA 未找到目标按钮，且手动取样失败）。"
                            "请先运行 diagnostics/ui_inspector 查看控件树")
        _finish(report, out_dir, verbose)
        return report

    report["candidate"] = cand
    if verbose:
        print(f"\n== 候选坐标（来源 {cand['source']}）==", flush=True)
        print(f"  中心 ratio  : {cand['center_ratio']}", flush=True)
        print(f"  中心 pos    : {cand['center_pos']}", flush=True)
        print(f"  目标区域 ratio: {cand['ratio']}", flush=True)
        if cand.get("control_name"):
            print(f"  命中控件    : {cand['control_name']!r} ({cand['control_type']}) "
                  f"rect={cand['control_rect_abs']}", flush=True)

    # ---------- 真实测试（规格书 §二十五 的核心） ----------
    hold = int(cfg.get("duration.coordinate.hold_ms", 120))
    if verbose:
        print(f"\n== 真实点击测试 ==", flush=True)
    res = verify(win, cfg, cand, detector, clicker or _default_clicker(hold), logger,
                 max_attempts=max(1, int(cfg.get("duration.max_retries", 3))),
                 interval=max(0.0, int(cfg.get("duration.retry_interval_ms", 1500)) / 1000.0),
                 timeout=max(0.5, int(cfg.get("duration.verify_timeout_ms", 3000)) / 1000.0),
                 verbose=verbose)
    report["test"] = res
    report["state_after"] = (res["records"][-1]["state_after"] if res["records"] else None)

    window_id = window_identity(win_mod.refresh(win))
    report["window_after"] = window_id

    marked = os.path.join(out_dir, "calibration_point.png")
    report["marked_png"] = marked if save_marked_png(win, res.get("region"), res.get("point"), marked) else None

    if not res["verified"]:
        # 绝不写配置、绝不显示 SUCCESS
        report["status"] = "FAILED"
        report["reason"] = res["reason"]
        if verbose:
            print(f"\n  ✘ Calibration FAILED — {res['reason']}", flush=True)
            print("    已保留原有校准结果不变（不会用一次失败的校准覆盖可用配置）。", flush=True)
            if report["marked_png"]:
                print(f"    标记图: {report['marked_png']}（红叉=实际点击点，蓝框=目标区域）", flush=True)
        _finish(report, out_dir, verbose)
        return report

    # ---------- 成功：写入配置 ----------
    ratio = cand["center_ratio"]
    pos = cand["center_pos"]
    cal = {
        # 规格书 §二十五 要求必须包含的字段
        "hwnd": window_id["hwnd"], "hwnd_hex": window_id["hwnd_hex"],
        "title": window_id["title"], "class_name": window_id["class_name"],
        "pid": window_id["pid"], "process_name": window_id["process_name"],
        "exe_path": window_id["exe_path"], "client_version": window_id["client_version"],
        "window_width": window_id["window_width"], "window_height": window_id["window_height"],
        "dpi": window_id["dpi"], "dpi_scale": window_id["dpi_scale"],
        "relative_ratio": ratio, "relative_pos": pos,
        "target_region_ratio": cand["ratio"], "target_region_pos": cand["pos"],
        "source": cand["source"],
        "hit_test": res.get("hit"),
        # 验证结论
        "verified": True,
        "verified_state": "PAUSED",
        "verified_attempts": res["attempts"],
        "verified_detail": res["reason"],
        "ts": report["ts"], "ts_str": report["ts_str"],
    }
    cfg.set("duration.coordinate.ratio", ratio, save=False)
    cfg.set("duration.coordinate.pos", pos, save=False)
    cfg.set("duration.coordinate.hold_ms", hold, save=False)
    cfg.set("duration.coordinate.calibration", cal, save=False)
    cfg.save()
    report["calibration"] = cal
    report["status"] = "SUCCESS"

    if verbose:
        print("\n  ✔ Calibration SUCCESS", flush=True)
        print(f"    相对坐标 ratio = {ratio}   pos = {pos}", flush=True)
        print(f"    目标区域       = {cand['ratio']}（相对窗口）", flush=True)
        print(f"    依据窗口       = {window_id['hwnd_hex']} {window_id['class_name']} "
              f"{window_id['window_width']}x{window_id['window_height']} "
              f"@{window_id['dpi']}dpi", flush=True)
        print(f"    已写入         = {cfg.path}", flush=True)
        print("    当前时长处于「已暂停」（这正是本次测试的结果）。", flush=True)

    # ---------- 可选：恢复计时 ----------
    if restore:
        if verbose:
            print("\n== 恢复计时（--restore）==", flush=True)
        rect = cf.get_window_rect(win.hwnd)
        x, y = click_point_of(win, cand, rect)
        (clicker or _default_clicker(hold))(x, y)
        reading = _wait_until_paused(detector, win, time.time() + 3.0, settle=0.4)
        report["restore"] = {"screen_point": [x, y], "state_after": reading.state.value,
                             "detail": reading.summary()}
        if verbose:
            print(f"  恢复后状态 → {reading.state.value}（{reading.summary()}）", flush=True)
            if reading.state is not DurationState.RUNNING:
                print("  ⚠ 未能确认已恢复计时，请到雷神界面手动点一次「开启时长」。", flush=True)

    _finish(report, out_dir, verbose)
    return report


def _finish(report: dict, out_dir: str, verbose: bool) -> None:
    path = os.path.join(out_dir, "calibration.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        if verbose:
            print(f"\n校准报告已写入 {path}", flush=True)
    except OSError as e:
        if verbose:
            print(f"\n写入校准报告失败: {e}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="雷神暂停按钮坐标校准（校准后立即做真实点击测试，未通过不写配置）")
    ap.add_argument("--mode", default="auto", choices=["auto", "manual", "point"],
                    help="auto=UIA 定位（失败退化为 manual）；manual=手动取样；point=显式屏幕点")
    ap.add_argument("--point", default=None, help="mode=point 时的屏幕坐标，形如 1200,180")
    ap.add_argument("--delay", type=float, default=5.0, help="手动取样的倒计时秒数（默认 5）")
    ap.add_argument("--out", default=os.path.join(base_dir(), "logs", "calibration"),
                    help="产物目录（calibration.json / calibration_point.png）")
    ap.add_argument("--restore", action="store_true",
                    help="校准成功后点回去恢复计时（注意：会重新开始消耗时长）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    logger = setup_logging("INFO", console=False)
    point = None
    if a.point:
        try:
            point = [int(v) for v in a.point.replace("，", ",").split(",")]
        except ValueError:
            print(f"--point 格式错误: {a.point}（应为 X,Y）", flush=True)
            return EXIT_CANNOT_RUN

    rep = run(a.out, mode=a.mode, point=point, delay=a.delay, logger=logger,
              verbose=not a.quiet, restore=a.restore)
    st = rep.get("status")
    if st == "SUCCESS":
        return EXIT_OK
    if st == "FAILED":
        return EXIT_FAILED
    if not a.quiet:
        print(f"\n无法执行校准：{rep.get('reason')}", flush=True)
    return EXIT_CANNOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())
