"""离线验证：用**真实截图**检查 OCR 按钮定位算出来的坐标是否落在真按钮上。

为什么需要它：
  `test_units.py` 里的几何用例用的是**构造数据**，能证明「换算式没错」，
  但证明不了「换算式算出来的地方真的是按钮」。这一步必须拿真图跑一遍，
  并且把定位结果**裁出来存成图** —— 让人一眼能看出点在哪。
  规格书 §二十五 的精神就是这个：坐标必须经过真实素材验证，不能只靠推导。

用法：
    python tests/verify_ocr_button_locate.py                     # 跑默认的两张真机全图
    python tests/verify_ocr_button_locate.py 图1.png [图2.png]

  ⚠️ 输入必须是**整个窗口**的截图（如 real_state/leigod_full.png）。
     不要传 duration_region.png —— 那已经是裁剪过的区域，会被再裁一次，
     结果必然认不出文字。脚本会对这类输入给出提示。

产出（存到 tests/out/ocr_locate/）：
    <原图名>.<状态>.annotated.png   在定位到的矩形上画框（全图）
    <原图名>.<状态>.crop.png        定位到的区域放大 3 倍（本人眼确认是不是按钮）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(HERE, "out", "ocr_locate")

DEFAULT_IMAGES = [
    os.path.join(ROOT, "real_state", "leigod_full.png"),
    os.path.join(ROOT, "real_inspector", "leigod_full.png"),
]


def _load(path):
    cv2 = __import__("cv2")
    import numpy as np
    data = np.fromfile(path, dtype=np.uint8)          # 兼容中文路径
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读不了图：{path}")
    return cv2, np, cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def run_one(path: str) -> bool:
    from core import config as config_mod
    from core.state_machine import DurationState
    from detection import image_detection as img_mod
    from detection import ocr as ocr_mod
    from leigod.duration_detector import DurationDetector

    cv2, np, rgb = _load(path)
    h, w = rgb.shape[:2]
    print(f"\n{'=' * 72}\n图片：{path}\n尺寸：{w}x{h}")

    cfg = config_mod.Config({})
    crop_cfg = cfg.get("detection.topbar_crop",
                       {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14})
    print(f"裁剪配置：{crop_cfg}")

    region = img_mod.crop_relative(rgb, (0, 0, w, h), crop_cfg)
    if region is None:
        print("✗ 裁剪失败")
        return False
    lines = ocr_mod.read_text(region)
    print(f"OCR 识别到 {len(lines)} 行文字：")
    for ln in lines:
        print(f"   conf={ln['confidence']:.2f}  bbox={ln['bbox']}  text={ln['text']!r}")

    det = DurationDetector(cfg)
    # 截图坐标系 == 屏幕坐标系（frame 原点设为 0），方便肉眼对照
    det._frame_of_last_capture = [0, 0, w, h]
    det.last_ocr_lines = lines
    det._record_ocr_geom(rgb, region, crop_cfg)
    print(f"几何信息：{det.last_ocr_geom}")

    ok = False
    for state in (DurationState.RUNNING, DurationState.PAUSED):
        rect = det.button_rect_on_screen(state)
        ctr = det.button_center_on_screen(state)
        print(f"  {state.value:<8} 定位 = {rect}   中心 = {ctr}")
        if rect:
            ok = True
            os.makedirs(OUT_DIR, exist_ok=True)
            base = os.path.splitext(os.path.basename(path))[0]
            tag = f"{base}.{state.value.lower()}"

            # ① 全图标注：把按钮框画成粗红框（涨红跌绿与本题无关，这里红=目标）
            ann = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
            cv2.rectangle(ann, (rect[0], rect[1]), (rect[2], rect[3]), (0, 0, 255), 3)
            cv2.circle(ann, ctr, 6, (255, 0, 0), -1)
            cv2.imwrite(os.path.join(OUT_DIR, tag + ".annotated.png"), ann)

            # ② 局部放大：以中心为原点取 5 倍框宽的窗口，方便确认框住的到底是什么
            half = max(60, (rect[2] - rect[0]) * 3)
            x0, y0 = max(0, ctr[0] - half), max(0, ctr[1] - max(30, (rect[3] - rect[1]) * 3))
            x1, y1 = min(w, ctr[0] + half), min(h, ctr[1] + max(30, (rect[3] - rect[1]) * 3))
            patch = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2BGR).copy()
            patch = cv2.resize(patch, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
            # 在放大图上把框重新按比例画出来，避免缩放后对不上
            cv2.rectangle(patch, ((rect[0] - x0) * 3, (rect[1] - y0) * 3),
                          ((rect[2] - x0) * 3, (rect[3] - y0) * 3), (0, 0, 255), 2)
            cv2.imwrite(os.path.join(OUT_DIR, tag + ".crop.png"), patch)
            print(f"    → 已存 {tag}.annotated.png / {tag}.crop.png")

    if not ok:
        print("⚠️ 两个方向都没能定位到按钮文字框 —— 需要看上面的 OCR 原文，"
              "判断是裁剪区域不对、OCR 认不出，还是文案关键词没覆盖。")
        if "region" in os.path.basename(path).lower():
            print("   提示：这张看起来已经是裁剪后的区域图，会被再裁一次 → 请改用整窗截图。")
    return ok


def main() -> int:
    paths = sys.argv[1:] or [p for p in DEFAULT_IMAGES if os.path.isfile(p)]
    if not paths:
        print("没有可用的真实截图（real_state/leigod_full.png 等）。"
              "请先跑 tests/probe_real_crop.py 采集。")
        return 2
    try:
        from detection import ocr as ocr_mod
    except Exception as e:                     # pragma: no cover
        print(f"导入失败：{e}")
        return 2
    if not ocr_mod.engine_available():
        print("未安装 OCR 引擎（rapidocr-onnxruntime），无法验证。")
        return 2

    results = [run_one(p) for p in paths]
    print(f"\n{'=' * 72}")
    print(f"结论：{sum(results)}/{len(results)} 张图成功定位到按钮文字框")
    print(f"产出目录：{OUT_DIR}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
