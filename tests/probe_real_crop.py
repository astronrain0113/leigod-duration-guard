"""离线验证：用真实雷神截图比对两套 topbar_crop 的 OCR 结果。

动机：core/config.py 的 DEFAULT_CONFIG.detection.topbar_crop（top=0.010,bottom=0.065）
与 config/config.json（top=0.0,bottom=0.14）不一致，而 load_config 合并时
**文件值覆盖默认值**，所以真正生效的是较松的那套（会把下方游戏分类标签一起框进来）。
本脚本用 real_inspector/leigod_full.png 复算，确定哪一套更干净且仍能正确判 PAUSED。

用法：
    python tests/probe_real_crop.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

IMG = os.path.join(ROOT, "real_inspector", "leigod_full.png")

CROPS = {
    "现行config.json(bottom=0.14)": {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14},
    "config.py默认(bottom=0.065)":  {"left": 0.45, "top": 0.010, "right": 1.0, "bottom": 0.065},
    "top0.000 b0.070":              {"left": 0.45, "top": 0.000, "right": 1.0, "bottom": 0.070},
    "top0.000 b0.075":              {"left": 0.45, "top": 0.000, "right": 1.0, "bottom": 0.075},
    "top0.000 b0.080":              {"left": 0.45, "top": 0.000, "right": 1.0, "bottom": 0.080},
    "top0.000 b0.085":              {"left": 0.45, "top": 0.000, "right": 1.0, "bottom": 0.085},
    "top0.000 b0.090":              {"left": 0.45, "top": 0.000, "right": 1.0, "bottom": 0.090},
    "top0.000 b0.095":              {"left": 0.45, "top": 0.000, "right": 1.0, "bottom": 0.095},
    "top0.005 b0.085":              {"left": 0.45, "top": 0.005, "right": 1.0, "bottom": 0.085},
    "top0.005 b0.090":              {"left": 0.45, "top": 0.005, "right": 1.0, "bottom": 0.090},
    "top0.005 b0.095":              {"left": 0.45, "top": 0.005, "right": 1.0, "bottom": 0.095},
    "top0.010 b0.085":              {"left": 0.45, "top": 0.010, "right": 1.0, "bottom": 0.085},
    "top0.010 b0.090":              {"left": 0.45, "top": 0.010, "right": 1.0, "bottom": 0.090},
    "top0.010 b0.095":              {"left": 0.45, "top": 0.010, "right": 1.0, "bottom": 0.095},
}


def main():
    from PIL import Image
    import numpy as np
    from detection import image_detection as img
    from detection import ocr as ocr_mod

    if not os.path.exists(IMG):
        print("缺少真实截图:", IMG)
        return 1
    pil = Image.open(IMG).convert("RGB")
    arr = np.asarray(pil)
    print(f"真实截图: {IMG}")
    print(f"  尺寸 = {pil.size[0]}x{pil.size[1]}  (窗口 rect 实测 1500x938)")
    print()

    if not ocr_mod.engine_available():
        print("rapidocr 不可用")
        return 1

    for label, crop in CROPS.items():
        region = img.crop_relative(arr, (0, 0, pil.size[0], pil.size[1]), crop)
        h, w = (region.shape[0], region.shape[1]) if region is not None else (0, 0)
        lines = ocr_mod.read_text(region)
        state, detail = ocr_mod.classify_text(lines)
        texts = [f"{l['text']}({l['confidence']:.2f})" for l in lines]
        print(f"[{label}] 区域={w}x{h}  行数={len(lines)}  判定={state}")
        print(f"    文案: {texts}")
        print(f"    判据: {detail}")
        print()

    with open(os.path.join(HERE, "out", "real_crop_compare.txt"), "w", encoding="utf-8") as f:
        f.write("见 stdout\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
