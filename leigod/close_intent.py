"""关闭意图识别与吞点策略（关闭保护 v2 的判定层）。

## 为什么单独成一个文件

这段判定必须跑在 **`WH_MOUSE_LL` 低层鼠标钩子的回调里**，而低层钩子有一条
残酷的规则：回调超过 `LowLevelHooksTimeout`（默认 300ms）没返回，Windows 会
**静默把钩子摘掉**——保护彻底失效，而且用户和程序都不会收到任何错误。
因此本模块被刻意约束为：

  · 只依赖一个**不可变的快照** `GuardSnapshot`（由主循环线程定时整体替换），
    钩子回调只读取一次引用，不做加锁、不写共享状态；
  · 只做**纯数值比较**：不跨进程 `SendMessage`、不读文件、不查进程、不打日志。

真正的「这一点是不是真的压在雷神窗口上」这类**跨进程**核查，放到主循环线程
（消费端）做；万一核查不通过（误吞），立刻把这次点击**重放**出去，
用户几乎无感。

## 为什么判据必须联合（规格书 §十三）

旧实现的判据是 `WM_NCHITTEST == HTCLOSE`，真机实测**恒为假**（整个客户区一律
返回 `HTCLIENT(1)`，见 `tests/out/real_close_caps_admin.txt`），因此关闭意图
永远不会被识别。新判据由**四条联合条件**构成，缺一不可：

  1. 保护已开启、且不处于「用户显式放行」状态；
  2. 当前状态**不是 `PAUSED`**（已暂停就完全不干预，把误吞概率降到最低）；
  3. 该点落在**雷神窗口矩形内**；
  4. 该点落在 **✕ 热区**内（相对窗口宽高的比例，窗口移动/缩放后依旧成立）。

第 3、4 条是几何的，但**只有两条同时成立才有意义**：单看「在窗口内」太宽，
单看「在右上角邻域」又可能落在别的窗口上（§十三 反对的就是后者）。
消费端还会再加一条第 5 条核查：`WindowFromPoint` 的根窗口必须是雷神。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 真机实测得出的 ✕ 热区默认值（Leigod v11.3.2.9 / 1500x938 / DPI 120）
#:   ✕ 中心 (2003,318) → 距右边 27px、距顶边 18px
#:   → 中心比例 = (1 - 27/1500, 18/938) = (0.982, 0.019)
DEFAULT_HOT_ZONE = {
    "cx": 0.982,
    "cy": 0.019,
    "half_w": 0.020,
    "half_h": 0.024,
    "min_half_px": 18,
    "hover_pad_px": 26,
}

#: 需要「先暂停再放行」的状态（PAUSED 不在其中：已暂停就完全不干预）
UNSAFE_STATES = ("RUNNING", "UNKNOWN")


@dataclass(frozen=True)
class GuardSnapshot:
    """主循环维护的只读快照。

    `frozen=True` 是刻意的：钩子回调拿到的是**一个不会再被改写的对象**，
    因此不需要任何同步原语；主循环用「整体替换引用」的方式发布新快照。
    """
    hwnd: int = 0
    rect: tuple = None              # GetWindowRect：(left, top, right, bottom)
    state: str = "UNKNOWN"          # DurationState 的 value
    enabled: bool = False
    escaping: bool = False          # 用户显式选择「允许关闭雷神（跳过保护）」
    foreground: int = 0
    zone: dict = field(default=None)
    armed: bool = True              # 钩子确认可用；False 时一律不吞
    ts: float = 0.0


def merge_zone(zone: dict | None = None) -> dict:
    """把用户配置的局部热区参数合并到默认值上（缺项用实测默认值）。"""
    z = dict(DEFAULT_HOT_ZONE)
    for k, v in (zone or {}).items():
        if v is not None:
            z[k] = v
    return z


def zone_rect(rect: tuple, zone: dict | None = None) -> tuple:
    """算出 ✕ 热区的屏幕矩形 `(x_lo, y_lo, x_hi, y_hi)`。

    用**相对比例**定位（窗口移动/缩放后依然对准），再用 `min_half_px` 保底，
    最后夹到窗口矩形内——绝不保存绝对屏幕坐标。
    """
    z = merge_zone(zone)
    l, t, r, b = [int(v) for v in rect]
    w, h = max(1, r - l), max(1, b - t)
    half_w = max(float(z["min_half_px"]), w * float(z["half_w"]))
    half_h = max(float(z["min_half_px"]), h * float(z["half_h"]))
    cx = l + w * float(z["cx"])
    cy = t + h * float(z["cy"])
    x_lo = int(max(l, cx - half_w))
    x_hi = int(min(r, cx + half_w))
    y_lo = int(max(t, cy - half_h))
    y_hi = int(min(b, cy + half_h))
    return (x_lo, y_lo, x_hi, y_hi)


def hover_rect(rect: tuple, zone: dict | None = None) -> tuple:
    """悬停预警区：在 ✕ 热区基础上向外扩 `hover_pad_px`（用于「提前暂停」）。"""
    z = merge_zone(zone)
    pad = float(z.get("hover_pad_px") or 0)
    x_lo, y_lo, x_hi, y_hi = zone_rect(rect, zone)
    l, t, r, b = [int(v) for v in rect]
    return (int(max(l, x_lo - pad)), int(max(t, y_lo - pad)),
            int(min(r, x_hi + pad)), int(min(b, y_hi + pad)))


def _inside(r: tuple, x, y) -> bool:
    return r is not None and r[0] <= x <= r[2] and r[1] <= y <= r[3]


def in_hot_zone(rect: tuple, x, y, zone: dict | None = None) -> bool:
    """该屏幕点是否落在 ✕ 热区内。"""
    if not rect:
        return False
    return _inside(zone_rect(rect, zone), x, y)


def in_hover_zone(rect: tuple, x, y, zone: dict | None = None) -> bool:
    """该屏幕点是否落在 ✕ 悬停预警区（比热区更宽）内。"""
    if not rect:
        return False
    return _inside(hover_rect(rect, zone), x, y)


def should_swallow(snap: GuardSnapshot, x: int, y: int) -> tuple:
    """判断这次左键点击是否应当被**吞掉**（阻止其送达任何窗口）。

    返回 `(是否吞, 原因)`。原因字符串会进日志，便于事后复核每一次吞点。
    这是纯函数：同样的快照 + 同样的坐标，结论永远相同，可被单元测试完全覆盖。
    """
    if snap is None or not snap.armed:
        return False, "钩子不可用"
    if not snap.enabled:
        return False, "关闭保护未开启"
    if snap.escaping:
        return False, "用户已选择放行"
    if snap.state not in UNSAFE_STATES:
        return False, f"当前状态 {snap.state} 无需干预"
    if not snap.rect:
        return False, "没有窗口矩形"
    if not _inside(snap.rect, x, y):
        return False, "点击不在雷神窗口内"
    if not in_hot_zone(snap.rect, x, y, snap.zone):
        return False, "点击不在 ✕ 热区"
    return True, "关闭意图成立：先确保暂停再放行"


def should_prepause(snap: GuardSnapshot, x: int, y: int) -> tuple:
    """判断该点是否**有资格**触发「提前暂停」（方案 B）。

    它的价值是把用户的等待时间降到 0：绝大多数情况下，用户真正点下去时
    状态已经是 PAUSED，吞点分支只需重放即可。

    ⚠️ 比 `should_swallow` 多一条**前台窗口**要求，这一点很重要：
    预暂停会真的把加速停掉。如果只按矩形几何判断，用户从别的程序上把光标
    掠过雷神右上角所在的屏幕区域，就会莫名其妙地**打断正在进行的加速**。
    因此必须要求「雷神当前就是前台窗口」——此时用户的光标确实在雷神身上。

    真正调用暂停前，消费端还会再做一次跨进程核查
    （`WindowFromPoint` 的根窗口必须是雷神、光标仍在该区域内），
    并在钩子侧再叠加一个「停留时长」门槛，避免快速划过就触发。
    """
    if snap is None or not snap.armed:
        return False, "钩子不可用"
    if not snap.enabled:
        return False, "关闭保护未开启"
    if snap.escaping:
        return False, "用户已选择放行"
    if snap.state not in UNSAFE_STATES:
        return False, f"当前状态 {snap.state} 无需干预"
    if not snap.rect:
        return False, "没有窗口矩形"
    if snap.hwnd and snap.foreground != snap.hwnd:
        return False, "雷神不在前台，光标只是路过"
    if not in_hover_zone(snap.rect, x, y, snap.zone):
        return False, "光标未进入 ✕ 邻域"
    return True, "光标已接近 ✕，可提前暂停"


def describe_zone(rect: tuple, zone: dict | None = None) -> dict:
    """把热区算成可读信息，用于日志与 Inspector 展示。"""
    z = merge_zone(zone)
    hot = zone_rect(rect, z)
    hov = hover_rect(rect, z)
    return {
        "rect": list(rect) if rect else None,
        "hot_zone": list(hot),
        "hover_zone": list(hov),
        "hot_center": [(hot[0] + hot[2]) // 2, (hot[1] + hot[3]) // 2],
        "zone_cfg": z,
    }
