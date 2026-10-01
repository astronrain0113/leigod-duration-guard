"""坐标校准的自动化验收（对应规格书 §二十五）。

被验证的硬要求：
  1. 校准**必须**包含一次真实点击测试；只有重新识别到 PAUSED 才算成功；
  2. 测试未通过时：状态为 FAILED、退出码 2、**绝不写配置**、绝不显示 SUCCESS；
  3. 状态为 UNKNOWN 或已 PAUSED 时：不做任何点击，退出码 3（Fail Safe）；
  4. 校准产物必须含 HWND / 窗口身份 / 宽高 / DPI / 相对坐标 / 目标区域。

关于「真实点击」的执行者——本机执行环境禁止合成鼠标输入（见
tests/out/synthetic_input_report.txt：SetCursorPos 返回 False、mouse_event 无效果），
因此本测试向校准工具注入一个 clicker，它**只把落在靶机按钮子窗口上的点**转成
BM_CLICK。关键点在于：坐标是校准工具按相对比例算出来的真实屏幕坐标，
命中判定走真实的 WindowFromPoint，只有当点确实压在按钮窗口上才可能通过，
所以「坐标算错了」一定失败。被替代的只有「内核把硬件事件派发给目标窗口」这一段，
报告里如实标注。

运行：python tests/test_calibration.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness as H                                       # noqa: E402
from detection import coordinate_fallback as cf           # noqa: E402
from diagnostics import calibration as calib              # noqa: E402

user32 = ctypes.windll.user32
BM_CLICK = 0x00F5


# --------------------------------------------------------------- 测试替身
def child_button(hwnd, timeout: float = 10.0):
    """返回靶机内的按钮子窗口 (hwnd, class, rect)，没有则 None。

    必须**轮询等待**：`start_mock` 只保证顶层窗口已出现，子窗口（按钮）
    可能晚几十毫秒才创建。早期写法枚举一次即返回，在批量跑（机器繁忙）时
    会偶发拿到 None，表现为「找到靶机按钮子窗口 — None」这种假失败。
    """
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(h, buf, 128)
        if buf.value.lower() == "button":
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            found.append((int(h), buf.value, (r.left, r.top, r.right, r.bottom)))
        return True

    end = time.time() + timeout
    while time.time() < end:
        found.clear()
        user32.EnumChildWindows(wt.HWND(int(hwnd)), cb, 0)
        if found:
            return found[0]
        time.sleep(0.1)
    return None


class PointClicker:
    """把「落在靶机按钮上的点」转成 BM_CLICK 的替身点击器。

    记录每一次调用与是否命中按钮，便于断言坐标是否算对。
    """

    def __init__(self, mock_hwnd):
        self.mock_hwnd = int(mock_hwnd)
        self.calls = []
        self.hits = 0

    def __call__(self, x, y):
        h = cf.window_from_point(x, y)
        cls = ""
        if h:
            buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wt.HWND(int(h)), buf, 128)
            cls = buf.value
            root = user32.GetAncestor(wt.HWND(int(h)), 2)
        else:
            root = 0
        hit = (cls.lower() == "button"
               and int(user32.GetParent(wt.HWND(int(h))) or 0) == self.mock_hwnd)
        self.calls.append({"point": [int(x), int(y)], "hwnd": int(h) if h else 0,
                           "class": cls, "over_button": bool(hit)})
        if hit:
            self.hits += 1
            user32.SendMessageW(wt.HWND(int(h)), BM_CLICK, 0, 0)
        return bool(hit)


def fresh_config(name: str):
    cfg = H.test_config()
    cfg.path = os.path.join(H.OUT, name)
    return cfg


def read_cfg_file(path: str) -> dict:
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------- 用例
def case_success(rep: H.Report) -> None:
    rep.section("用例 1：UIA 定位 → 真实点击测试 → 必须变成 PAUSED 才算 SUCCESS")
    proc, hwnd = H.start_mock("RUNNING")
    try:
        cfg = fresh_config("cal_success.json")
        clicker = PointClicker(hwnd)
        out = os.path.join(H.OUT, "cal")
        res = calib.run(out, config=cfg, mode="auto", clicker=clicker, verbose=False)

        rep.check("校准状态为 SUCCESS", res.get("status") == "SUCCESS",
                  str(res.get("reason")))
        rep.check("靶机状态确实变为 PAUSED（不是「点击成功」就报成功）",
                  H.read_mock_state() == "PAUSED", f"靶机={H.read_mock_state()}")
        rep.check("确实执行了真实点击（替身记录到调用）", len(clicker.calls) >= 1,
                  f"{clicker.calls}")
        rep.check("点击点确实压在暂停按钮上", clicker.hits >= 1,
                  f"{clicker.calls[:1]}")

        cal = res.get("calibration") or {}
        for key in ("hwnd", "class_name", "window_width", "window_height", "dpi",
                    "relative_ratio", "target_region_ratio"):
            rep.check(f"校准产物含 {key}", cal.get(key) is not None, repr(cal.get(key)))
        rep.check("校准产物标记 verified=True", cal.get("verified") is True)
        rep.check("验证结论为 PAUSED", cal.get("verified_state") == "PAUSED")
        # 台账：窗口身份必须是真值而不是猜的
        rep.check("HWND 与靶机一致", int(cal.get("hwnd", 0)) == int(thumb(hwnd)),
                  f"{cal.get('hwnd')} vs {thumb(hwnd)}")
        rep.check("窗口尺寸与真实窗口一致",
                  (cal.get("window_width"), cal.get("window_height")) == cf_size(hwnd),
                  f"{cal.get('window_width')}x{cal.get('window_height')} vs {cf_size(hwnd)}")

        # 相对坐标必须落在靶机按钮的真实比例范围内（按钮 x=0.700W 起、宽 104）
        ratio = cal.get("relative_ratio") or [0, 0]
        rep.check("相对坐标在合理范围内（约 x≈0.74, y≈0.10）",
                  0.70 <= ratio[0] <= 0.82 and 0.03 <= ratio[1] <= 0.16, f"ratio={ratio}")
        pos = cal.get("relative_pos") or [0, 0]
        rep.check("像素相对坐标也写了（pos）", isinstance(pos, list) and len(pos) == 2,
                  f"pos={pos}")

        # 落盘
        data = read_cfg_file(cfg.path)
        node = data["duration"]["coordinate"]
        rep.check("配置已写入 ratio", node.get("ratio") == cal.get("relative_ratio"),
                  str(node.get("ratio")))
        rep.check("配置已写入 calibration 元数据",
                  (node.get("calibration") or {}).get("verified") is True)
        rep.check("配置里没有出现绝对屏幕坐标（禁止项）",
                  "screen" not in str(node.get("calibration", {})).replace("screen_point", ""))
    finally:
        proc.terminate()


def case_failed_keeps_old(rep: H.Report) -> None:
    rep.section("用例 2：点击后状态不变 → FAILED，且不得覆盖已有的可用校准")
    proc, hwnd = H.start_mock("RUNNING", label="暂停时长")   # 文案可识别但点击无效
    try:
        # 复用同一个配置文件：先写一份「已验证」的假校准，再跑一次注定失败的校准
        cfg_path = os.path.join(H.OUT, "cal_keep.json")
        cfg = fresh_config("cal_keep.json")
        cfg.set("duration.coordinate.ratio", [0.7444, 0.0986], save=False)
        cfg.set("duration.coordinate.calibration", {"verified": True, "marker": "OLD"}, save=True)

        clicker = PointClicker(hwnd)
        res = calib.run(os.path.join(H.OUT, "cal_fail"), config=cfg, mode="auto",
                        clicker=clicker, verbose=False)
        rep.check("校准状态为 FAILED", res.get("status") == "FAILED", str(res.get("reason")))
        rep.check("明确说明未通过的原因", bool(res.get("reason")), str(res.get("reason")))
        rep.check("重试到了配置上限（3 次）",
                  (res.get("test") or {}).get("attempts", 0) == 3,
                  str((res.get("test") or {}).get("attempts")))
        rep.check("没有任何一次被误判为成功",
                  (res.get("test") or {}).get("verified") is False)

        data = read_cfg_file(cfg_path)
        node = data["duration"]["coordinate"]
        rep.check("旧校准结果未被覆盖（marker 仍为 OLD）",
                  (node.get("calibration") or {}).get("marker") == "OLD",
                  str(node.get("calibration")))
        rep.check("旧 ratio 保持不变", node.get("ratio") == [0.7444, 0.0986],
                  str(node.get("ratio")))
    finally:
        proc.terminate()


def case_unknown_no_click(rep: H.Report) -> None:
    rep.section("用例 3：状态 UNKNOWN → 不做任何点击，拒绝校准（Fail Safe）")
    proc, hwnd = H.start_mock("RUNNING", label="雷神加速器")
    try:
        cfg = fresh_config("cal_unknown.json")
        cfg.set("detection.ocr_enabled", False, save=False)   # 去掉 OCR，保证只靠 UIA
        cfg.save()
        clicker = PointClicker(hwnd)
        res = calib.run(os.path.join(H.OUT, "cal_unknown"), config=cfg, mode="auto",
                        clicker=clicker, verbose=False)
        rep.check("拒绝执行校准", res.get("status") == "CANNOT_RUN", str(res.get("status")))
        rep.check("说明了原因", "无法确认" in str(res.get("reason")), str(res.get("reason")))
        rep.check("没有执行任何点击", len(clicker.calls) == 0, str(clicker.calls))
        rep.check("靶机状态未被改动", H.read_mock_state() == "RUNNING")
    finally:
        proc.terminate()


def case_paused_refuses(rep: H.Report) -> None:
    rep.section("用例 4：当前已 PAUSED → 无法验证 RUNNING→PAUSED，拒绝校准")
    proc, hwnd = H.start_mock("PAUSED")
    try:
        cfg = fresh_config("cal_paused.json")
        clicker = PointClicker(hwnd)
        res = calib.run(os.path.join(H.OUT, "cal_paused"), config=cfg, mode="auto",
                        clicker=clicker, verbose=False)
        rep.check("拒绝执行校准", res.get("status") == "CANNOT_RUN", str(res.get("status")))
        rep.check("提示需要先恢复计时", "开启时长" in str(res.get("reason")),
                  str(res.get("reason")))
        rep.check("没有执行任何点击", len(clicker.calls) == 0)
    finally:
        proc.terminate()


def case_point_mode(rep: H.Report) -> None:
    rep.section("用例 5：point 模式（手工/显式点）同样必须通过真实测试")
    proc, hwnd = H.start_mock("RUNNING")
    try:
        btn = child_button(hwnd)
        rep.check("找到靶机按钮子窗口", btn is not None, str(btn))
        if not btn:
            return
        r = btn[2]
        cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        cfg = fresh_config("cal_point.json")
        clicker = PointClicker(hwnd)
        res = calib.run(os.path.join(H.OUT, "cal_point"), config=cfg, mode="point",
                        point=[cx, cy], clicker=clicker, verbose=False)
        rep.check("校准状态为 SUCCESS", res.get("status") == "SUCCESS",
                  str(res.get("reason")))
        rep.check("靶机状态变为 PAUSED", H.read_mock_state() == "PAUSED")
        rep.check("来源标记为 screen_point",
                  (res.get("candidate") or {}).get("source") == "screen_point",
                  str((res.get("candidate") or {}).get("source")))
        rep.check("目标区域已计算", bool((res.get("calibration") or {}).get("target_region_ratio")),
                  str((res.get("calibration") or {}).get("target_region_ratio")))
    finally:
        proc.terminate()


def case_wrong_point_fails(rep: H.Report) -> None:
    rep.section("用例 6：故意给错误的点 → 必须 FAILED（不许假装成功）")
    proc, hwnd = H.start_mock("RUNNING")
    try:
        r = wt.RECT()
        user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r))
        bad = (r.left + 40, r.bottom - 40)      # 窗口左下角，离按钮十万八千里
        cfg = fresh_config("cal_bad.json")
        cfg.set("duration.verify_timeout_ms", 800, save=False)
        cfg.set("duration.retry_interval_ms", 300, save=False)
        cfg.save()
        clicker = PointClicker(hwnd)
        res = calib.run(os.path.join(H.OUT, "cal_bad"), config=cfg, mode="point",
                        point=[bad[0], bad[1]], clicker=clicker, verbose=False)
        rep.check("校准状态为 FAILED", res.get("status") == "FAILED", str(res.get("status")))
        rep.check("没有写配置里的 ratio",
                  read_cfg_file(cfg.path)["duration"]["coordinate"].get("ratio") is None,
                  str(read_cfg_file(cfg.path)["duration"]["coordinate"].get("ratio")))
        rep.check("靶机状态未被改动", H.read_mock_state() == "RUNNING")
        rep.check("替身点击一次都没命中按钮", clicker.hits == 0, str(clicker.calls))
    finally:
        proc.terminate()


# --------------------------------------------------------------- 小工具
def thumb(hwnd) -> int:
    return int(hwnd)


def cf_size(hwnd) -> tuple:
    rect = cf.get_window_rect(hwnd)
    return (rect[2] - rect[0], rect[3] - rect[1])


def main() -> int:
    from core.logging_setup import setup_logging
    setup_logging("INFO", console=False)
    rep = H.Report("雷神暂停按钮坐标校准 —— 自动化验收（针对仿雷神靶机）")
    print(rep.title, flush=True)
    H.kill_stale_mocks()
    case_success(rep)
    case_failed_keeps_old(rep)
    case_unknown_no_click(rep)
    case_paused_refuses(rep)
    case_point_mode(rep)
    case_wrong_point_fails(rep)
    path = rep.save("calibration_report.txt")
    fails = sum(1 for l in rep.lines if "[FAIL]" in l)
    print(f"\n结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}；报告 {path}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
