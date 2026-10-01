"""第三优先级识别方案：OCR + 图像特征（规格书 §九）。

实测依据：真实雷神顶栏按钮的两种文案是
    加速中 → 「暂停时长」（红底白字）
    已暂停 → 「开启时长」（白底深字）
见 ThunderGuard/logs/frames/topbar_zoom.png。

OCR 常把「开启」误识别成「并启 / 开肩 / 开」等，因此这里用
「必须同时出现『时长』与『启』类字符」这样的组合条件，而不是单字匹配。
"""
from __future__ import annotations

import re
import threading

PY_UNKNOWN = "UNKNOWN"
PY_RUNNING = "RUNNING"
PY_PAUSED = "PAUSED"

_PUNCT_RE = re.compile(r"[\s\u3000:：。.!！?？|丨()（）\[\]【】\"'“”‘’]")


def normalize(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or ""))


_ENGINE = None
_ENGINE_FAILED = False
#: 引擎构造失败的原因（供界面/自检**如实**展示）。
#: 为什么必须留着它：真机踩过 —— 打包版曾出现「`import rapidocr_onnxruntime`
#: 成功、但 .onnx 模型没被打进包」的情况，`RapidOCR()` 构造抛异常被这里吞掉，
#: 之后每一轮 OCR 都静默返回空列表，而 `engine_available()` 却报「可用」，
#: 把排查方向直接带偏（2026-09-30，见 packaging/*.spec 里的 _OCR_DATAS 注释）。
_ENGINE_ERROR = ""
# ⚠️ 引擎是**全局单例**，而调用它的不止一个线程：
#   · 状态识别（主循环 / 保护引擎的 poll）
#   · 层级3 确认框监测的 `confirm-ocr` 扫描线程
# 同一个 RapidOCR(onnxruntime) 会话被两个线程并发 `eng(img)` 会**阻塞**（真机实测：
# 真机闭环脚本在"等待切到计时中"阶段整体冻住 100+ 秒，无任何报错，只是不再刷新）。
# 推理与惰性初始化都必须串行化。
_ENGINE_LOCK = threading.RLock()


def engine_available() -> bool:
    """OCR 引擎**真的能用**吗 —— 不是"模块能不能 import"。

    这两件事必须分开：`import rapidocr_onnxruntime` 成功只能说明**代码**在，
    而模型是数据文件（打包时极易漏掉）。旧实现只看 import，于是模型缺失时
    照样报「可用」，而实际每一轮 OCR 都返回空 —— 一个会把人带偏的假信号。
    现在要求「引擎真的构造得起来」；构造结果会被缓存，所以不影响热路径开销。
    """
    return get_engine() is not None


def last_error() -> str:
    """引擎不可用的原因（空字符串表示没有错误）。"""
    return _ENGINE_ERROR


def get_engine():
    """惰性创建 RapidOCR 引擎（首次初始化约 1-2 秒）。线程安全。"""
    global _ENGINE, _ENGINE_FAILED, _ENGINE_ERROR
    # 双检 + 锁：否则两个线程会各自创建一个引擎（各 1-2s），后建的把先建的覆盖掉，
    # 先建的那个从此无人回收；更糟的是并发初始化期间对方读到的是半就绪对象。
    if _ENGINE is not None or _ENGINE_FAILED:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None or _ENGINE_FAILED:
            return _ENGINE
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ENGINE = RapidOCR()
            _ENGINE_ERROR = ""
        except Exception as e:
            # 绝不静默：把原因留下来，让 --check / 状态明细 / 日志都能说清楚。
            _ENGINE_FAILED = True
            _ENGINE = None
            _ENGINE_ERROR = f"{type(e).__name__}: {e}"
    return _ENGINE


def release_engine() -> None:
    global _ENGINE
    _ENGINE = None


def read_text(rgb_image) -> list:
    """对 RGB numpy 图像做 OCR。

    返回 [{text, confidence, box}]，box 为四点坐标（左上起顺时针）。
    """
    eng = get_engine()
    if eng is None or rgb_image is None:
        return []
    try:
        import numpy as np
        img = np.ascontiguousarray(rgb_image[:, :, ::-1])   # RGB -> BGR（RapidOCR 约定）
        # 整个推理过程持锁串行化：onnxruntime 会话不支持被并发调用，
        # 并发的代价不是报错而是**静默阻塞**（见 _ENGINE_LOCK 处的注释）。
        with _ENGINE_LOCK:
            result, _ = eng(img)
    except Exception:
        return []
    lines = []
    for item in (result or []):
        try:
            box, text, conf = item[0], item[1], float(item[2])
        except Exception:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append({
            "text": str(text),
            "confidence": round(conf, 4),
            "box": [[float(x), float(y)] for x, y in box],
            "bbox": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)],
        })
    return lines


def classify_text(lines, min_confidence: float = 0.5) -> tuple:
    """从 OCR 结果判定总时长状态。

    返回 (state_or_None, detail)。
    """
    texts = [normalize(l["text"]) for l in lines
             if l.get("confidence", 0) >= min_confidence and l.get("text")]
    if not texts:
        return None, "未识别到可用文字"
    joined = " ".join(texts)

    has_duration = "时长" in joined or ("时" in joined and "长" in joined)
    has_pause = "暂停" in joined
    # 「开肩 / 并启 / 并起」都是「开启」的常见误识别，必须显式收进来，
    # 否则「并起时长」会因为既没有「启」也不在关键词表里而被判成无法识别。
    # 「并起」来自真机实测：tests/probe_real_crop.py 在 bottom=0.080 的裁剪下
    # 把「开启时长」识别成「并起时长」，当时被误判为 UNKNOWN。
    has_start = any(k in joined for k in
                    ("开启", "开始", "恢复", "并启", "开肩", "并起", "开起"))

    # 冲突判定必须放在最前面：两路文案同时出现时，绝不能因为 has_duration
    # 成立就顺着往下走返回 PAUSED —— 那是「猜」，规格书要求冲突即 UNKNOWN。
    if has_pause and has_start:
        return None, f"同时识别到暂停与开启，结果冲突: {joined}"
    if has_pause:
        return PY_RUNNING, f"识别到「暂停」类文案: {joined}"
    if has_duration and (has_start or "启" in joined or "肩" in joined):
        return PY_PAUSED, f"识别到「开启时长」类文案: {joined}"
    return None, f"文案无法判定: {joined}"


def to_state_name(state: str):
    """把本模块的字符串结论映射到 core.state_machine.DurationState。"""
    from core.state_machine import DurationState
    return {"RUNNING": DurationState.RUNNING,
            "PAUSED": DurationState.PAUSED}.get(state, DurationState.UNKNOWN)
