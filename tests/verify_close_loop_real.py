"""真机闭环验证：关闭保护 v2（输入层）+ 确认框形态判定。

## 为什么要把两件事合到一个脚本里

真机上有两个问题都只能靠「真人点一次 ✕」来回答，而它们消耗的是同一份人力成本：

  ① **v2 主路径在真机上是否走通**：吞点 → 确保暂停 → 重放 → 雷神弹自己的确认框；
  ② **确认框是什么形态**：独立顶层窗口（可用 `SetWinEventHook` 实时发现）
     还是 Electron 页内绘制（只能截图 + OCR）。

所以本脚本一次运行、分两个阶段，把两件事都拿到：

    阶段 1  输入层**开启**：用户点 ✕ → 观察是否吞点、是否暂停、是否重放、确认框是否出现
    阶段 2  输入层**关闭**（但确认框监测保持开启）：用户再点一次 ✕
            → 点击直达应用 → 确认框必定出现 → 判定它的形态

阶段 2 是必要的：若阶段 1 的「确保暂停」失败（例如暂停按钮还没校准），
按 Fail Safe 设计**不会重放**点击，确认框也就不会出现，阶段 1 拿不到形态结论。

## 安全前置

- **必须提权**：雷神恒以管理员运行，未提权时低层钩子与合成输入都会被 UIPI 无效化。
- 默认要求当前状态为 **PAUSED**（此时点 ✕ 不会造成任何时长损失）。
  需要连「自动暂停」一起验证时加 `--allow-running`——**那会真的去点暂停按钮，
  也会真的停掉加速**，确认可接受再用。
- 脚本只监听与点击雷神自己的 UI；**不注入、不改内存、不发网络请求**。

## 用法（需管理员）

    python tests/run_elevated.py --wait tests/out/verify_close_loop.json -- \
        tests/verify_close_loop_real.py --wait 180

运行时按屏幕/控制台提示，在雷神窗口上真人点 ✕（会被提示两次）。
结果：`tests/out/verify_close_loop.json` 与 `.txt`
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

user32 = ctypes.windll.user32
RESULT = os.path.join(HERE, "out", "verify_close_loop.json")

GA_ROOT = 2


def window_at(x: int, y: int) -> dict:
    """取屏幕上某个坐标归属的窗口。

    低层鼠标钩子只给我们坐标，不给目标窗口。而「点了 ✕ 却没反应」最常见的两种
    原因是「这次点击根本没落在雷神上」与「脚本压根没接到点击」——两者对策完全
    不同，必须能分开，所以每次点击都要顺带记一份归属窗口。

    ⚠️ **绝不给 `ctypes.windll.user32` 上的 API 赋 argtypes。**
    `ctypes.windll.user32` 在整个进程里是**同一个对象**，`detection/coordinate_fallback.py`
    在 import 时已经把 `WindowFromPoint.argtypes` 声明成 `[wt.POINT]`；这里若覆盖成
    自己的 POINT 类型，会连带把那个模块的所有调用搞挂，而且是
    `ctypes.ArgumentError` 这种看起来毫不相干的崩溃（踩过，代价是一整轮人工验证）。
    需要自定类型 → 另起一个 `ctypes.WinDLL` 实例；这里直接复用项目自己的 helper。
    """
    out = {"hwnd": 0, "root": 0, "class": "", "pid": 0, "title": "", "own": False}
    try:
        from detection import coordinate_fallback as cf
        h = int(cf.window_from_point(int(x), int(y)) or 0)
        if not h:
            return out
        root = int(user32.GetAncestor(wt.HWND(h), GA_ROOT) or h)
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, buf, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(wt.HWND(root), ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(wt.HWND(root))
        tbuf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(wt.HWND(root), tbuf, n + 1)
        out.update({"hwnd": h, "root": root, "class": buf.value,
                    "pid": int(pid.value), "title": tbuf.value})
    except Exception as e:                     # 纯取证，绝不因为它让主流程失败
        out["error"] = f"{type(e).__name__}: {e}"
    return out


class _UnknownReading:
    """状态读取失败 / 超时时的占位结论。

    §六 三态纪律：**UNKNOWN 绝不当 PAUSED** —— 读不到就是读不到，
    绝不能为了让流程往下走而假装"已暂停"。
    """

    def __init__(self, detail: str = "状态读取失败，按 UNKNOWN 处理"):
        from core.state_machine import DurationState
        self.state = DurationState.UNKNOWN
        self._detail = detail

    def summary(self) -> str:
        return self._detail


def call_with_timeout(fn, timeout: float = 10.0, name: str = ""):
    """在独立线程里跑一次调用，超时就放弃（不 join 到天荒地老）。

    为什么必须有：观测循环里任何一次「卡住」都会让整轮验证**冻死** ——
    不报错、不写日志，只是再也不刷新，用户看到的就是"提示一直不变"。
    真机上已经发生过一次（并发 OCR 把状态读取卡死 100+ 秒，期间报告零更新）。
    返回 None 表示超时；否则是 `("ok", 值)` 或 `("err", 异常文本)`。
    """
    box = {}

    def _w():
        try:
            box["v"] = ("ok", fn())
        except BaseException as e:                       # noqa: BLE001
            box["v"] = ("err", f"{type(e).__name__}: {e}")

    t = threading.Thread(target=_w, name=name or "probe", daemon=True)
    t.start()
    t.join(timeout)
    return box.get("v")


class Pump:
    """独立线程按 50ms 调 `guard.maintain()`。

    为什么必须独立线程：主循环里要做 OCR（状态识别，单次数百毫秒）。
    低层钩子的「死手开关」靠 `maintain()` 刷新，如果和 OCR 串在同一个循环里，
    主循环慢的时候死手会周期性过期 → 吞点时灵时不灵。
    """

    def __init__(self, guard, interval: float = 0.05):
        self.guard = guard
        self.interval = interval
        self.ticks = 0
        self.errors = []
        self._stop = threading.Event()
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._run, name="verify-pump", daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.guard.maintain()
                self.ticks += 1
            except Exception as e:
                self.errors.append(f"{type(e).__name__}: {e}")
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1.5)
            self._t = None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=180.0,
                    help="每个阶段等待真人点击的秒数")
    ap.add_argument("--probe-wait", type=float, default=40.0,
                    help="阶段0 钩子连通性自检的等待秒数（随便点一下屏幕）")
    ap.add_argument("--skip-probe", action="store_true",
                    help="跳过阶段0 连通性自检（不建议：出问题时无从判断）")
    ap.add_argument("--arm-wait", type=float, default=120.0,
                    help="等待你把雷神切到「计时中」的秒数（只有计时中才会触发拦截）")
    # 保留兼容：这个开关原本用来绕过「必须 PAUSED 才继续」的门槛，而那条门槛已被
    # 证明写反了（PAUSED 时按设计不拦截，等于永远测不到吞点），故改为无副作用。
    ap.add_argument("--allow-running", action="store_true",
                    help="（已废弃·保留兼容）现在本来就需要计时中才能验证拦截")
    ap.add_argument("--phase2", default="yes", choices=["yes", "no"],
                    help="是否执行阶段2（关掉吞点、专门骗出确认框看形态）")
    ap.add_argument("--out", default=RESULT)
    args = ap.parse_args()

    out_dir = os.path.dirname(os.path.abspath(args.out)) or HERE
    os.makedirs(out_dir, exist_ok=True)
    # 转录文件与心跳文件：与 --out 同目录，不随参数漂移。
    # 为什么必须要有：本脚本靠 `run_elevated.py` 提权后**另开一个控制台**运行，
    # 那个窗口的生命周期不由我们掌控（用户可能直接关掉、也可能被别的窗口盖住）。
    # 一旦控制台没了又没落盘，现象就是「跑过但什么都没有」——无从排查。
    log_path = os.path.join(out_dir, "verify_close_loop.console.log")
    beat_path = os.path.join(out_dir, "verify_close_loop.heartbeat")
    _log_fp = open(log_path, "a", encoding="utf-8", errors="replace")
    _log_fp.write("\n" + "=" * 70 + "\n")
    _log_fp.write(f"### 本次运行开始 {time.strftime('%Y-%m-%d %H:%M:%S')}"
                  f"｜pid={os.getpid()}｜argv={sys.argv[1:]}\n")
    _log_fp.flush()

    rep = {
        "ts": time.time(), "elevated": False, "phase": "boot",
        "pid": os.getpid(), "argv": sys.argv[1:], "console_log": log_path,
        "window": None, "state_before": None, "state_detail": "",
        "probe": {}, "phase1": {}, "phase2": {}, "clicks": {}, "new_windows": [],
        "guard_events": [], "confirm_recent": [], "notes": [], "verdicts": [],
        "fatal": None,
    }

    def flush():
        rep["beat"] = time.time()
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        os.replace(tmp, args.out)
        # 心跳单独一个纯文本文件：比 JSON 更容易被"是不是跑过"这个问题一眼回答。
        try:
            with open(beat_path, "w", encoding="utf-8") as f:
                st = rep.get("phase1") or {}
                f.write(f"{time.strftime('%H:%M:%S')} phase={rep.get('phase')} "
                        f"verdict={(rep.get('verdicts') or [{}])[-1].get('verdict', '')} "
                        f"clicks={st.get('clicks_total', 0)} "
                        f"in_hot={st.get('clicks_in_hot', 0)} "
                        f"swallowed={(st.get('stats') or {}).get('swallowed', 0)}\n")
        except OSError:
            pass

    def say(msg):
        print(msg, flush=True)
        rep["notes"].append(msg)
        try:
            _log_fp.write(str(msg) + "\n")
            _log_fp.flush()
        except (OSError, ValueError):
            pass

    # 第一件事就是落盘。哪怕下一行就崩，也能从文件 mtime 与 phase="boot" 确认
    # 「脚本确实启动过」——这次排查就卡在分不清这一点上。
    flush()

    rep["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    say(f"本进程提权 = {rep['elevated']}")
    if not rep["elevated"]:
        say("⚠️ 未提权：对提权运行的雷神基本无效（UIPI）。请用 run_elevated.py 运行本脚本。")

    from core.config import load_config
    from core.state_machine import DurationState
    from detection import coordinate_fallback as cf
    from leigod import window as win_mod
    from leigod.close_protection import CloseIntentGuard, is_elevated
    from leigod.duration_controller import DurationController
    from leigod.duration_detector import DurationDetector

    cfg = load_config()
    # 观测用的覆盖必须在构造 Guard **之前**写进配置：
    # Guard 的构造参数（是否吞点、层级3 的 mode）是从配置里读一次快照的。
    cfg.set("close_protection.enabled", True, save=False)
    cfg.set("close_protection.swallow_close_click", True, save=False)
    # 悬停预暂停在真机上先不开：它会真的停掉加速，会干扰对「点 ✕ 之后发生了什么」的判断
    cfg.set("close_protection.prepause_on_hover", False, save=False)
    cfg.set("close_protection.confirm_dialog.enabled", True, save=False)
    # ⚠️ 用 auto 而不是 winevent：这次的目的是**判定确认框形态**，必须让两条通路
    # 同时开着，才能拿到正面证据。
    #   只开 winevent 的话，结论只能靠"没看到新窗口"反推页内绘制 —— 那是沉默证据，
    #   钩子一旦漏掉（窗口早于钩子创建 / 被类名过滤 / 属主是别的进程）就会得出
    #   错误结论，而且事后无法分辨。
    #   两条都开：winevent 看到新顶层窗口 → 独立窗口；winevent 没看到但 OCR 在
    #   主窗口里读到确认框文案 → 页内绘制（正面确认）。两个都没有才是"无法判定"。
    cfg.set("close_protection.confirm_dialog.mode", "auto", save=False)

    win = win_mod.find_main_window(cfg)
    if not win:
        say("✗ 未找到雷神主窗口，请先启动雷神客户端")
        flush()
        return 3
    rep["window"] = win.as_dict()
    say(f"雷神主窗口 HWND=0x{win.hwnd:X} class={win.class_name} pid={win.pid} rect={win.rect}")
    if win.minimized_to_tray:
        say("检测到雷神处于「最小化到托盘」（窗口在屏幕外），尝试恢复…")

    cf.ensure_visible(win.hwnd)
    cf.activate_window(win.hwnd)
    time.sleep(0.8)
    win = win_mod.refresh(win)

    det = DurationDetector(cfg)
    ctrl = DurationController(cfg, det)
    # 启动初期也带超时：这里一卡就是"什么都没输出就消失"，最难排查。
    _first = call_with_timeout(lambda: det.detect(win, allow_ocr=True),
                               timeout=20.0, name="detect-first")
    if _first is None:
        say("⚠️ 首次状态读取超过 20s 未返回（判定为卡住），按 UNKNOWN 继续。")
        reading = _UnknownReading()
        rep["state_before"], rep["state_detail"] = "UNKNOWN", "首次读取卡住（超时 20s）"
    else:
        reading = _first[1] if _first[0] == "ok" else _UnknownReading()
        rep["state_before"] = reading.state.value
        rep["state_detail"] = reading.summary()
    say(f"当前总时长状态 = {reading.state.value}（{reading.summary()}）")
    # ⚠️ 这里曾经写反过，代价是一整轮无效人工验证 —— 记牢：
    # 钩子侧 4 条联合判据里有一条是「状态 ∈ {RUNNING, UNKNOWN}」。**PAUSED 时不吞点**
    # （没有时长在消耗 → 没有需要保护的东西 → 直接放行，这是设计意图）。
    # 于是「要求当前必须 PAUSED 以免损失时长」= **永远测不到吞点**，逻辑自相矛盾。
    # 正确做法：验证拦截的前提就是让雷神处于**计时中**，并在下面显式等这个前提成立。
    if reading.state is DurationState.RUNNING:
        say("当前状态 = RUNNING（计时中）→ 这正是验证拦截需要的前提。")
    elif reading.state is DurationState.UNKNOWN:
        say("当前状态 = UNKNOWN → 也会触发吞点（Fail Safe），但无法确认是否真的暂停成功。")
    else:
        say("当前状态 = PAUSED → **此时按设计不会拦截**（没有时长在消耗，无需保护）。"
            "下面会请你先把雷神切回计时状态。")
    rep["state_gate"] = {"state": reading.state.value,
                         "blocks_only_when": ["RUNNING", "UNKNOWN"]}
    # 暂停手段体检 —— 这一步直接决定闭环节点「确保已暂停」能不能跑通。
    # 真机上 UIA 常常枚举 0 个控件、比例坐标通常又没校准，唯一的依靠是 OCR 文字框定位；
    # 所以这里必须把「打算点哪里」先打出来，否则失败时无从判断是哪一环断了。
    #
    # ⚠️ 整块包在 try 里：**体检是取证手段，绝不能自己先把主流程搞死**。
    # 这一条是踩出来的：体检里的一行打印写错类型，在未提权（UIA 枚举 0 个控件）时
    # 侥幸不触发，提权后 UIA 真枚举到控件就抛 TypeError —— 而它位于所有阶段之前，
    # 于是表现为「点了 ✕ 毫无反应」，排查方向被完全带偏。
    try:
        ratio = cfg.get("duration.coordinate.ratio")
        strat, why = ctrl._strategy_available()
        rep["pause_strategy"] = {"name": strat, "why": why, "ratio": ratio}
        say(f"暂停手段：strategy={strat or '（无）'}｜{why}")
        # `last_uia` 的 `all` 是 **ControlInfo 的扁平列表**（不是"值是列表的字典"），
        # 所以计数只能 len()，不能 sum(len(v) ...)。
        all_uia = list(det.last_uia.get("all") or [])
        rep["pause_strategy"]["uia_controls"] = len(all_uia)
        say(f"  相对坐标校准 ratio={ratio}｜UIA 枚举到的控件数={len(all_uia)}"
            f"｜顶栏过滤={'已启用' if det.last_uia.get('filtered') else '未启用（未传裁剪区）'}")
        # 关键：把**候选本身**打出来，而不是只打数量。
        # 「1 个暂停时长 + 20 个开启时长」这种分布，只有看到 name+rect 才能判断
        # 谁是真按钮、谁是列表噪声 —— 只报"证据冲突"等于没报。
        for _kind, _label in (("pause", "暂停时长→RUNNING"), ("start", "开启时长→PAUSED")):
            for _c in (det.last_uia.get(_kind) or [])[:5]:
                say(f"      UIA[{_kind}] {_label}：name={getattr(_c, 'name', '')!r} "
                    f"type={getattr(_c, 'control_type', '')} "
                    f"rect={getattr(_c, 'rect', None)} "
                    f"offscreen={getattr(_c, 'is_offscreen', None)} "
                    f"invoke={getattr(_c, 'supports_invoke', None)}")
        if (det.last_uia.get("pause") and det.last_uia.get("start")):
            say(f"      ⚠️ 过滤后仍有冲突：暂停 {len(det.last_uia['pause'])} 个 / "
                f"开启 {len(det.last_uia['start'])} 个 → 本轮判 UNKNOWN。"
                "若雷神正处于「点完开启时长→变红色暂停时长」的 1~2 秒过渡态，"
                "属正常，下一轮就会稳定下来。")
    except Exception as e:
        rep["pause_strategy"] = {"error": f"{type(e).__name__}: {e}"}
        say(f"  ⚠️ 暂停手段体检自身失败（{type(e).__name__}: {e}），"
            "不影响后续观测，但缺少这一环的判断依据。")

    try:
        want = (DurationState.PAUSED if reading.state is DurationState.PAUSED
                else DurationState.RUNNING)
        ocr_rect = det.button_rect_on_screen(want)
        ocr_ctr = det.button_center_on_screen(want)
        rep["ocr_button"] = {"for_state": want.value, "rect": ocr_rect, "center": ocr_ctr,
                             "geom": det.last_ocr_geom, "lines": det.last_ocr_lines}
        if ocr_ctr:
            f = (det.last_ocr_geom or {}).get("frame") or [0, 0, 0, 0]
            rel = ""
            if f[2] - f[0] > 0 and f[3] - f[1] > 0:
                rel = (f"；相对窗口 ≈ ({(ocr_ctr[0] - f[0]) / (f[2] - f[0]):.3f}, "
                       f"{(ocr_ctr[1] - f[1]) / (f[3] - f[1]):.3f})")
            say(f"  OCR 按钮定位（{want.value}）= rect {ocr_rect}，中心 {ocr_ctr}{rel}"
                " —— 暂停会点这里")
        else:
            label = "暂停时长" if want is DurationState.RUNNING else "开启时长"
            say(f"  ✗ OCR 没能定位到「{label}」按钮文字框 → 暂停这一步大概率会失败。"
                "下面是本轮 OCR 原文，用来判断是裁剪区域不对、认错字还是关键词没覆盖：")
            if not det.last_ocr_lines:
                say("      （一行都没有 —— 极可能是截图被 UIPI 拦了或窗口被遮挡）")
            for ln in (det.last_ocr_lines or []):
                say(f"      conf={ln.get('confidence')} bbox={ln.get('bbox')} "
                    f"text={ln.get('text')!r}")
    except Exception as e:
        rep["ocr_button"] = {"error": f"{type(e).__name__}: {e}"}
        say(f"  ⚠️ OCR 按钮定位体检自身失败（{type(e).__name__}: {e}）。")

    pause_log = []

    def read_state(timeout: float = 10.0):
        """读一次状态，**带硬超时**。返回 (DurationState, 说明)。

        超时一律按 UNKNOWN 处理（§六 三态纪律：UNKNOWN 绝不当 PAUSED），
        并且把「读不到」的**具体原因**带回去 —— 最常见的原因是雷神不在前台
        （截屏护栏生效），那和「雷神没在计时」是完全不同的两件事，
        不分开报的话用户只会一直看到同一句提示，无从下手。
        """
        got = call_with_timeout(lambda: det.detect(win, allow_ocr=True),
                                timeout=timeout, name="detect")
        if got is None:
            return DurationState.UNKNOWN, f"状态读取超过 {timeout:.0f}s 未返回（判定为卡住）"
        kind, val = got
        if kind == "err":
            return DurationState.UNKNOWN, f"状态读取异常：{val}"
        return val.state, val.summary()

    def ensure_paused() -> DurationState:
        """真实的「确保已暂停」：读状态 → 不 PAUSED 就点暂停 → 重新检测。

        整块包 try：这个函数是**守卫的暂停回调**，在防护链路里被调用。
        它要是抛异常，整条闭环会当场断掉，而现象只是「点了 ✕ 没反应」——
        排障方向会被彻底带偏。宁可记录失败、按 Fail Safe 不放行，也不能炸。
        """
        rec = {"t": round(time.time(), 3)}
        pause_log.append(rec)
        try:
            return _ensure_paused_inner(rec)
        except Exception as e:
            rec["action"] = "暂停回调自身抛异常 → 按 Fail Safe 不放行"
            rec["error"] = f"{type(e).__name__}: {e}"
            say(f"  ⚠️ 暂停回调异常：{type(e).__name__}: {e}")
            return DurationState.UNKNOWN

    def _ensure_paused_inner(rec: dict) -> DurationState:
        rec.update({"state_before": r.state.value, "detail": r.summary()})
        if r.state is DurationState.PAUSED:
            rec["action"] = "已是 PAUSED，未点击"
            return DurationState.PAUSED
        if r.state is DurationState.UNKNOWN:
            rec["action"] = "状态未知，按 §十四 不点任何东西"
            return DurationState.UNKNOWN
        out = ctrl.pause(win)
        rec["action"] = "已执行暂停"
        rec["success"] = bool(out.success)
        rec["detail2"] = out.detail
        say(f"  暂停尝试：success={out.success}｜{out.detail}")
        return DurationState.PAUSED if out.success else DurationState.RUNNING

    # ---- 输入层：吞点 + 层级3：确认框监测 ----
    guard = None
    pump = None
    try:
        guard = CloseIntentGuard(cfg, logger=None)
        guard.bind(win.hwnd)
        if guard.confirm is not None:
            guard.confirm.bind(win.hwnd, win.pid)
        guard.set_state(reading.state)
        guard.set_pause_callback(ensure_paused)
        started = guard.start()
        rep["hook"] = {"started": started, "installed": guard.watcher.installed,
                       "error": guard.watcher.error,
                       "confirm": guard.confirm.status() if guard.confirm else None}
        say(f"输入层钩子 = {guard.watcher.installed}"
            f"（{guard.watcher.error or 'ok'}）；层级3 钩子 = "
            f"{(guard.confirm.listener.installed if guard.confirm and guard.confirm.listener else False)}")
        if guard.confirm is not None:
            cs = guard.confirm.status()
            say(f"层级3 通路：mode={cs.get('mode')}｜winevent 监听={cs.get('hook_installed')}"
                f"｜OCR 扫描线程={cs.get('ocr_thread')}"
                f"（两条都要 True 才能正面判定确认框形态）")
        if not guard.watcher.installed:
            say("✗ 低层鼠标钩子没装上，闭环节点 ① 无法验证。请确认以管理员运行。")
        pump = Pump(guard)
        pump.start()
        time.sleep(0.6)                        # 让 maintain() 先武装死手开关

        r0 = cf.get_window_rect(win.hwnd)
        import leigod.close_intent as ci
        hot = ci.zone_rect(tuple(r0), guard.zone)
        rep["hot_zone"] = list(hot)
        say(f"✕ 热区（按窗口相对比例算出）= {hot}；窗口矩形 = {r0}")

        # 热区必须落在窗口矩形内，否则「怎么也吞不到点」是几何错误而不是识别错误。
        # 这两种原因的修法完全不同，所以这里就判掉，别留到事后猜。
        if not (r0[0] <= hot[0] and hot[2] <= r0[2]
                and r0[1] <= hot[1] and hot[3] <= r0[3]):
            say(f"⚠️ ✕ 热区 {hot} 没有被窗口矩形 {r0} 完全包含 —— "
                "窗口可能仍处于托盘/异形位置，点 ✕ 永远吞不到。")
        if r0[0] < -10000 or r0[1] < -10000:
            say(f"⚠️ 窗口矩形 {r0} 在屏幕外（仍是最小化到托盘状态）。"
                "请手动点开雷神窗口后重跑。")

        # ---------------- 阶段 0：钩子连通性自检 ----------------
        # 为什么必须有这一步：低层鼠标钩子「收不到输入」和「收到了但这个点击不该吞」
        # 在现象上完全一样 —— 都是「点了 ✕ 毫无反应」。不先分开这两件事，
        # 后面所有阶段都在盲猜。判据很便宜：请用户随便点一下屏幕任意位置即可。
        if args.skip_probe:
            say("按参数跳过阶段0 连通性自检。")
        else:
            say("=" * 70)
            say("阶段 0／3：钩子连通性自检")
            say("请**随便点一下屏幕上任何地方**（桌面空白处就行，不要点 ✕）。")
            say("目的：确认这个进程真的能收到鼠标输入。")
            say("=" * 70)
            rep["phase"] = "probe"
            flush()
            t_probe = time.time()
            got = []
            while time.time() - t_probe < args.probe_wait:
                got.extend(guard.watcher.pop_clicks())
                if got:
                    break
                flush()
                time.sleep(0.15)
            rep["probe"] = {
                "waited": round(time.time() - t_probe, 2),
                "clicks": len(got),
                "elevated": rep["elevated"],
                "hook_installed": guard.watcher.installed,
                "hook_error": guard.watcher.error,
                "points": [{"x": x, "y": y,
                            "window_class": (window_at(x, y) or {}).get("class"),
                            "window_pid": (window_at(x, y) or {}).get("pid")}
                           for (x, y, _t) in got[:5]],
            }
            if got:
                say(f"✓ 钩子已收到 {len(got)} 次点击 → 输入层通路正常。"
                    f"（首点坐标 {got[0][0]},{got[0][1]}）")
                for (x, y, _t) in got[:3]:
                    wa = window_at(x, y)
                    say(f"    点 ({x},{y}) 落在 class={wa.get('class')} "
                        f"pid={wa.get('pid')} title={wa.get('title')!r}")
                flush()
            else:
                say(f"✗ {args.probe_wait:.0f}s 内**一次鼠标点击都没收到** —— "
                    "这就是「点了 ✕ 毫无反应」的直接原因，不用再往下走。")
                say(f"  钩子安装状态 = {guard.watcher.installed}"
                    f"；错误 = {guard.watcher.error or '（无）'}")
                if not rep["elevated"]:
                    say("  最可能的原因：**本进程未提权**。雷神恒以管理员运行，"
                        "未提权的进程装得上钩子、却收不到对高完整性级别窗口的输入。")
                    say("  → 请用项目根目录的 `2-真机闭环.bat`（会自动请求管理员）。")
                else:
                    say("  已提权却仍收不到点击，可能是：")
                    say("   · 你在这段时间里确实没点任何地方（重跑一次，按提示点）；")
                    say("   · 安全软件/输入法拦截了全局鼠标钩子；")
                    say("   · 点击落在了一个独立的全屏独占程序上。")
                rep["probe"]["verdict"] = "no_input"
                rep["verdicts"].append({"phase": "probe", "verdict": "no_input",
                                        "reason": "钩子收不到任何鼠标点击"})
                flush()
                return 6
            rep["probe"]["verdict"] = "ok"
            rep["verdicts"].append({"phase": "probe", "verdict": "ok",
                                    "reason": f"收到 {len(got)} 次点击"})

        def drain(phase: dict) -> None:
            """把钩子侧与层级3 的观测搬进报告（每轮都刷，方便外部实时查看）。

            注意：**吞掉的点击不会到达任何窗口**，所以不能靠「靶机/雷神收到点击」取证。
            这里用的是钩子自己记录的原始按下事件（`pop_clicks`），
            它在决定吞不吞之前就已经记下来了 —— 这才是「用户确实点了 ✕」的证据。

            `phase["stats"]` 存的是**本阶段增量**（guard.stats 是跨阶段累计的，
            直接用累计值会让阶段2 误判「有吞点」）。
            """
            for (x, y, _ts) in guard.watcher.pop_clicks():
                phase["clicks_total"] = phase.get("clicks_total", 0) + 1
                inzone = bool(hot[0] <= x <= hot[2] and hot[1] <= y <= hot[3])
                if inzone:
                    phase["clicks_in_hot"] = phase.get("clicks_in_hot", 0) + 1
                wa = window_at(x, y)
                wa["on_leigod"] = bool(int(wa.get("root") or 0) == int(win.hwnd))
                if wa["on_leigod"]:
                    phase["clicks_on_leigod"] = phase.get("clicks_on_leigod", 0) + 1
                pts = phase.setdefault("click_points", [])
                pts.append({"x": x, "y": y, "in_hot": inzone,
                            "window_class": wa.get("class"),
                            "window_pid": wa.get("pid"),
                            "window_title": wa.get("title"),
                            "on_leigod": wa["on_leigod"],
                            "t_rel": round(_ts - (phase.get("started") or _ts), 3)})
                del pts[:-10]
            base = phase.get("_stats_base") or {}
            phase["stats"] = {k: int(v) - int(base.get(k, 0))
                              for k, v in guard.stats.items()}
            phase["watcher"] = guard.watcher.status()
            cw = guard.confirm.status() if guard.confirm else {}
            rep["confirm_recent"] = cw.get("recent_new", [])
            if cw.get("stats"):
                phase["confirm_stats"] = cw["stats"]
            # 层级3 的**本阶段增量**（winevent 命中 / OCR 命中 / 检查次数）。
            # 判定确认框形态靠的就是这几个数：`ocr_hits` 是"页内绘制"的正面证据。
            if cw:
                cbase = phase.get("_confirm_base") or {}
                phase["confirm"] = {
                    "mode": cw.get("mode"),
                    "hook_installed": cw.get("hook_installed"),
                    "ocr_thread": cw.get("ocr_thread"),
                    "last_detail": cw.get("last_detail"),
                    "stats": {k: int(v) - int(cbase.get(k, 0))
                              for k, v in (cw.get("stats") or {}).items()},
                }
            rep["guard_events"] = list(guard.events)[-60:]
            rep["errors"] = guard.errors[-5:]
            if pump is not None:
                rep["pump"] = {"ticks": pump.ticks, "errors": pump.errors[:3]}

        def decide(phase_key: str):
            """按证据下结论 —— 只描述**观测到的事实**，不做乐观推断。"""
            p1 = rep[phase_key]
            st = p1.get("stats", {})
            sw, rp, bl = st.get("swallowed", 0), st.get("replayed", 0), st.get("blocked", 0)
            in_hot = p1.get("clicks_in_hot", 0)
            if phase_key == "phase1":
                if sw == 0:
                    # 「没吞」有两种完全不同的成因，必须分开报：
                    #   ① 压根没收到落在热区的点击；② 收到了，钩子按判据**决定放行**。
                    # 加这一句是因为真机上出现过第 ② 种（状态 PAUSED → 放行），
                    # 只报"没有吞点记录"会让人以为是钩子坏了。
                    w = p1.get("watcher") or {}
                    reason = w.get("last_reason") or ""
                    if (p1.get("clicks_in_hot") or 0) > 0 or int(w.get("passed") or 0) > 0:
                        return None, (f"点击已收到，但钩子**主动放行**了"
                                      f"（放行 {w.get('passed', 0)} 次；"
                                      f"钩子给出的理由：{reason or '（未记录）'}）"
                                      "—— 说明联合判据有一条没满足，"
                                      "最常见是「当前状态不在 {RUNNING, UNKNOWN} 之内」。")
                    return None, "还没有吞点记录（还没有落在 ✕ 热区内的点击）"
                if bl:
                    return "blocked", ("吞点成功但**未能确认已暂停** → 按 Fail Safe 不放行，"
                                       "所以雷神的确认框不会出现（这是设计行为，不是故障）。"
                                       "下一步应先完成暂停按钮的真机校准。")
                if rp:
                    return "released", "吞点 → 确保暂停 → 重放 已全部发生。"
                return None, f"已吞点 {sw} 次，等待暂停与重放结果…"
            # 阶段2：判定确认框形态。两条通路同时开着，所以能拿到**正面证据**，
            # 而不是靠"没看到"反推。
            cst = (p1.get("confirm") or {}).get("stats") or {}
            wv, oc, checked = (int(cst.get("winevent_hits", 0)),
                               int(cst.get("ocr_hits", 0)),
                               int(cst.get("checked", 0)))
            if p1.get("new_windows") or wv:
                return "dialog_toplevel", ("点 ✕ 后出现了**新的顶层窗口**"
                                          f"（winevent 命中 {wv} 次）→ 确认框是独立顶层窗口。")
            if in_hot == 0 or st.get("swallowed", 0) > 0:
                return None, "尚未取得有效的点击证据（点 ✕ 应直达应用且不被吞）"
            if oc:
                return "dialog_inpage", ("winevent 没看到新顶层窗口，但 OCR 在雷神主窗口里"
                                         f"**正面读到**确认框文案（命中 {oc} 次）"
                                         "→ 确认框是 Electron 页内绘制。")
            if checked == 0:
                return None, ("点击证据已拿到，但 OCR 一次都还没检查过（checked=0，"
                              "可能雷神不在前台或被遮挡）—— 继续等…")
            return "dialog_inconclusive", (
                f"既没看到新顶层窗口，OCR 也没命中确认框文案（已检查 {checked} 次）。"
                "这**不能**直接判成页内绘制：无法区分「钩子漏了那个窗口」与"
                "「页内绘制但 OCR 认不出」。需要人眼看当时的画面。")

        def run_phase(key: str, title: str, prompt: str, wait: float):
            say("=" * 70)
            say(title)
            say(prompt)
            say("=" * 70)
            rep["phase"] = key
            ph = rep[key]
            ph["started"] = time.time()
            ph["new_windows"] = []
            ph["_stats_base"] = dict(guard.stats)     # 只统计本阶段增量
            ph["_confirm_base"] = dict(
                (guard.confirm.status().get("stats") or {}) if guard.confirm else {})
            t0 = time.time()
            last_detect = 0.0
            last_beat = 0.0
            # 用 hwnd 集合去重：recent_new 会被裁剪（>40 时丢前 20 条），
            # 按下标切片会错位，必须按 hwnd 比较。
            seen_hwnds = {w["hwnd"] for w in
                          (guard.confirm.recent_new if guard.confirm else [])}
            settled_at = None
            while time.time() - t0 < wait:
                # 状态在外层定期重读（OCR 慢，不能每轮都做）
                if time.time() - last_detect > 2.0:
                    last_detect = time.time()
                    # 带硬超时：这里若卡住，整个阶段就永远等不到结论、也不报错。
                    st_now, st_detail = read_state()
                    guard.set_state(st_now)
                    ph["state_now"] = st_now.value
                    ph["state_detail"] = st_detail
                    drain(ph)
                if guard.confirm is not None:
                    for w in guard.confirm.recent_new:
                        if w["hwnd"] in seen_hwnds:
                            continue
                        seen_hwnds.add(w["hwnd"])
                        ph["new_windows"].append(w)
                        say(f"★ 新出现的窗口 class={w['class']} title={w['title']!r} "
                            f"size={w['size']} top_level={w['top_level']} "
                            f"owner={hex(w['owner'])}")
                verdict, why = decide(key)
                ph["hint"] = why
                rep["phase_hint"] = why
                flush()
                # 每 2 秒报一次「我还活着 + 我看到了什么」。
                # 用户报「点了 ✕ 毫无反应」时，这一行能立刻分清三种情况：
                # 脚本没在跑 / 在跑但没接到点击 / 接到了但没落在 ✕ 热区。
                now_ = time.time()
                if now_ - last_beat >= 2.0:
                    last_beat = now_
                    st_ = ph.get("stats") or {}
                    say(f"  [存活 {now_ - t0:4.0f}s／{wait:.0f}s] "
                        f"收到点击 {ph.get('clicks_total', 0)} 次"
                        f"（落在 ✕ 热区 {ph.get('clicks_in_hot', 0)} 次）"
                        f"｜吞点 {st_.get('swallowed', 0)}｜重放 {st_.get('replayed', 0)}"
                        f"｜阻止 {st_.get('blocked', 0)}｜{why}")
                # 已有结论后多看 2.5s 确认稳定：避免把瞬时状态当成结论
                if verdict:
                    if settled_at is None:
                        settled_at = time.time()
                    elif time.time() - settled_at > 2.5:
                        ph["verdict"] = verdict
                        ph["verdict_reason"] = why
                        say(f"→ 阶段结论：{verdict}｜{why}")
                        drain(ph)
                        flush()
                        return verdict
                else:
                    settled_at = None
                time.sleep(0.25)
            ph["verdict"] = ph.get("verdict") or "timeout"
            ph["verdict_reason"] = ph.get("verdict_reason") or \
                f"等待 {wait:.0f}s 未得出结论：{ph.get('hint') or '没有足够证据'}"
            say(f"→ 阶段超时：{ph['verdict_reason']}")
            drain(ph)
            flush()
            return ph["verdict"]

        # ---------------- 阶段 0.5：拦截的前提是「计时中」 ----------------
        # PAUSED 时钩子按设计放行（见上方解释）。以前这一步不存在，于是用户点了 ✕
        # 却"没有任何拦截"，还以为是钩子坏了。现在把前提显式等待并告诉你它在等什么。
        t_arm = time.time()
        cur_state, cur_detail = read_state()
        guard.set_state(cur_state)
        while (cur_state is not DurationState.RUNNING
               and time.time() - t_arm < args.arm_wait):
            left = args.arm_wait - (time.time() - t_arm)
            if cur_state is DurationState.PAUSED:
                say(f"  ⏳ 雷神当前是**已暂停**（按钮显示「开启时长」）→ 按设计不会拦截。"
                    f"请**在雷神里点一下「开启时长」把它切到计时中**（剩 {left:.0f}s）…")
            else:
                # UNKNOWN 有两种截然不同的成因，提示必须不一样，否则用户只会
                # 反复看到同一句话而无从下手（真机上就这么冻了一轮）。
                if "不在前台" in cur_detail or "遮挡" in cur_detail:
                    how = (f"读不到状态的原因是：{cur_detail}。"
                           "请**把雷神窗口点回最前面**并停一下（别切到别的窗口）")
                elif "超过" in cur_detail or "卡住" in cur_detail:
                    how = f"状态读取卡住了：{cur_detail}（已按 UNKNOWN 处理，不再等待这一次）"
                elif "同时存在" in cur_detail or "冲突" in cur_detail:
                    # 真机事实：点完「开启时长」后按钮会先变灰、显示仍是「开启时长」，
                    # 约 1~2 秒后才变成红色的「暂停时长」。这段过渡期里两种控件可能
                    # 同时被 UIA 枚举到 → 判定冲突。这是**过渡态**，不是故障。
                    how = (f"两种按钮同时出现（{cur_detail}）→ 判为冲突/UNKNOWN。"
                           "若你刚点过「开启时长」，这是**1~2 秒的过渡态**，稍等即可")
                else:
                    how = f"状态 = UNKNOWN：{cur_detail}"
                say(f"  ⏳ {how}（剩 {left:.0f}s）…")
            flush()
            time.sleep(2.0)
            cur_state, cur_detail = read_state()
            guard.set_state(cur_state)
        rep["arm"] = {"state": cur_state.value, "detail": cur_detail,
                      "waited": round(time.time() - t_arm, 1)}
        if cur_state is not DurationState.RUNNING:
            say(f"✗ 等了 {args.arm_wait:.0f}s 仍不是 RUNNING（当前 {cur_state.value}）→ "
                "无法验证吞点路径。状态详情如下，据此判断下一步：")
            say(f"    {cur_detail}")
            if "不在前台" in cur_detail or "遮挡" in cur_detail:
                say("    → 看起来是**雷神没在最前面**（截屏护栏不给截）。"
                    "先把雷神点回前台，再重跑一次。")
            rep["arm"]["verdict"] = "not_running"
            flush()
            return 7
        say(f"✓ 雷神已进入计时中（{cur_state.value}）→ 现在点 ✕ **应当被拦截**。")

        # ---------------- 阶段 1：输入层开启 ----------------
        # 前提清理：上一步可能残留点击（比如关掉雷神弹出来的小框），会把本阶段计数污染。
        _resid = guard.watcher.pop_clicks()
        if _resid:
            say(f"（已丢弃 {len(_resid)} 次阶段前残留点击，不计入本阶段统计）")
        v1 = run_phase(
            "phase1",
            "阶段 1／3：验证 v2 主路径（吞点 → 确保暂停 → 重放）",
            "现在请在雷神窗口上点一次右上角的 ✕。"
            "预期：点下去会有一瞬间没反应（已被吞住）→ 自动暂停 → 点击被重放"
            "→ 雷神弹出自己的确认框。**弹框后先不要动它**，等脚本给出结论。",
            args.wait)
        rep["verdicts"].append({"phase": "phase1", "verdict": v1,
                                "reason": rep["phase1"].get("verdict_reason", "")})
        rep["phase1"]["pause_log"] = pause_log
        flush()

        # ---------------- 阶段 2：关掉吞点，专门骗出确认框看形态 ----------------
        if args.phase2 == "yes":
            if guard.watcher.installed:
                # 只关「吞点」，**保留层级3** 的窗口监听（这正是要观测的东西）。
                # 不用 set_enabled()，因为它会连层级3 一起停掉。
                guard.set_swallow(False)
                say("已关闭输入层吞点（层级3 的窗口监听保持开启），"
                    "接下来这次点击会**直达应用**。")
                say("如果上一阶段雷神弹出了确认框，请**现在**把它处理掉"
                    "（选「最小化到托盘」），然后再点 ✕。")
                # 处理弹框的那几次点击属于上一阶段的余波，不能算进阶段2。
                time.sleep(1.0)
                _r2 = guard.watcher.pop_clicks()
                if _r2:
                    say(f"（已丢弃 {len(_r2)} 次收尾残留点击）")
                flush()
                v2 = run_phase(
                    "phase2",
                    "阶段 2／3：判定确认框形态（独立顶层窗口 vs 页内绘制）",
                    "请再点一次雷神右上角的 ✕。这次不会有任何拦截，"
                    "雷神的确认框应当立刻出现。出现后**先不要**选任何按钮。",
                    args.wait)
                rep["verdicts"].append({"phase": "phase2", "verdict": v2,
                                        "reason": rep["phase2"].get("verdict_reason", "")})
                say("提示：现在可以在雷神的确认框里选「最小化到托盘」把它收起来（不损失时长）。")
                flush()
            else:
                say("跳过阶段 2：输入层钩子本来就没装上，阶段 1 的点击已直达应用。")
        else:
            say("按参数跳过阶段 2。")
    finally:
        if guard is not None:
            try:
                guard.set_swallow(True)      # 恢复配置语义后再卸载
                guard.stop()
            except Exception:
                pass
        if pump is not None:
            pump.stop()
        rep["phase"] = "done"
        rep["guard_after"] = {
            "hook_installed": (guard.watcher.installed if guard else None),
            "confirm": (guard.confirm.status() if guard and guard.confirm else None),
        } if guard is not None else None
        rep["state_after"] = None
        try:
            alive = bool(user32.IsWindow(wt.HWND(int(win.hwnd))))
            rep["window_alive_after"] = alive
            if alive:
                # 收尾读取同样带超时：它一卡，最后这份报告就永远写不出来，
                # 前面所有阶段的证据全部白拿（真机上已经冻过一次）。
                _g = call_with_timeout(lambda: det.detect(win, allow_ocr=True),
                                       timeout=10.0, name="detect-final")
                rep["state_after"] = (_g[1].state.value if _g and _g[0] == "ok"
                                      else "UNKNOWN(收尾读取超时)")
        except Exception:
            rep["window_alive_after"] = None
        rep["pause_log"] = pause_log
        flush()

    # ---------------- 汇总 ----------------
    say("=" * 70)
    say("汇总")
    say("=" * 70)
    pr = rep.get("probe") or {}
    if pr:
        say(f"  阶段0 钩子连通性 = {pr.get('verdict')}｜等 {pr.get('waited')}s 收到 "
            f"{pr.get('clicks')} 次点击｜提权={pr.get('elevated')}"
            f"｜钩子已装={pr.get('hook_installed')}")
    for v in rep["verdicts"]:
        say(f"  {v['phase']}：{v['verdict']}｜{v['reason']}")
    p1 = rep["phase1"].get("stats", {})
    say(f"  阶段1 你的点击 = {rep['phase1'].get('clicks_total', 0)} 次"
        f"（落在 ✕ 热区 {rep['phase1'].get('clicks_in_hot', 0)} 次"
        f"／落在雷神窗口上 {rep['phase1'].get('clicks_on_leigod', 0)} 次）")
    for pt in (rep["phase1"].get("click_points") or [])[-4:]:
        say(f"      点 ({pt['x']},{pt['y']}) in_hot={pt['in_hot']} "
            f"on_leigod={pt['on_leigod']} 落点窗口 class={pt['window_class']}")
    say(f"  阶段1 保护动作 = 吞点 {p1.get('swallowed', 0)}／重放 {p1.get('replayed', 0)}"
        f"／阻止 {p1.get('blocked', 0)}／确认框 {p1.get('confirm_seen', 0)}"
        f"／自动暂停 {p1.get('confirm_paused', 0)}")
    p2 = rep["phase2"].get("stats", {}) if rep["phase2"] else {}
    if p2:
        say(f"  阶段2 统计 = 吞点 {p2.get('swallowed', 0)}（应为 0，已关吞点）"
            f"／新窗口 {len(rep['phase2'].get('new_windows', []))}")
    say(f"  主窗口存活 = {rep.get('window_alive_after')}；事后状态 = {rep.get('state_after')}")
    if rep["errors"]:
        say(f"  ⚠️ worker 异常：{rep['errors']}")

    # 「观测无效」的三种成因必须分开报 —— 对策完全不同，混在一起报等于没报。
    ct, ci_ = rep["phase1"].get("clicks_total", 0), rep["phase1"].get("clicks_in_hot", 0)
    cl = rep["phase1"].get("clicks_on_leigod", 0)
    w1 = rep["phase1"].get("watcher") or {}
    if ct == 0:
        say("⚠️ 阶段1 期间**一次点击都没收到** → 本次观测无效。"
            "先看上面阶段0 的连通性结论；若阶段0 正常而在阶段1 收不到，"
            "说明你当时没点在屏幕上。")
    elif ci_ == 0:
        say(f"⚠️ 收到了 {ct} 次点击，但**没有一次落在 ✕ 热区** {tuple(rep.get('hot_zone') or [])} "
            f"→ 本次观测无效，且这就是「点了 ✕ 没反应」的原因："
            f"你以为点的是 ✕，实际坐标不在热区。请对照上面的点击坐标检查。")
    elif (w1.get("passed") or 0) > 0 and (rep["phase1"].get("stats") or {}).get("swallowed", 0) == 0:
        say(f"⚠️ ✕ 热区内的点击收到了，但钩子**主动放行了 {w1.get('passed')} 次**，"
            f"钩子给出的理由是：{w1.get('last_reason') or '（未记录）'}")
        say("   这不是故障：**联合判据要求 状态 ∈ {RUNNING, UNKNOWN}**。"
            "若理由是「当前状态 PAUSED 无需干预」，说明点 ✕ 的那一刻雷神已经不在计时了，"
            "把它切回计时中（点「开启时长」）再验证。")
    elif cl == 0:
        say(f"⚠️ 有点击落在热区内，但**没有一次落在雷神窗口上** → "
            "当时雷神不在最前面（被别的窗口盖住）。请先把雷神切到前台再点。")
    flush()
    print(f"\n报告 -> {args.out}", flush=True)

    txt = args.out[:-5] + ".txt" if args.out.endswith(".json") else args.out + ".txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(rep["notes"]))
    print(f"文本 -> {txt}", flush=True)
    print(f"转录 -> {log_path}", flush=True)
    # 提权运行时子脚本带的是独立控制台窗口，进程一退出窗口就消失，用户根本来不及
    # 看上面的汇总。检测到是交互式控制台就停一下 —— 报告文件已经写好了，直接关掉也不会丢。
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\n按回车关闭这个窗口（报告已写好，直接关掉也不会丢）... ")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        # argparse 的 --help/--version 走的就是 SystemExit(0)，那不是崩溃。
        # 没有这一条会把「看用法」误报成崩溃、还会往 error.txt 里写一坨 traceback。
        raise
    except BaseException as _e:              # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        # 崩溃也要落盘：提权后子脚本跑在**另一个控制台**里，那个窗口随时可能被关掉，
        # 只往 stderr 打一遍 traceback 等于没打（本次排查就吃过这个亏）。
        try:
            d = os.path.dirname(os.path.abspath(RESULT))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "verify_close_loop.error.txt"),
                      "w", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 崩溃\n")
                f.write(f"argv={sys.argv[1:]}\n\n{tb}\n")
            with open(os.path.join(d, "verify_close_loop.console.log"),
                      "a", encoding="utf-8", errors="replace") as f:
                f.write("\n!!! 崩溃 !!!\n" + tb + "\n")
        except Exception:
            pass
        traceback.print_exc()
        code = 9 if isinstance(_e, Exception) else 1
    sys.exit(code)

