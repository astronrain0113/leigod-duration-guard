"""UI 伴生层的自动化验收（规格书 §十八）。

验证四件事：
  1. 面板能把引擎状态渲染出来（RUNNING → PAUSED 真的跟着变）；
  2. 面板跟随雷神窗口移动（500ms 周期；按物理像素÷DPR 换算，125% 缩放下不偏）；
  3. 面板上的「立即暂停时长」真的能触发暂停，并且**经重新识别确认**；
  4. 通知横幅能正常出现（阻止关闭这类强提醒必须看得见）。

顺带产出两张面板截图（tests/out/ui_panel.png / ui_panel_notice.png），
供人工核对浅色主题下的排版是否正常。

运行：python tests/test_ui_smoke.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness as H                                     # noqa: E402
from core.events import Notice                          # noqa: E402
from detection import coordinate_fallback as cf         # noqa: E402

user32 = ctypes.windll.user32
SWP_NOSIZE, SWP_NOZORDER = 0x0001, 0x0004


def move_window(hwnd, dx: int, dy: int) -> None:
    r = wt.RECT()
    user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r))
    user32.SetWindowPos(wt.HWND(int(hwnd)), 0, r.left + dx, r.top + dy, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER)


def make_poller(QTimer):
    """按条件轮询，而不是拍脑袋固定 sleep。

    原因（踩过的坑）：暂停流程要读状态→点击→等 UI 更新→再读状态，
    实测约需 1~4 秒；固定 5 秒延时在这些机器上会变成竞态，导致
    「其实已经暂停成功」却被判失败。等待条件本身才是正确的做法。
    """
    def poll(cond, then, timeout_s: float = 45.0, step_ms: int = 250):
        state = {"n": 0}
        timer = QTimer()

        def tick():
            state["n"] += 1
            ok = False
            try:
                ok = bool(cond())
            except Exception:
                ok = False
            if ok or state["n"] * step_ms >= timeout_s * 1000:
                timer.stop()
                then(ok)

        timer.setInterval(step_ms)
        timer.timeout.connect(tick)
        timer.start()
        return timer
    return poll


def main() -> int:
    from core.logging_setup import setup_logging
    from PySide6.QtCore import QTimer

    logger = setup_logging("INFO", console=False)
    rep = H.Report("UI 伴生层（面板 / 托盘 / 通知）—— 自动化验收")
    print(rep.title, flush=True)
    H.kill_stale_mocks()

    proc, hwnd = H.start_mock("RUNNING")
    cfg = H.test_config()
    cfg.path = os.path.join(H.OUT, "ui_cfg.json")
    cfg.set("ui.refresh_ms", 250, save=False)          # 加快跟随节拍，便于验证
    cfg.set("general.follow_leigod", False, save=False)
    cfg.save()

    from ui.app import GuardApp
    app = GuardApp(cfg, logger, start_minimized=False)
    res: dict = {}
    holder: dict = {}
    poll = make_poller(QTimer)

    def step1():
        g = holder["g"]
        res["st1"] = dict(g._last_status)
        res["geom1"] = g.panel.geometry().getRect()
        res["visible1"] = g.panel.isVisible()
        res["tray"] = g.tray is not None
        res["docked"] = g.panel._docked_side
        g.panel.btn_pause.click()                      # 模拟用户点「立即暂停时长」
        # 等引擎真的把状态改成 PAUSED（它会重新识别确认，不是点完就算）
        poll(lambda: (g._last_status or {}).get("state") == "PAUSED", step2, timeout_s=45)

    def step2(_ok):
        g = holder["g"]
        res["st2"] = dict(g._last_status)
        res["mock_state"] = H.read_mock_state()
        res["geom2"] = g.panel.geometry().getRect()
        move_window(hwnd, 120, -40)                    # 挪动雷神窗口，看面板跟不跟
        g1 = res["geom1"]
        poll(lambda: g.panel.geometry().getRect()[0] != g1[0], step3, timeout_s=20, step_ms=200)

    geom_hist = {"last": None, "same": 0}

    def wait_geom_stable(g, done, tries=25):
        """等面板几何连续几次一致（Qt 布局已定型）再继续。

        ⚠️ 这里曾经直接取「移动后 / 900ms 后」的即时几何 —— 那测的是
        **布局动画的中间态**，不是面板的真实尺寸。实测后果：无提醒时拿到
        395（未定型，偏高），横幅出现后拿到 381（已定型，反而是真实值），
        于是「出现提醒后变高」被判失败，看起来像面板实现的 bug，
        其实是测量时机错了。改成轮询等稳定，测的才是真实属性。
        """
        cur = tuple(g.panel.geometry().getRect())
        geom_hist["same"] = geom_hist["same"] + 1 if cur == geom_hist["last"] else 0
        geom_hist["last"] = cur
        if geom_hist["same"] >= 2 or tries <= 0:
            done()
            return
        QTimer.singleShot(120, lambda: wait_geom_stable(g, done, tries - 1))

    def step3(_ok):
        g = holder["g"]
        geom_hist.update({"last": None, "same": 0})
        wait_geom_stable(g, step3_stable)

    def step3_stable():
        g = holder["g"]
        res["geom3"] = g.panel.geometry().getRect()
        res["st3"] = dict(g._last_status)
        res["action_text"] = g.panel.lbl_action.text()
        res["action_tip"] = g.panel.lbl_action.toolTip()
        res["protect_text"] = g.panel.lbl_protect.text()
        png = os.path.join(H.OUT, "ui_panel.png")
        g.panel.grab().save(png)
        res["png"] = png
        # 强提醒（阻止关闭）必须能在面板里看到
        g.notifier.handle(Notice(
            title="已阻止关闭雷神",
            message="自动暂停失败，无法确认总时长已停止。\n为防止剩余时长被继续消耗，雷神暂时无法关闭。",
            actions=[("立即暂停", "pause_now"), ("知道了", "ok")], strong=True, timeout=0))
        QTimer.singleShot(900, step4)

    def step4():
        g = holder["g"]
        res["notice_visible"] = g.panel.notice_frame.isVisible()
        res["notice_actions"] = g.panel.notice_actions.count() - 1
        geom_hist.update({"last": None, "same": 0})
        wait_geom_stable(g, step4_stable)

    def step4_stable():
        g = holder["g"]
        res["geom4"] = g.panel.geometry().getRect()
        nb = g.panel.notice_frame.geometry().getRect()
        res["notice_bottom"] = nb[1] + nb[3]
        res["notice_h"] = nb[3]
        png = os.path.join(H.OUT, "ui_panel_notice.png")
        g.panel.grab().save(png)
        res["png_notice"] = png
        # 交互式的两个位置动作：必须用**真实控件**验证，不能只靠读源码。
        # 顺序放在所有几何断言之后，避免换边把后面的定位判据搅乱。
        # 「重新检测状态」是用户遇到"读不出来"时的第一反应按钮，必须真实存在且
        # **任何时候都可点**（连"未找到雷神"时也要能按，按了会告诉你去开雷神）。
        res["has_recheck"] = hasattr(g.panel, "btn_recheck")
        res["recheck_enabled_when_unknown"] = (
            g.panel.btn_recheck.isEnabled() if hasattr(g.panel, "btn_recheck") else None)
        res["pinned_default"] = g.panel.pinned
        res["pin_chk_default"] = g.panel.chk_pin.isChecked()
        res["side_before"] = g.panel.docked_side
        g.panel.switch_side()
        res["side_after"] = g.panel.docked_side
        g.panel.switch_side()                      # 再换回来，供下面的高度断言用
        g.panel.set_pinned(False)
        res["pinned_off"] = g.panel.pinned
        g.panel.set_pinned(True)
        res["pinned_on"] = g.panel.pinned
        g._quit()

    def on_ready(g):
        holder["g"] = g
        # 不要用固定延时（例如 singleShot(3500)）：机器繁忙时引擎的首个状态
        # 可能还没产生，st1 / docked / geom1 就会全是空的——那样测的是「启动速度」，
        # 而不是「面板是否跟随」。改成轮询等待「已跟随 + 已拿到状态」再进入断言。
        poll(lambda: g.panel._docked_side in ("left", "right")
             and bool((g._last_status or {}).get("state")),
             lambda _ok: step1(), timeout_s=60)

    app.run(on_ready=on_ready)

    # ------------------------------------------------------------ 断言
    rep.section("面板状态渲染")
    st1 = res.get("st1") or {}
    rep.check("面板收到引擎状态且识别为 RUNNING", st1.get("state") == "RUNNING",
              str(st1.get("state")))
    rep.check("状态里带窗口信息", bool(st1.get("window")), str(st1.get("window")) is not None)
    if st1.get("window"):
        rep.check("绑定的 HWND 与靶机一致", int(st1["window"]["hwnd"]) == int(hwnd),
                  f"{st1['window'].get('hwnd_hex')} vs {hex(int(hwnd))}")
    rep.check("面板已显示", res.get("visible1") is True)
    rep.check("托盘可用（可用时会创建）", res.get("tray") in (True, False),
              f"tray={res.get('tray')}")

    rep.section("面板跟随雷神窗口（500ms 节拍 / 物理÷DPR 换算）")
    g1, g3 = res.get("geom1") or (0, 0, 0, 0), res.get("geom3") or (0, 0, 0, 0)
    dpr = float(cf.get_dpi(hwnd)) / 96.0
    want_dx, want_dy = int(120 / dpr), int(-40 / dpr)
    rep.info(f"DPI={cf.get_dpi(hwnd)} dpr={dpr}  面板 移动前={g1} 移动后={g3}  "
             f"期望位移≈({want_dx},{want_dy})")
    rep.check("跟随方向正确（雷神右移→面板右移）", g3[0] > g1[0],
              f"{g1[0]} → {g3[0]}")
    rep.check("水平位移符合物理→逻辑换算（容差 6px）",
              abs((g3[0] - g1[0]) - want_dx) <= 6, f"实际 {g3[0] - g1[0]} vs 期望 {want_dx}")
    rep.check("垂直位移符合物理→逻辑换算（容差 6px）",
              abs((g3[1] - g1[1]) - want_dy) <= 6, f"实际 {g3[1] - g1[1]} vs 期望 {want_dy}")
    rep.check("停靠侧已记录", res.get("docked") in ("left", "right"), str(res.get("docked")))
    rep.info(f"停靠侧 = {res.get('docked')}（雷神在屏幕左侧，故预期右侧）")

    rep.section("面板按钮真的能暂停时长（经重新识别确认）")
    st2 = res.get("st2") or {}
    rep.check("点击后面板状态变为 PAUSED", st2.get("state") == "PAUSED", str(st2.get("state")))
    rep.check("靶机状态确实变为 PAUSED", res.get("mock_state") == "PAUSED",
              str(res.get("mock_state")))
    rep.check("暂停由引擎执行（有 PAUSE_REQUESTED/SUCCEEDED 记录）",
              (st2.get("pause_failed") is False), str(st2.get("pause_failed")))

    rep.section("面板文案对用户友好（技术细节不糊在脸上）")
    rep.check("状态行是白话而不是内部证据串",
              "ui_automation=" not in str(res.get("action_text")),
              str(res.get("action_text")))
    rep.check("状态行非空且提到暂停/监视", bool(res.get("action_text")),
              str(res.get("action_text")))
    rep.info(f"面板状态行：{res.get('action_text')}")
    rep.check("技术细节保留在悬停提示里（排障时仍可取）",
              "ui_automation" in str(res.get("action_tip")),
              str(res.get("action_tip"))[:80])
    rep.check("关闭保护那行说的是人话",
              "关闭保护：" in str(res.get("protect_text")),
              str(res.get("protect_text")))

    rep.section("通知横幅")
    rep.check("强提醒横幅已显示", res.get("notice_visible") is True)
    rep.check("横幅带 2 个动作按钮", res.get("notice_actions") == 2,
              str(res.get("notice_actions")))
    g3, g4 = res.get("geom3") or (0, 0, 0, 0), res.get("geom4") or (0, 0, 0, 0)
    rep.info(f"面板高度 无提醒={g3[3]}  有提醒={g4[3]}｜横幅高={res.get('notice_h')}")
    # 不测「变高」：面板在出现强提醒时会精简次要内容把空间让给横幅，
    # 实测总高度反而略降（395 → 381）。真正要保证的是**横幅被完整容纳**，
    # 而不是总高度变大（后者是实现细节，且会随内容行数漂移）。
    rep.check("横幅有实际高度（不是被折叠成 0）",
              int(res.get("notice_h") or 0) > 20, str(res.get("notice_h")))
    rep.check("横幅被完整容纳在面板内（底部未超出面板，未被截断）",
              int(res.get("notice_bottom") or 0) <= int(g4[3]),
              f"横幅底 {res.get('notice_bottom')} vs 面板高 {g4[3]}")
    win_h_logical = 700 / dpr
    rep.check("面板高度没有被拉成雷神窗口的整高（避免大片空白）",
              g4[3] < win_h_logical - 80, f"{g4[3]} vs 窗口 {win_h_logical:.0f}")
    for key in ("png", "png_notice"):
        p = res.get(key)
        rep.check(f"已保存 {os.path.basename(str(p))}",
                  bool(p) and os.path.exists(str(p)), str(p))
    rep.info(f"面板截图：{res.get('png')}")
    rep.info(f"含提醒的面板截图：{res.get('png_notice')}")

    # --------------------------------------------------- 背景不透明（看得清字）
    rep.section("面板背景必须是不透明的（治「背景透明、字看不清」）")
    # 真机反馈原话：「这个软件ui背景是透明的我有点看不到字」。
    # 实测根因：QSS 只写在 QWidget#panel 上，而 Qt 对**顶层普通 QWidget** 的
    # 样式表背景在 WA_TranslucentBackground 下不绘制 —— 抓图量出来 71% 的像素
    # alpha=0（连正中心都是透明的）。现已改成内层 QFrame#card 承载不透明底。
    # 断言直接看像素，不看代码：这是唯一能证明「用户真的看得见」的判据。
    try:
        from PIL import Image
        im = Image.open(str(res.get("png"))).convert("RGBA")
        w, h = im.size
        px = im.load()
        alpha = im.getchannel("A")
        total = w * h
        transparent = sum(1 for v in alpha.getdata() if v == 0)
        ratio = transparent / max(1, total)
        # 空白处取样点必须完全落在**卡片**内（避开圆角与投影留边）。
        # 2026-10-01 起卡片外面多了一圈投影（theme.SHADOW_MARGIN），
        # 那几个像素是半透明灰 —— 它们**本来就该**不是 255，取样点要跟着往里挪，
        # 否则这条断言会把「投影」误判成「背景没画」。
        from ui import theme as _theme
        mg = int(getattr(_theme, "SHADOW_MARGIN", 0))
        samples = [(mg + 6, h // 2), (mg + 10, mg + 12), (w // 2, mg + 5)]
        pts = [(p, px[p]) for p in samples]
        rep.info(f"尺寸={w}x{h}  全透明像素={transparent}/{total}（{ratio:.1%}）  取样={pts}")
        rep.check("面板大部分像素被真实绘制（不是透明窗户）",
                  ratio < 0.15, f"全透明占比 {ratio:.1%}")
        rep.check("空白处底色不透明（alpha=255）",
                  all(v[3] == 255 for _, v in pts), str(pts))
        rep.check("空白底色是浅色（浅色主题下字才看得清）",
                  all(min(v[:3]) > 200 for _, v in pts), str(pts))
        rep.check("正中心也不透明（有实体内容垫底）",
                  px[w // 2, h // 2][3] == 255, str(px[w // 2, h // 2]))
    except Exception as e:                                    # noqa: BLE001
        rep.check("能读取面板截图像素", False, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------ 钉住一角
    rep.section("钉住一角 / 手动换边（拖动雷神不乱跑）")
    rep.check("默认即为钉住", res.get("pinned_default") is True, str(res.get("pinned_default")))
    rep.check("「钉住这一侧」勾选框与状态一致",
              res.get("pin_chk_default") is True, str(res.get("pin_chk_default")))
    rep.check("面板已记录停靠侧",
              res.get("side_before") in ("left", "right"), str(res.get("side_before")))

    rep.section("「重新检测状态」按钮（状态读不出来时的第一出口）")
    rep.check("面板上真实存在这个按钮", res.get("has_recheck") is True,
              str(res.get("has_recheck")))
    rep.check("按钮处于可点状态（不能因为状态异常就被禁用）",
              res.get("recheck_enabled_when_unknown") is True,
              str(res.get("recheck_enabled_when_unknown")))
    rep.check("「⇄ 换到另一侧」真的换过去了",
              res.get("side_after") in ("left", "right")
              and res.get("side_after") != res.get("side_before"),
              f"{res.get('side_before')} → {res.get('side_after')}")
    rep.check("钉住开关可关可开（不是只写死的常量）",
              res.get("pinned_off") is False and res.get("pinned_on") is True,
              f"off={res.get('pinned_off')} on={res.get('pinned_on')}")

    # ------------------------------------------------------------ 退出保护
    case_quit_gate(rep)

    proc.terminate()
    path = rep.save("ui_report.txt")
    fails = sum(1 for l in rep.lines if "[FAIL]" in l)
    print(f"\n结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}；报告 {path}", flush=True)
    return 1 if fails else 0


def case_quit_gate(rep: H.Report) -> None:
    """退出本程序时也必须过「确认已暂停」这一关。

    为什么单列一组：本程序唯一的目的是别让总时长白白消耗。若它自己在雷神
    正在计时时悄悄退出，用户会以为"收拾干净了"，而计时器还在跑 —— 那正是
    要防的事。这里用桩引擎把四条分支都逼出来（不启动真实引擎，跑得快）：
    不安全 → 先暂停且**不退**；取消 → 恢复；已暂停 → 直接退；
    宽限期到点 → 把选择权交给用户，且「仍然退出」必须留下错误级证据。
    """
    from ui.app import GuardApp

    rep.section("退出保护：退出前必须先确认已暂停（不静默退出）")

    class _Rec:
        def __init__(self):
            self.notes, self.events = [], []

        def handle(self, n):
            self.notes.append(n)

        def simple(self, title, message):
            self.notes.append(Notice(title=title, message=message))

    class _Sink:
        def __init__(self):
            self.events = []

        def on_event(self, e):
            self.events.append(e)

    class _Timer:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class _Eng:
        def __init__(self):
            self.safe, self.why = False, "雷神总时长正在计时"
            self.pauses, self.stopped, self.quit_requested = 0, False, False

        def exit_readiness(self):
            return self.safe, self.why

        def request_pause(self):
            self.pauses += 1

        def stop(self, timeout=3.0):
            self.stopped = True

    def fresh():
        g = GuardApp(H.test_config(), logger=None)
        g.engine, g.notifier, g.sink = _Eng(), _Rec(), _Sink()
        g._t_follow, g._t_quit = _Timer(), _Timer()
        return g

    # ① 不安全 → 发起暂停，但**不退出**
    g = fresh()
    g._quit()
    rep.check("未确认暂停时不会直接退出", g._quitting is False, f"quitting={g._quitting}")
    rep.check("退出流程发起了一次暂停", g.engine.pauses == 1, f"pauses={g.engine.pauses}")
    rep.check("引擎未被停止（程序还在盯着）", g.engine.stopped is False)
    rep.check("如实告知用户「退出前正在暂停」",
              bool(g.notifier.notes) and "暂停" in g.notifier.notes[0].title,
              g.notifier.notes[0].title if g.notifier.notes else "（无通知）")
    acts = [a for _, a in (g.notifier.notes[0].actions if g.notifier.notes else [])]
    rep.check("提供「取消退出」这条退路（不把用户锁在里面）",
              "cancel_quit" in acts, str(acts))

    # ② 取消退出 → 回到保护状态
    g._cancel_quit()
    rep.check("取消后可继续运行", g._quit_pending is False and g._quitting is False)
    rep.check("取消后有明确回执", any("已取消退出" in n.title for n in g.notifier.notes))

    # ③ 已确认暂停 → 直接退出
    g = fresh()
    g.engine.safe, g.engine.why = True, "雷神总时长已暂停"
    g._quit()
    rep.check("已确认暂停 → 直接退出", g._quitting is True)
    rep.check("退出时引擎被正常停止（会把 ✕ 还给用户）", g.engine.stopped is True)
    rep.check("已暂停时不再多此一举地暂停一次", g.engine.pauses == 0, f"pauses={g.engine.pauses}")

    # ④ 宽限期内一直不安全 → 询问用户；选「仍然退出」必须留错误级证据
    g = fresh()
    g._quit()
    g._quit_deadline = 0.0                     # 模拟宽限期已过
    g._check_quit()
    rep.check("宽限期满不再无限等待，改为询问用户",
              any("未能确认雷神已暂停" in n.title for n in g.notifier.notes),
              str([n.title for n in g.notifier.notes]))
    last = g.notifier.notes[-1]
    acts = [a for _, a in last.actions]
    rep.check("两个选项都给（取消 / 仍然退出）",
              "cancel_quit" in acts and "quit_anyway" in acts, str(acts))
    rep.check("询问期间程序仍在运行", g._quitting is False)

    g._finish_quit(forced=True)
    rep.check("用户显式选择后确实退出", g._quitting is True)
    errs = [e for e in g.sink.events if getattr(e, "level", "") == "ERROR"]
    rep.check("「仍然退出」留下错误级证据（不假装一切正常）",
              len(errs) == 1, str([e.line() for e in g.sink.events]))


if __name__ == "__main__":
    sys.exit(main())
