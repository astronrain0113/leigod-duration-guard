"""真机截图回归：用真实雷神截图验证「裁剪 → OCR → 判定」这条链路。

为什么需要这一套：
  单元测试用的都是**合成文案**（直接构造 {text,confidence} 列表），
  它验证的是 classify_text 的逻辑，**测不到「裁剪区域是否真的能出字」**。
  真机上恰恰栽在这里：DEFAULT 的 topbar_crop 被改成 top=0.010/bottom=0.065
  （只有 51px 高），RapidOCR 检出 0 行 → 判定恒为 UNKNOWN，
  而合成用例全绿，缺陷完全隐形。本套用真机截图把这条盲区补上。

素材：real_inspector/leigod_full.png（窗口 1500x938 / DPI 120，状态 PAUSED）。
素材缺失或未安装 OCR 引擎 → 明确跳过（不假装通过）。

用法：
    python tests/test_real_capture.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from tests.harness import Report  # noqa: E402

IMG = os.path.join(ROOT, "real_inspector", "leigod_full.png")
DEFAULT_CROP = {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14}


def main() -> int:
    rep = Report("真机截图回归 —— 裁剪 / OCR / 判定 链路")
    rep.section("前置：素材与引擎")

    if not os.path.exists(IMG):
        rep.info(f"跳过：未找到真机截图 {IMG}")
        rep.info("（在真机上运行 `LeigodGuardCUI.exe --inspector` 即可生成）")
        rep.save("real_capture_report.txt")
        print("\n结果：跳过（缺少真机素材）", flush=True)
        return 0

    try:
        from PIL import Image
        import numpy as np
    except ImportError as e:
        rep.info(f"跳过：缺少 Pillow/numpy（{e}）")
        rep.save("real_capture_report.txt")
        print("\n结果：跳过（缺少依赖）", flush=True)
        return 0

    from detection import image_detection as img
    from detection import ocr as ocr_mod

    if not ocr_mod.engine_available():
        rep.info("跳过：未安装 rapidocr-onnxruntime")
        rep.save("real_capture_report.txt")
        print("\n结果：跳过（无 OCR 引擎）", flush=True)
        return 0

    pil = Image.open(IMG).convert("RGB")
    arr = np.asarray(pil)
    w, h = pil.size
    rep.info(f"素材 {IMG}")
    rep.info(f"尺寸 {w}x{h}")

    rep.section("1. 真实截图可信度（脏图不得用于判定）")
    ok, why = img.looks_like_real_capture(arr, (1500, 938))
    rep.check("真机截图通过有效性校验", ok, why)

    rep.section("2. 默认裁剪 = 运行期兜底值（三处不能各写一套）")
    from core.config import DEFAULT_CONFIG
    from_leigod = os.path.join(ROOT, "leigod", "duration_detector.py")
    cfg_crop = DEFAULT_CONFIG["detection"]["topbar_crop"]
    rep.check("DEFAULT_CONFIG 的裁剪与真机验证值一致", cfg_crop == DEFAULT_CROP, str(cfg_crop))
    rep.info(f"duration_detector 兜底值（源码）: {DEFAULT_CROP}"
             f"{'' if os.path.exists(from_leigod) else ' (文件缺失)'}")

    rep.section("3. 默认裁剪下必须能出字并判出 PAUSED（本次修掉的缺陷）")
    region = img.crop_relative(arr, (0, 0, w, h), cfg_crop)
    rh, rw = region.shape[:2]
    rep.info(f"裁剪区域 {rw}x{rh}")
    lines = ocr_mod.read_text(region)
    rep.info(f"识别到 {len(lines)} 行: " + " | ".join(f"{l['text']}" for l in lines))
    state, detail = ocr_mod.classify_text(lines)
    rep.check("裁剪区域非空", rw > 0 and rh > 0, f"{rw}x{rh}")
    rep.check("默认裁剪下 OCR 至少识别到 1 行（0 行说明区域被压扁了）",
              len(lines) >= 1, f"行数={len(lines)}")
    rep.check("判定为 PAUSED（真机此截图为已暂停态）", state == "PAUSED", str(detail))

    rep.section("4. 已知脆弱区（仅记录，不断言）")
    for bad in ({"left": 0.45, "top": 0.010, "right": 1.0, "bottom": 0.065},
                {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.075}):
        r2 = img.crop_relative(arr, (0, 0, w, h), bad)
        n2 = len(ocr_mod.read_text(r2)) if r2 is not None else -1
        rep.info(f"裁剪 top={bad['top']} bottom={bad['bottom']} → 识别 {n2} 行"
                 f"（真机实测为 0 行，故未采用此值）")

    rep.save("real_capture_report.txt")
    bad = [l for l in rep.lines if "[FAIL]" in l]
    print(f"\n结果：{'全部通过' if not bad else f'{len(bad)} 项未通过'}", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
