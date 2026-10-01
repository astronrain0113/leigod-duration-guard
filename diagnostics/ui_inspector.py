"""Leigod UI Inspector（规格书 §二十四）。

这是整个项目最重要的开发/排障工具：雷神每次更新都可能改 UI 结构，
一旦「开启时长 / 暂停时长」认不出来，就必须靠它快速定位到底哪里变了。

输出内容：
  Leigod PID / HWND / Window Title / ClassName / Process Path / Window Rect /
  Width / Height / DPI / 显示器 / 客户端版本
  UI Automation 控件树（Name / ControlType / AutomationId / BoundingRectangle /
  IsEnabled / 是否支持 Invoke）
  目标按钮（开启时长 / 暂停时长）的定位结果
  截图：leigod_full.png、duration_region.png
  OCR：recognized_text / confidence / bounding_box → ocr.json
  最终结论：DurationState = RUNNING / PAUSED / UNKNOWN

用法：
  python -m diagnostics.ui_inspector                 # 全量诊断
  python -m diagnostics.ui_inspector --out tests/out # 指定产物目录
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core.config import load_config                      # noqa: E402
from core.logging_setup import setup_logging              # noqa: E402
from core.paths import base_dir                            # noqa: E402
from core.state_machine import DurationState              # noqa: E402
from detection import coordinate_fallback as cf           # noqa: E402
from detection import image_detection as img              # noqa: E402
from detection import ocr as ocr_mod                      # noqa: E402
from detection import ui_automation as uia                # noqa: E402
from diagnostics import window_inspector as wi            # noqa: E402
from leigod.duration_detector import DurationDetector     # noqa: E402


def _save_png(arr, path: str) -> bool:
    if arr is None:
        return False
    try:
        from PIL import Image
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        Image.fromarray(arr).save(path)
        return True
    except Exception:
        return False


def run(out_dir: str, config=None, verbose: bool = True) -> dict:
    cf.set_dpi_aware()
    cfg = config or load_config()
    os.makedirs(out_dir, exist_ok=True)

    report = {"ts": time.time(), "out_dir": out_dir}
    wdata = wi.inspect(cfg)
    report["window"] = wdata
    if verbose:
        print(wi.render(wdata), flush=True)

    win = None
    if wdata.get("window"):
        from leigod.window import LeigodWindow
        win = LeigodWindow(**{k: wdata["window"][k] for k in (
            "hwnd", "title", "class_name", "pid", "process_name", "exe_path",
            "version", "rect", "frame", "client", "dpi", "monitor",
            "minimized_to_tray")})
        report["window"]["picked"] = win.as_dict()

    if win is None:
        msg = "未找到雷神主窗口：请先启动雷神客户端再重试"
        print("\n" + msg, flush=True)
        report["result"] = {"state": DurationState.UNKNOWN.value, "reason": msg}
        _write(report, out_dir)
        return report

    # ---------------- UI Automation ----------------
    print("\n== UI Automation 控件树 ==", flush=True)
    controls = uia.dump_tree(win.hwnd, max_depth=14)
    report["ui_automation"] = {"control_count": len(controls), "controls": controls}
    if verbose:
        for c in controls[:80]:
            print(f"  {c['control_type']:<18} name={c['name']!r:<28} "
                  f"id={c['automation_id']!r:<12} rect={c['rect']} "
                  f"enabled={c['is_enabled']} invoke={c['supports_invoke']}", flush=True)
        if len(controls) > 80:
            print(f"  ... 另有 {len(controls) - 80} 个控件（完整清单见 inspector.json）", flush=True)

    found = uia.find_duration_controls(win.hwnd)
    report["duration_controls"] = {
        "pause": [c.as_dict() for c in found["pause"]],
        "start": [c.as_dict() for c in found["start"]],
    }
    print("\n== 目标按钮定位 ==", flush=True)
    for key, label in (("pause", "暂停时长(计时中)"), ("start", "开启时长(已暂停)")):
        items = found[key]
        if items:
            for c in items:
                print(f"  ✔ {label}: name={c.name!r} type={c.control_type} "
                      f"rect={c.rect} enabled={c.is_enabled} invoke={c.supports_invoke}",
                      flush=True)
        else:
            print(f"  ✘ 未找到 {label}", flush=True)

    # ---------------- 截图 + OCR ----------------
    detector = DurationDetector(cfg)
    cap = detector.capture(win)
    report["capture"] = {"ok": cap.ok, "method": cap.method, "detail": cap.detail}
    print(f"\n== 截图 ==\n  {cap.method}: {'成功' if cap.ok else '失败'} — {cap.detail}", flush=True)
    full_png = os.path.join(out_dir, "leigod_full.png")
    region_png = os.path.join(out_dir, "duration_region.png")
    region = None
    if cap.ok:
        _save_png(cap.array, full_png)
        crop_cfg = cfg.get("detection.topbar_crop")
        region = img.crop_relative(cap.array, win.rect, crop_cfg)
        _save_png(region, region_png)
        print(f"  已保存 {full_png}\n  已保存 {region_png}（顶栏裁剪区域 {crop_cfg}）", flush=True)

    lines = ocr_mod.read_text(region) if region is not None else []
    report["ocr"] = {"engine_available": ocr_mod.engine_available(), "lines": lines}
    print("\n== OCR ==", flush=True)
    if not ocr_mod.engine_available():
        print("  OCR 引擎不可用（未安装 rapidocr-onnxruntime）", flush=True)
    elif not lines:
        print("  没有识别到文字", flush=True)
    for l in lines:
        print(f"  text={l['text']!r:<22} confidence={l['confidence']} "
              f"bbox={l['bbox']}", flush=True)
    if lines:
        with open(os.path.join(out_dir, "ocr.json"), "w", encoding="utf-8") as f:
            json.dump(lines, f, ensure_ascii=False, indent=2)

    # ---------------- 结论 ----------------
    reading = detector.detect(win, allow_ocr=True)
    report["result"] = {
        "state": reading.state.value,
        "conflict": reading.conflict,
        "detail": reading.summary(),
        "evidence": [{"method": e.method, "state": e.state.value,
                      "detail": e.detail} for e in reading.evidence],
    }
    print("\n== 结论 ==", flush=True)
    print(f"  DurationState = {reading.state.value}", flush=True)
    print(f"  依据          : {reading.summary()}", flush=True)
    if reading.state is DurationState.UNKNOWN:
        print("  提示          : 无法确认状态时本工具不会做任何点击（Fail Safe）。\n"
              "                 请把 inspector.json 与两张截图一起交给排障。", flush=True)

    _write(report, out_dir)
    return report


def _write(report: dict, out_dir: str) -> None:
    path = os.path.join(out_dir, "inspector.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n完整诊断已写入 {path}", flush=True)
    except OSError as e:
        print(f"\n写入失败: {e}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="雷神 UI 诊断工具（UI Inspector）")
    ap.add_argument("--out", default=os.path.join(base_dir(), "logs", "inspector"),
                    help="产物目录（截图 / ocr.json / inspector.json）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    setup_logging("INFO", console=False)
    rep = run(a.out, verbose=not a.quiet)
    return 0 if rep.get("result", {}).get("state") != "UNKNOWN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
