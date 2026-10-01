"""图像特征：截图有效性校验 + 按钮底色辅助判断（规格书 §九）。

为什么必须有「截图有效性校验」：
旧实现直接对屏幕截取结果做 OCR，抓到的是被遮挡的别的画面
（ThunderGuard/logs/btn_probe.png、frames/classify_strip.png 里截到的是
游戏画面和弹幕），OCR 于是给出无关文字，结论自然是错的。
这里先用一组廉价的图像统计量判断「这张图到底是不是雷神窗口」，
不通过就当作没有证据（UNKNOWN），绝不用脏图去猜。
"""
from __future__ import annotations

#: 判为「几乎全黑/全白」的阈值（UIPI 拦截 PrintWindow 时返回黑图）
_BLANK_MEAN_LOW = 6.0
_BLANK_MEAN_HIGH = 250.0
_MIN_STD = 6.0


def looks_like_real_capture(arr, expected_size=None, size_tolerance: float = 0.25) -> tuple:
    """判断截图是否可信。返回 (是否可信, 说明)。"""
    if arr is None:
        return False, "截图为空"
    try:
        import numpy as np
        a = np.asarray(arr)
    except Exception as e:
        return False, f"无法解析截图: {e}"
    if a.ndim != 3 or a.shape[2] < 3:
        return False, f"截图通道数异常: {getattr(a, 'shape', None)}"
    h, w = a.shape[:2]
    if h < 20 or w < 40:
        return False, f"截图过小: {w}x{h}"
    mean = float(a.mean())
    std = float(a.std())
    if mean <= _BLANK_MEAN_LOW:
        return False, f"截图接近全黑（mean={mean:.1f}，通常是被 UIPI 拦截）"
    if mean >= _BLANK_MEAN_HIGH:
        return False, f"截图接近全白（mean={mean:.1f}）"
    if std < _MIN_STD:
        return False, f"截图几乎没有内容（std={std:.1f}）"
    if expected_size:
        ew, eh = expected_size
        if ew > 0 and eh > 0:
            if abs(w - ew) / ew > size_tolerance or abs(h - eh) / eh > size_tolerance:
                return False, f"截图尺寸与窗口不符（{w}x{h} vs 期望 {ew}x{eh}）"
    return True, f"截图有效（{w}x{h} mean={mean:.0f} std={std:.0f}）"


def crop_relative(arr, rect, crop: dict):
    """按相对比例裁剪。crop 形如 {"left":0.45,"top":0.0,"right":1.0,"bottom":0.14}。

    必须保证返回的区域**非空**：numpy 切片越界会静默返回 0 尺寸数组，
    而空数组喂给 OCR 会得到「没有文字」，最后被当成「没有证据」——
    一个坐标写错就会让整个识别链悄悄退化成 UNKNOWN，很难查。
    这里把范围夹到图像内部，保证至少有 1x1 个像素。
    """
    if arr is None:
        return None
    h, w = arr.shape[:2]
    if h < 1 or w < 1:
        return None
    l = int(max(0.0, min(1.0, crop.get("left", 0.0))) * w)
    t = int(max(0.0, min(1.0, crop.get("top", 0.0))) * h)
    r = int(max(0.0, min(1.0, crop.get("right", 1.0))) * w)
    b = int(max(0.0, min(1.0, crop.get("bottom", 1.0))) * h)
    l = min(max(0, l), w - 1)
    t = min(max(0, t), h - 1)
    r = min(max(r, l + 1), w)
    b = min(max(b, t + 1), h)
    return arr[t:b, l:r].copy()


def region_mean_color(arr, box=None):
    """区域平均颜色（box 为图像内像素坐标 (l,t,r,b)；None 表示整图）。"""
    if arr is None:
        return None
    try:
        import numpy as np
        a = np.asarray(arr)
        if box:
            l, t, r, b = [int(v) for v in box]
            a = a[max(0, t):max(1, b), max(0, l):max(1, r)]
        if a.size == 0:
            return None
        return tuple(int(v) for v in a.reshape(-1, a.shape[-1])[:, :3].mean(axis=0))
    except Exception:
        return None


def classify_button_color(rgb) -> tuple:
    """按按钮底色推断状态（辅助证据，默认不参与最终判定）。

    实测：RUNNING 为红底（约 RGB 214,111,112），PAUSED 为白底（约 255,255,255）。
    界面上有大量暖色广告位，颜色在不同主题/压缩下会漂移，
    因此这个信号只作交叉核对，不作唯一依据。
    """
    if not rgb:
        return None, "无颜色样本"
    r, g, b = rgb[0], rgb[1], rgb[2]
    if r > 200 and g > 200 and b > 200:
        return "PAUSED", f"按钮底色偏白 {rgb}"
    if r > 150 and r - g > 50 and r - b > 40:
        return "RUNNING", f"按钮底色偏红 {rgb}"
    return None, f"底色无法判定 {rgb}"
