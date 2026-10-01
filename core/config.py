"""配置：加载 / 保存 / 点号访问。

对应规格书 §二十六，保留其 JSON 结构（general / duration / game_monitor /
ui / close_protection），并补充 leigod / detection 两节用于记录本机实测信息。

设计要点：
  - 点号访问（cfg.get("duration.max_retries", 3)），调用方不必层层解包。
  - 写盘用「临时文件 + 替换」，避免掉电/崩溃写出半截 JSON。
  - JSON 非法时不静默丢配置，而是记录 LAST_ERROR 由上层提示用户。
"""
from __future__ import annotations

import copy
import json
import os
import threading

from core.paths import base_dir

#: 配置一律放在「程序所在目录」（打包后是 exe 旁边，源码运行时是项目根目录），
#: 绝不能基于 __file__ —— 单文件 exe 的 __file__ 指向临时解包目录，会被系统清掉。
BASE_DIR = base_dir()
CONFIG_DIR = os.path.join(BASE_DIR, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "general": {
        "auto_start": False,          # 开机自启（随雷神同伴启动）
        "notifications": True,
        "log_level": "INFO",
        "single_instance": True,
        "snooze_minutes": 30,
        "follow_leigod": True,        # 雷神退出后本程序自动退出（--follow）
        "follow_exit_seconds": 90,
    },
    "leigod": {
        "exe_path": r"D:\LeiGod_Acc\leigod.exe",
        "launcher_path": r"D:\LeiGod_Acc\leigod_launcher.exe",
        "process_patterns": ["leigod.exe"],
        # 主窗口类名候选：Electron 默认是 Chrome_WidgetWin_1；留空则只用进程+标题判定
        "window_class_candidates": ["Chrome_WidgetWin_1"],
        "window_title_keywords": ["雷神"],
        "min_window_size": [300, 200],
        "start_timeout_seconds": 90,  # 启动器等待雷神主窗口的上限
        # 严格绑定等待上限（秒）：雷神进程在，但**没有**窗口匹配
        # window_class_candidates / window_title_keywords 时，最多先等这么久，
        # 再退而求其次用"弱匹配"的窗口。
        # 为什么需要等：真机实测（2026-09-30 13:06），守望在雷神启动途中唤起守卫，
        # 此时 Electron 的辅助顶层窗口 Chrome_WidgetWin_0(1920x1130) 已可见，
        # 而真主窗口 Chrome_WidgetWin_1(1500x938) 还要 2 秒才建好。旧实现会
        # 静默挑最大的顶层窗口 → 绑错 → UIA 只枚举到 6 个控件、OCR 报"不在前台"、
        # 状态永久 UNKNOWN。宁可多等 15 秒，也不要绑错窗口。
        "strict_bind_wait_seconds": 15,
        "client_version": "11.3.2.9",
        "client_kind": "electron",
    },
    "detection": {
        # 识别优先级（规格书 §七/§八/§九）
        "order": ["ui_automation", "ocr"],
        "ocr_enabled": True,
        # 顶栏裁剪区域（相对窗口整体，用于 OCR）。
        # 真机实测（tests/probe_real_crop.py，对 real_inspector/leigod_full.png 复算，
        # 窗口 1500x938 / DPI 120）：
        #   bottom=0.14  → 8 行、判定 PAUSED ✅
        #   bottom=0.065 → 0 行、判定 UNKNOWN ❌（区域仅 51px 高，RapidOCR 检不出文字）
        #   0.075 / 0.085 / 0.090 / 0.095 → 0 行 ❌
        #   0.080 → 只有 1 行且把「开启时长」误识成「并起时长」❌
        # 结论：极窄条带对 RapidOCR 极不稳定（检测阶段需要足够的垂直上下文），
        # 不能为了「少框进无关文字」而把区域压扁——那会让识别链静默退化成 UNKNOWN。
        # 多识别出的游戏分类标签（主机游戏/本地游戏/平台下载…）不含
        # 暂停 / 开启 / 时长 任一关键词，对 classify_text 无影响，故采用 0.14。
        "topbar_crop": {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14},
        # 截图方式：auto = 先 PrintWindow，无效再用屏幕截取；printwindow / screen 强制指定
        "capture": "auto",
        # 屏幕截取要求雷神在前台（避免截到被遮挡的画面）
        "require_foreground_for_screen_grab": True,
        "use_color_check": False,     # 颜色只作辅助证据，默认不参与判定
        "save_debug_captures": False,
        # OCR 调度：auto = UIA 已给出确定结论时跳过整窗 OCR（省 1.3s/轮）；
        #            always = 每轮都跑（最保守，排障时用）；off = 从不跑。
        # 注意 auto 下仍会每 60s 强制刷一次，以保证按钮定位几何不过期。
        #
        # ⚠️ 这一项是**全局默认档**：`DurationDetector` 在构造时读走它，
        # `detect()` 未显式传 `ocr_mode` 时一律用它。之前它只对「状态轮询」
        # 生效、暂停链路仍写死 always，导致每次「确保暂停」白跑 4~8 次整窗 OCR
        # （真机上就是「点了 ✕ 要等很久才暂停」）。以后再加性能开关，
        # 请一律做成这种「实例默认值」，不要指望每个调用点都记得传参。
        "ocr_mode": "auto",
    },
    "duration": {
        "verify_timeout_ms": 3000,    # 点击后等待并确认状态的总超时
        "max_retries": 3,             # 最大尝试次数
        "retry_interval_ms": 1500,    # 每次尝试之间的间隔
        # 常态轮询识别的间隔。原先 2000ms：那时每轮都要跑整窗 OCR（1.3s+），
        # 状态最快也要 2 秒才更新一次，用户感受就是「识别延迟高」。
        # OCR 改成 auto（UIA 定论就跳过）之后单轮只要几十毫秒，1000ms 很轻松。
        # 状态识别节拍（毫秒）。用户要求「0.5~1 秒确认一次状态」，故取 500ms。
        # 为什么敢调这么快：**贵的那一步不按它来**。
        #   · 全量枚举控件树（真机 657ms）由 `ui_automation.CACHE_TTL` 门控，
        #     与轮询间隔无关 —— 缩短轮询**不会**让全量扫描变多；
        #   · OCR 由 `ocr_mode=auto`（UIA 定论即跳过）与 60s 几何刷新门控；
        #   · 每轮真正做的是"重读 1~2 个控件的 Name"，实测 0.3ms。
        # 也就是说：把 1000 调成 500，只多了每秒一次的微秒级读取，CPU 几乎不变。
        "poll_ms": 500,
        # 自适应轮询：PAUSED 时把间隔放大（安全状态，晚知道毫无风险）；
        # UNKNOWN 放大 1.5 倍；RUNNING 保持原速（危险状态，必须跟得紧）。
        # 为什么要做：即便走了控件缓存，每次识别仍要跨进程读属性、
        # 每 3 秒还要一次全量扫描 —— 用户反馈的「后台占用高」主要来自这里。
        "adaptive_poll": True,
        "coordinate": {
            "ratio": None,            # 相对窗口宽高的比例坐标（校准写入，优先）
            "pos": None,              # 相对窗口左上角像素偏移
            "hold_ms": 120,           # 鼠标按下保持时长
            "calibration": {}         # 校准元数据：hwnd/class/size/dpi/verified/ts
        },
    },
    "game_monitor": {
        "enabled": True,
        "exit_wait_seconds": 30,      # 游戏全部退出后等待多久才暂停（规格书 §十七）
        "processes": [],              # 追加监控的游戏进程名（小写，子串匹配）
        "fullscreen_heuristic": False,
    },
    "ui": {
        "follow_leigod": True,        # 伴生面板跟随雷神窗口
        # 引擎 tick 节拍，同时也是面板跟随的兜底节拍。
        # ⚠️ 它是**状态轮询的上限**：`_poll_state` 只在 tick 里被调用，
        # tick 1000ms 时，即使把 poll_ms 调到 500 也跑不出 0.5s 的节拍
        # （用户要求 0.5~1 秒确认一次，所以两者必须一起降到 500）。
        # 它同时负责刷新「死手开关」快照，必须显著小于 swallow_deadman_ms(2500)。
        "refresh_ms": 500,
        "dock_enabled": True,
        "start_minimized": False,
        # 钉住当前一侧：一旦选定停靠侧就**永不自动换边**，只跟着雷神平移。
        # 治「吸附度不高、乱跳」：旧判据会在雷神拖到屏幕边缘时把面板甩到对面，
        # 拖动过程中看起来就是面板在两个位置之间跳。钉住后位置是可预测的，
        # 想换边由用户点面板上的「⇄ 换到另一侧」显式决定。
        "pin_side": True,
        # 几何快跟周期（毫秒）。状态轮询（duration.poll_ms，默认 1000ms）不够快：
        # 面板位置若跟着它走，拖动雷神时面板会滞后半秒再"啪"地归位。
        # 这条定时器只做「读一次 GetWindowRect + 移动窗口」，不查进程、不做识别。
        "follow_fast_ms": 150,
    },
    "standby": {
        # 待命守望：一个只做「雷神在不在 / 守卫在不在」的极轻常驻进程。
        # 它在雷神出现的那一刻把守卫以**可见面板**拉起，实现「开雷神即唤起」。
        # 为什么不能直接靠计划任务的 onlogon 解决，见 launcher/watch.py 顶部注释。
        "auto_wake": True,            # 发现雷神就唤起守卫
        # 守望轮询间隔。3 秒足够（开雷神是人的动作，晚 3 秒唤起无感），
        # 而每次轮询都要做一次进程枚举 + 窗口枚举，别为了"快"白烧 CPU。
        "poll_ms": 3000,
    },
    "close_protection": {
        "enabled": True,
        "block_when_unknown": True,       # 规格书 §十四 情况3
        "block_when_pause_failed": True,  # 情况4
        # ---- 拦截方式 ----
        # 真机实测（docs/真机发现-关闭保护架构问题.md §2.3）：
        #   · 雷神自绘 ✕ 点下去**不关窗**，只弹应用内确认框（最小化到托盘 / 真的退出）
        #   · ✕ 由渲染进程处理，**不走** WM_SYSCOMMAND/SC_CLOSE
        #   · 整窗 WM_NCHITTEST 恒返回 HTCLIENT → 依赖 HTCLOSE 的旧判据恒假
        # 因此主路径改为**输入层**：吞掉 ✕ 热区的点击 → 先确保暂停 → 再重放点击放行。
        # disable_system_menu 降级为「系统关闭路径（Alt+F4 / 任务栏右键）」的兜底。
        "method": "input_swallow",
        "swallow_close_click": True,      # 关闭保护主路径（**必须提权**，否则无效）
        # 悬停预暂停：光标在 ✕ 旁停留 dwell 毫秒后**提前**暂停，把用户等待降到 0。
        # 默认**关闭**，因为它会真的停掉加速：用户在雷神前台把光标挪到右上角
        # 歇一下（很自然的动作）就会被打断加速。开启后再叠加两条防护：
        # ① 必须雷神当前是前台窗口 ② 必须在 ✕ 邻域停留满 prepause_dwell_ms。
        "prepause_on_hover": False,
        "prepause_dwell_ms": 300,
        "prepause_states": ["RUNNING", "UNKNOWN"],
        "swallow_deadman_ms": 2500,       # 消费端无响应时自动停止吞点（离手保护）
        # 启动宽限（秒）：启动后这段时间内死手窗口直接给到上限。
        # 为什么需要：首轮 tick 要等 Chromium 建好无障碍树并构造 OCR 引擎
        # （真机实测 10s 量级），而默认窗口只有 2.5s → 启动后立刻降级一次，
        # 恰好落在"刚启动、用户最可能点 ✕"的时段。
        # 为什么不做成全局初值：那会让消费端真死时也吞 10 秒点击（用户被锁死）。
        "startup_grace_seconds": 15,
        # 死手窗口的上限。真正的窗口 = clamp(实测 tick 间隔 × 3, swallow_deadman_ms,
        # swallow_deadman_max_ms)：死手要防的是消费端**死了**，不是它**正忙**。
        # 一次 tick 里有一次整窗 OCR（本机实测 1.3s 量级），固定 2.5s 会被「忙」顶穿，
        # 让吞点周期性失效（点了 ✕ 有时拦有时不拦）。所以按实测间隔自适应放宽，
        # 但保留上限：消费端真死了，最迟这么久一定停止吞点。
        "swallow_deadman_max_ms": 10000,
        # 关闭意图的「新鲜度」上限。同样必须大于消费端两次消费之间的间隔，否则
        # 事件会因为「排在慢步骤后面」被判过期 —— 实测本机 tick 被整窗 OCR 拖到
        # 1.4s、固定 1.5s 卡在临界点，表现为「注入成功但引擎毫无反应」。
        # 实际窗口 = clamp(2×实测消费间隔 + 0.3, intent_max_age_ms, intent_max_age_max_ms)。
        "intent_max_age_ms": 1500,
        "intent_max_age_max_ms": 5000,
        "close_hot_zone": {
            # 真机实测（Leigod v11.3.2.9 / 窗口 1500x938 / DPI 120）：
            #   ✕ 中心 (2003,318) → 距右边 27px、距顶边 18px
            #   → 中心比例 = (1 - 27/1500, 18/938) = (0.982, 0.019)
            "cx": 0.982, "cy": 0.019,
            "half_w": 0.020, "half_h": 0.024,   # 半宽 30px / 半高 22px
            "min_half_px": 18,                  # 小窗口下的保底尺寸
            "hover_pad_px": 26,                 # 悬停预警的外扩像素
        },
        "replay_marker": 0x4C474452,      # 'LGDR'：标记本程序重放的输入，避免被自己吞掉
        # ---- 层级3：确认框监测 ----
        # 真机实测：点 ✕ 不关窗，只弹雷神自己的确认框（「最小化到托盘」/「真的退出」）。
        # 本层**不与用户的选择对抗**，而是在确认框出现的瞬间**确保总时长已暂停**，
        # 于是用户无论选哪一项都不再损失时长；同时兜住层级2 被绕过的情形
        # （触摸屏、远程桌面、托盘右键菜单退出、或本程序钩子没起来的时候）。
        # 真机形态（独立顶层窗口 vs Electron 页内绘制）尚未观测，故两条通路都实现：
        #   winevent → SetWinEventHook 找该 pid 的新顶层窗口
        #   ocr      → 截主窗口图，按「两组关键词同时命中」判定（只命中一组视为误报）
        "confirm_dialog": {
            "enabled": True,
            "mode": "auto",                   # auto | winevent | ocr | off
            "pause_on_dialog": True,          # 发现确认框即确保暂停
            # OCR 通路（要整窗截图 + 识别，成本高）的扫描间隔。主循环本来就要为
            # 状态识别做 OCR，这里**不能**再高频加一路，否则明显拖慢主循环。
            # 另外 OCR 只在「雷神是前台窗口」时才会真的跑（见 confirm_dialog.py）。
            "poll_ms": 2000,
            "min_confidence": 0.4,
            "require_both": True,             # 必须同时命中「最小化」与「退出」两组词
            "keywords_minimize": ["最小化到托盘", "最小化"],
            "keywords_exit": ["真的退出", "确定退出", "退出程序"],
        },
        "detect_close_intent": True,      # 旧鼠标钩子（仅记录，不吞点；保留兼容）
        "auto_close_after_pause": True,   # 暂停成功后自动放行关闭
        "reapply_interval_ms": 2000,      # 周期性重申拦截状态（防止被外部重置）
    },
}

