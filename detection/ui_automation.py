"""第一优先级识别方案：Windows UI Automation（规格书 §七）。

从旧实现里吸取的教训（务必保留这段说明，避免退回旧行为）：
  旧代码用 `kw in ctrl.Name` 的**子串**匹配找「暂停」，结果命中了雷神设置页的
  「自动暂停延迟：」以及本工具自己窗口里的「⏸ 已暂停（未消耗）」标签，
  在日志里表现为：
      手动暂停: UIAutomation - 已点击按钮: 自动暂停延迟：
  于是每次「自动暂停」都在点一个无关的设置项，状态永远不变。
  修复：① 只做**精确**文本匹配（允许少量 OCR/排版变体）；② 只在雷神主窗口
  的子树内搜索，绝不可能命中本工具自己的控件。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

#: 精确匹配用标签（规格书 §六）
PAUSE_LABELS = {"暂停时长", "暂停加速", "停止加速", "停止时长", "暂停"}
START_LABELS = {"开启时长", "开启加速", "开始加速", "恢复加速", "开启"}

_PUNCT = " \t\u3000:：。.!！?？|丨()（）[]【】\"'“”‘’"


def normalize(text: str) -> str:
    """归一化控件文本：去掉空白与常见标点，便于精确比较。"""
    if not text:
        return ""
    out = []
    for ch in str(text):
        if ch in _PUNCT:
            continue
        out.append(ch)
    return "".join(out)


#: 归一化后的标签集合（模块级算一次）
_PAUSE_NORM = frozenset(normalize(x) for x in PAUSE_LABELS)
_START_NORM = frozenset(normalize(x) for x in START_LABELS)


def classify_control_name(name: str):
    """按控件文本判定它是哪种按钮。返回 "pause" / "start" / None。

    这里是**必须精确匹配**的地方，抽成纯函数是为了能被单元测试直接盯住：
    旧实现用 `kw in ctrl.Name` 做子串匹配，于是「暂停」命中了雷神设置页的
    「自动暂停延迟：」，每次「自动暂停」都在点一个无关设置项，状态永远不变。
    现在「自动暂停延迟：」归一化后是「自动暂停延迟」，不等于「暂停时长」，
    因此安全落空（返回 None）。
    """
    n = normalize(name)
    if not n:
        return None
    if n in _PAUSE_NORM:
        return "pause"
    if n in _START_NORM:
        return "start"
    return None


@dataclass
class ControlInfo:
    name: str = ""
    normalized: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    rect: tuple = None
    is_enabled: bool = True
    is_offscreen: bool = False
    supports_invoke: bool = False
    depth: int = 0
    _control: object = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "control_type": self.control_type,
            "automation_id": self.automation_id, "class_name": self.class_name,
            "rect": self.rect, "is_enabled": self.is_enabled,
            "is_offscreen": self.is_offscreen, "supports_invoke": self.supports_invoke,
            "depth": self.depth,
        }


def _to_info(ctrl, depth: int, probe_invoke: bool = True) -> ControlInfo:
    rect = None
    try:
        r = ctrl.BoundingRectangle
        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        pass
    info = ControlInfo(
        name=ctrl.Name or "",
        normalized=normalize(ctrl.Name or ""),
        control_type=getattr(ctrl, "ControlTypeName", "") or "",
        automation_id=getattr(ctrl, "AutomationId", "") or "",
        class_name=getattr(ctrl, "ClassName", "") or "",
        rect=rect,
        is_enabled=bool(getattr(ctrl, "IsEnabled", True)),
        is_offscreen=bool(getattr(ctrl, "IsOffscreen", False)),
        depth=depth,
        _control=ctrl,
    )
    if probe_invoke:
        info.supports_invoke = has_invoke_pattern(ctrl)
    return info


def has_invoke_pattern(ctrl) -> bool:
    try:
        import uiautomation as auto
        return ctrl.GetPattern(auto.PatternId.InvokePattern) is not None
    except Exception:
        return False


def probe() -> tuple:
    """精确判断 UIA 是否真的能用。返回 (可用, 说明)。

    为什么不能只判断 `import uiautomation`：
    uiautomation 的客户端 DLL（UIAutomationClient_VC140_X64.dll）是按
    **<包目录>/bin** 相对 __file__ 加载的（见 uiautomation/uiautomation.py
    的 _DllClient.__init__：binPath = dirname(__file__)/bin，
    然后 os.add_dll_directory(binPath)）。
    打包成 exe 后这个相对路径仍会被解析，但如果构建时没把 bin/*.dll 收进去，
    import 照样成功、真正调用时才报错——表现就是「第一优先级静默失效，
    只能退化到坐标点击」。所以这里主动把 DLL 拿出来验一下。

    注意：`_DllClient` 定义在**子模块** `uiautomation.uiautomation` 里，
    包的 `__init__` 并没有把它导出（只写 `hasattr(uiautomation, "_DllClient")`
    会永远拿不到，探测会退化成「假可用」）。
    """
    try:
        import uiautomation  # noqa: F401
        from uiautomation import uiautomation as _impl
    except ImportError as e:
        return False, f"未安装 uiautomation：{e}"
    client = getattr(_impl, "_DllClient", None)
    if client is None:
        return True, "UIA 可用（该版本未暴露 DLL 客户端，按可用处理）"
    try:
        dll = client.instance().dll
    except Exception as e:
        return False, f"UIA 客户端 DLL 加载失败：{e}"
    if dll is None:
        return False, ("UIA 客户端 DLL 未加载：通常是打包时漏了 "
                       "uiautomation/bin/UIAutomationClient_VC140_*.dll")
    return True, f"UIA 可用（DLL 已加载：{os.path.basename(getattr(dll, '_name', '') or '') or 'ok'}）"


def available() -> bool:
    """UIA 是否可用（含 DLL 真实加载检查，结果被 uiautomation 自身缓存）。"""
    return probe()[0]


def list_controls(hwnd, max_depth: int = 12, limit: int = 4000) -> list:
    """列出窗口内的全部控件（含名称、类型、AutomationId、包围盒、是否可 Invoke）。"""
    try:
        import uiautomation as auto
    except ImportError:
        return []
    root = auto.ControlFromHandle(int(hwnd))
    if root is None:
        return []
    out = []
    try:
        for ctrl, depth in auto.WalkControl(root, maxDepth=max_depth):
            if len(out) >= limit:
                break
            try:
                out.append(_to_info(ctrl, depth))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _rect_center(rect):
    if not rect or len(rect) < 4:
        return None
    try:
        return ((float(rect[0]) + float(rect[2])) / 2.0,
                (float(rect[1]) + float(rect[3])) / 2.0)
    except (TypeError, ValueError):
        return None


def _in_crop(rect, frame, crop) -> bool:
    """控件中心是否落在「顶栏裁剪区」内（与 OCR 用同一套区域）。"""
    c = _rect_center(rect)
    if c is None:
        return False
    try:
        fx0, fy0, fx1, fy1 = [float(v) for v in frame]
    except (TypeError, ValueError):
        return False
    w, h = fx1 - fx0, fy1 - fy0
    if w <= 0 or h <= 0:
        return False
    try:
        l = float((crop or {}).get("left", 0.0))
        t = float((crop or {}).get("top", 0.0))
        r = float((crop or {}).get("right", 1.0))
        b = float((crop or {}).get("bottom", 1.0))
    except (TypeError, ValueError):
        return False
    return (fx0 + w * l <= c[0] <= fx0 + w * r
            and fy0 + h * t <= c[1] <= fy0 + h * b)


def _name_of(ctrl) -> str:
    """只读控件名。1 次跨进程属性读取 —— 是整个扫描里最便宜的一步。"""
    try:
        return ctrl.Name or ""
    except Exception:
        return ""


#: 控件缓存：`hwnd -> {"ts", "pause", "start", "all_n"}`
#:
#: **为什么要缓存**（2026-09-30 性能治理）：
#: 旧 `find_duration_controls` 每次都对**整棵树**的每个控件调 `_to_info()`，
#: 而 `_to_info` 要读 8 个属性 + 做一次 pattern 查询。真机树里有 ~476 个控件，
#: 也就是每轮状态识别要付 ~4000 次跨进程 COM 调用 —— 单次枚举 0.3~2 秒。
#: 后果有两处，都很致命：
#:   · 「点 ✕ 之后要 6~8 秒才自动暂停」（用户明确要求 1~2 秒）；
#:   · 引擎 tick 被这次枚举堵住 → 死手窗口没人刷新 →
#:     日志里反复出现 `[degraded] 消费端超时未刷新，已自动停止吞点`，
#:     而这正是**用户点 ✕ 的那一刻** —— 第二次点就吞不住了。
#: 缓存之后，稳态每轮只需重读 1~2 个控件的 Name（微秒级）。
_CACHE = {}
#: 缓存的"新鲜"时限（秒）。超过就做一次全量扫描，防止长期漂移。
#:
#: 3.0 → 5.0 的理由（2026-10-01）：状态轮询压到 0.5s 后，若 TTL 仍是 3s，
#: 全量枚举（真机 ~657ms，是后台占用的主要来源）的频次就白白被推高。
#: 而**状态变化本身不靠它发现**：缓存元素的名字一变就被读到、元素一失效就退回
#: 全量扫描，所以检测一帧都不会晚。TTL 只决定"多久重新整体核对一遍"，
#: 放长一点等于用更少的枚举换同样的及时性。
#: 放行 ✕ 之前的那次全量扫描是**独立**的（`_verify` 里的地面真相），不受这里影响。
CACHE_TTL = 5.0
#: 缓存上限（按 hwnd 计），防止长期运行堆积
_CACHE_MAX = 8


def _cache_put(hwnd, pause, start, all_n) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k]["ts"])
        _CACHE.pop(oldest, None)
    _CACHE[int(hwnd)] = {"ts": time.time(), "pause": pause, "start": start,
                         "all_n": all_n}


def clear_cache(hwnd=None) -> None:
    """清掉控件缓存（绑定窗口变化、测试隔离时用）。"""
    if hwnd is None:
        _CACHE.clear()
    else:
        _CACHE.pop(int(hwnd), None)


def read_cached(hwnd) -> dict:
    """**快路径**：只重读缓存控件的名字，判断当前是暂停还是开启。

    返回 None 表示"缓存不可用/已失效"，调用方应做一次全量扫描。

    安全性（这是本函数唯一需要论证的地方）：它只用缓存元素做**两件事**：
      ① 判断缓存元素现在叫什么名字；
      ② 名字完全认不出（元素被替换/失效）时**放弃**，交回全量扫描。
    它**不做**任何猜测：绝不因为"缓存里原来是暂停"就推断现在还是暂停。
    而且调用方（`DurationController._verify`）在给出最终结论前还会用一次
    **全量扫描**复核 —— 快路径只用来把等待时间压下来，不用来下结论。
    """
    c = _CACHE.get(int(hwnd))
    if not c or (time.time() - c["ts"]) > CACHE_TTL:
        return None
    pause, start = [], []
    for kind, items in (("pause", c["pause"]), ("start", c["start"])):
        for info in items:
            name = _name_of(info._control)
            k = classify_control_name(name)
            if k is None:
                continue                    # 这个元素没用了（可能已被替换）
            info.name, info.normalized = name, normalize(name)
            (pause if k == "pause" else start).append(info)
    if not pause and not start:
        return None                         # 缓存元素全失效 → 老实全量重扫
    return {"pause": pause, "start": start, "all": [], "all_n": c.get("all_n", 0),
            "filtered": True, "from_cache": True}


def find_duration_controls(hwnd, max_depth: int = 12, retries: int = 2,
                           frame=None, crop=None, fast: bool = False) -> dict:
    """在雷神窗口子树内定位「暂停时长 / 开启时长」控件。

    返回 {"pause": [ControlInfo...], "start": [...], "all": [...], ...}

    `fast=True`：先走缓存快路径（只重读 1~2 个控件的 Name，微秒级）；
    缓存不可用就退回全量扫描。**状态轮询的高频路径应该用它**。

    Chromium/Electron 的辅助功能树是**按需启用**的：第一次 UIA 查询时可能
    还没建好，因此这里允许重试。

    ⚠️ **必须按「顶栏裁剪区」过滤**（真机踩出来的）：
    雷神窗口里叫「开启时长」的控件不止顶栏那一个按钮 —— 真机实测枚举到
    **1 个「暂停时长」 + 20 个「开启时长」**，于是两条证据同时成立、判定为
    「证据冲突 → UNKNOWN」，状态**永久读不出来**（UIA 明明能读到，却被自己的
    噪声淹没）。现在与 OCR 用同一套 `detection.topbar_crop` 区域收敛候选，
    并优先取**在屏**（非 offscreen）的控件。
    `frame`/`crop` 缺省时保持旧的整窗口行为（不传 = 不过滤）。
    """
    if fast and frame and crop:
        hit = read_cached(hwnd)
        if hit is not None:
            return hit

    t0 = time.time()
    infos = []          # 只装**命中名字**的控件（不再为整棵树的每个节点建对象）
    total = 0
    pause, start = [], []
    for attempt in range(max(1, retries)):
        pause, start, total = _scan_by_name(hwnd, max_depth)
        if pause or start:
            break
        if attempt + 1 < retries:
            time.sleep(0.35)      # 给辅助功能树一点建立时间
    last_scan_ms = (time.time() - t0) * 1000.0
    if pause or start:
        if frame and crop:
            p2 = [c for c in pause if _in_crop(c.rect, frame, crop)]
            s2 = [c for c in start if _in_crop(c.rect, frame, crop)]
            # 只有过滤后还剩下候选才采用；若把两边都滤空了，说明坐标系对不上，
            # 退回未过滤的结果（不至于因为过滤而彻底失灵）。
            if p2 or s2:
                pause, start = p2, s2
            # 在屏优先：offscreen 的控件多半是隐藏面板/缓存节点
            pause = [c for c in pause if not c.is_offscreen] or pause
            start = [c for c in start if not c.is_offscreen] or start
        _probe_invoke(pause)
        _probe_invoke(start)
        _cache_put(hwnd, pause, start, total)
    return {"pause": pause, "start": start, "all": infos, "all_n": total,
            "filtered": bool(frame and crop), "scan_ms": last_scan_ms,
            # 明确标出"这一读不是缓存"：调用方据此判断它能否直接当**地面真相**用
            # （见 `DurationController._verify`）。少标这一位，就会多跑一次全量扫描 ——
            # 真机上那正是「点 ✕ 要 2.5 秒」里最后 683ms 的来源。
            "from_cache": False}


def _scan_by_name(hwnd, max_depth: int) -> tuple:
    """全量扫描：**只读 Name**，命中的才建 `ControlInfo`。

    这是本模块最大的性能改动。旧实现对每个控件都调 `_to_info()`（8 个属性 +
    1 次 pattern 查询），476 个控件就是 ~4000 次跨进程调用；现在只有命中的
    那几个（通常 ≤21 个）才付全价，其余只读 1 个属性就跳过。
    """
    pause, start, total = [], [], 0
    try:
        import uiautomation as auto
    except ImportError:
        return pause, start, total
    try:
        root = auto.ControlFromHandle(int(hwnd))
    except Exception:
        return pause, start, total
    if root is None:
        return pause, start, total
    try:
        for ctrl, depth in auto.WalkControl(root, maxDepth=max_depth):
            total += 1
            kind = classify_control_name(_name_of(ctrl))
            if kind is None:
                continue                       # ← 只读一个属性就跳过
            info = _to_info(ctrl, depth, probe_invoke=False)
            (pause if kind == "pause" else start).append(info)
    except Exception:
        pass
    return pause, start, total


def _probe_invoke(items: list, limit: int = 3) -> None:
    """只给前几个候选测 InvokePattern（旧实现给全部控件都测）。

    为什么只测前几个：`GetPattern()` 是一次跨进程调用，476 个控件就要 476 次。
    而调用方（`DurationController._do_pause`）只按 `supports_invoke` **排序**，
    真正的动作是"拿第一个能 Invoke 的"，所以只需要知道前几个的能力。
    """
    for info in items[:max(0, limit)]:
        info.supports_invoke = has_invoke_pattern(info._control)


def invoke(info: ControlInfo) -> tuple:
    """优先用 InvokePattern 直接调用（最可靠，不受遮挡/焦点影响）。

    返回 (是否成功, 说明)。失败时调用方应退回相对坐标点击。
    """
    ctrl = info._control
    if ctrl is None:
        return False, "控件句柄失效"
    try:
        import uiautomation as auto
        pat = ctrl.GetPattern(auto.PatternId.InvokePattern)
        if pat is not None:
            pat.Invoke()
            return True, "InvokePattern"
    except Exception as e:
        first_err = f"InvokePattern 失败: {e}"
    else:
        first_err = "控件不支持 InvokePattern"
    try:
        ctrl.Click(simulateMove=False)
        return True, "UIA Click"
    except Exception as e:
        return False, f"{first_err}; Click 也失败: {e}"


def clickable_point(info: ControlInfo):
    """控件中心点（屏幕坐标）。"""
    if not info.rect:
        return None
    l, t, r, b = info.rect
    return (l + (r - l) // 2, t + (b - t) // 2)


def dump_tree(hwnd, max_depth: int = 12) -> list:
    return [i.as_dict() for i in list_controls(hwnd, max_depth=max_depth)]