LAST_ERROR = None

#: 本次加载所做的配置迁移说明（`load_config` 填充）。
#: 存在的意义：配置升级**必须让用户看得见**——静默改掉用户配置同样是「假装成功」。
MIGRATION_NOTES: list = []

#: 旧版 `close_protection.method` → 新语义的迁移表。
#:
#: 背景：`method` 这个键诞生时，「禁用系统菜单」与「输入层吞点」被当成**二选一**。
#: 真机实测后两者关系变成**分层**：输入层是 ✕ 的主路径，禁用系统菜单只是
#: 「Alt+F4 / 任务栏右键」的兜底。于是 `method` 的旧值必须重新解释，
#: 否则「磁盘上那份旧 config.json」会让 v2 主路径**完全不启动**，
#: 而面板仍显示「关闭保护已启用」——静默失效，最坏的一类缺陷。
_LEGACY_METHOD_MAP = {
    "disable_system_menu": "input_swallow",
}


def _migrate(data: dict) -> list:
    """把旧版配置就地升级到当前结构，返回迁移说明列表。

    只在「确实是旧结构」时才动手，绝不覆盖用户的显式选择：
    若用户已显式写过 `swallow_close_click`，则视为他已经表态，不再迁移。
    """
    notes: list = []
    cp = data.get("close_protection")
    if not isinstance(cp, dict):
        return notes

    legacy = cp.get("method")
    if legacy in _LEGACY_METHOD_MAP and "swallow_close_click" not in cp:
        cp["method"] = _LEGACY_METHOD_MAP[legacy]
        notes.append(
            f"close_protection.method：{legacy} → {cp['method']}"
            "（该机制现在只作系统关闭路径的兜底；✕ 的主路径是输入层，需要开启）")
    return notes


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _dig(d: dict, path: str, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _plant(d: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


class Config:
    """线程安全的配置容器：引擎线程与 UI 线程共享同一实例。"""

    def __init__(self, data: dict = None, path: str = CONFIG_PATH):
        self._lock = threading.RLock()
        # 迁移必须在**合并默认值之前**做：合并之后所有默认键都会出现，
        # 「用户是否显式写过某个键」这个信息就丢了，迁移判据会失效。
        raw = copy.deepcopy(data) if isinstance(data, dict) else {}
        self.migration_notes = _migrate(raw)
        self._data = _merge(DEFAULT_CONFIG, raw)
        self.path = path

    # ---------- 读 ----------
    def get(self, path: str, default=None):
        with self._lock:
            return _dig(self._data, path, default)

    def raw(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    # ---------- 写 ----------
    def set(self, path: str, value, save: bool = True) -> None:
        with self._lock:
            _plant(self._data, path, value)
            if save:
                self.save()

    def update(self, **kw) -> None:
        with self._lock:
            self._data.update(kw)
            self.save()

    def save(self, path: str = None) -> None:
        target = path or self.path
        with self._lock:
            data = copy.deepcopy(self._data)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)


def load_config(path: str = CONFIG_PATH) -> Config:
    """读取配置；文件不存在则按默认值生成一份（便于用户直接改）。"""
    global LAST_ERROR, MIGRATION_NOTES
    LAST_ERROR = None
    MIGRATION_NOTES = []
    data = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            LAST_ERROR = (f"config.json 不是合法 JSON（{e}）。"
                          "Windows 路径里的反斜杠必须写成 \\\\，例如 "
                          "D:\\\\LeiGod_Acc\\\\leigod.exe")
        except OSError as e:
            LAST_ERROR = f"config.json 读取失败：{e}"
    cfg = Config(data, path)
    MIGRATION_NOTES = list(cfg.migration_notes)
    if not os.path.exists(path):
        try:
            cfg.save(path)
        except OSError:
            pass
    elif cfg.migration_notes:
        # 迁移结果必须落盘，否则下次启动又要再迁一遍（且用户看到的文件与运行行为不一致）
        try:
            cfg.save(path)
        except OSError:
            pass
    return cfg
