"""规格书 §三十五「最终验收标准」的自动化版本：测试 A/B/C/D 全部针对靶机执行。

  测试 A  PAUSED          关闭 → 正常放行
  测试 B  RUNNING         关闭 → 阻止 → 自动暂停 → 验证 PAUSED → 放行关闭
  测试 C  UNKNOWN         关闭 → 阻止，且**不做任何点击**
  测试 D  RUNNING+暂停失败  关闭 → 阻止（不许自动放行）
  测试 E  拦截可逆性      停止保护后 ✕ 必须恢复可用（绝不能把用户的雷神锁死）

关于「怎么模拟用户点 ✕」——本机执行环境有两处硬限制，必须如实处理：
  1. 合成鼠标输入被系统拦截（SendInput 返回 0 / UIPI），无法模拟真人点击；
     早前在允许输入注入的上下文里，tests/probe_close_protection.py 已用**真实
     鼠标点击**验证过：禁用 SC_CLOSE 后点 ✕ 窗口存活且收不到 WM_CLOSE，
     对照组则正常关闭（报告见 tests/out/close_protection_report.txt）。
  2. 程序投递的 SC_CLOSE **不会**被灰色菜单拦住（Windows 只在用户点击 ✕ /
     Alt+F4 这条系统链路里查菜单状态），所以不能用它验证拦截。

因此本测试用两种可观测事实来判定拦截是否真的武装好了：
  · GetMenuState 读到的系统菜单「关闭」命令状态（系统真值）；
  · 关闭意图进入保护流程后的行为与靶机侧证据（窗口存活 / 是否被点过按钮）。
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness as H                                   # noqa: E402
from core.events import EventKind                     # noqa: E402


def _wait_state(sink, want: str, timeout: float = 12.0):
    return H.wait_until(lambda: sink.last_status().get("state") == want, timeout)


def _trigger_close(rep: H.Report, engine, hwnd) -> str:
    """触发一次「用户要关闭雷神」。返回实际使用的模式。

    选路径前必须看清一件事：**真实点击 ✕ 只有在系统 ✕ 仍然可用时才会产生
    关闭请求**。老机制（禁用系统菜单）会把 ✕ 变成死的，此时点它什么都不会
    发生 —— 于是「点了没反应」既不是产品失败、也不是拦截生效，而是这条验证
    路径本身失效了。原来的写法只看「合成输入能不能用」这一个不稳定的探针，
    结果是：合成输入恰好可用时走真实点击 → 点在一个灰掉的 ✕ 上 → 用例误报失败。
    所以这里把「系统 ✕ 是否可用」也纳入判据，并对降级原因如实标注。
    """
    if H.sc_close_disabled(hwnd) is False and H.synthetic_input_works(hwnd):
        rep.info("使用真实鼠标点击 ✕")
        H.click_close(hwnd, times=2)
        return "real_click"
    if H.sc_close_disabled(hwnd) is True:
        rep.info("系统 ✕ 已被关闭保护禁用（点它不会有任何反应），"
                 "改用「意图队列注入 + 真实 WM_NCHITTEST」"
                 "（只替代内核派发硬件事件这一段，坐标与命中测试都是真实的）")
    else:
        rep.info("本环境禁止合成鼠标输入（UIPI），改用「意图队列注入 + 真实 WM_NCHITTEST」")
    if H.inject_close_intent(engine):
        return "injected_intent"
    return "failed"


def case_a(rep: H.Report) -> None:
    rep.section("测试 A：PAUSED → 关闭 → 正常放行")
    proc, hwnd = H.start_mock("PAUSED")
    engine = None
    try:
        engine, sink = H.make_engine(H.test_config())
        engine.start()
        if not _wait_state(sink, "PAUSED", 12):
            rep.check("识别为 PAUSED", False, f"实际 {sink.last_status().get('state')}")
            return
        rep.check("识别为 PAUSED", True, sink.last_status().get("state_detail", ""))
        rep.check("系统菜单「关闭」已恢复可用（允许关闭）",
                  H.sc_close_disabled(hwnd) is False)
        rep.check("引擎决策为 ALLOW", sink.last_status().get("decision") == "ALLOW",
                  str(sink.last_status().get("decision")))
        H.post_sc_close(hwnd)
        rep.check("关闭成功", bool(H.wait_until(lambda: not H.alive(hwnd), 8)))
        rep.check("未发起暂停（PAUSED 不该点击）",
                  sink.count(EventKind.PAUSE_REQUESTED) == 0)
        rep.check("靶机没有被点过按钮", "mock_button_clicked" not in H.mock_event_names())
        noticed = H.wait_until(
            lambda: sink.has(EventKind.CLOSE_ALLOWED) or sink.has(EventKind.LEIGOD_GONE), 6)
        rep.check("引擎确认退出且时长已暂停", bool(noticed))
        rep.check("没有产生「未保护关闭」告警",
                  not sink.has(EventKind.WINDOW_CLOSED_UNPROTECTED))
    finally:
        if engine:
            engine.stop()
        proc.terminate()


def case_b(rep: H.Report) -> None:
    rep.section("测试 B：RUNNING → 关闭 → 阻止 → 暂停 → 验证 → 放行关闭")
    proc, hwnd = H.start_mock("RUNNING")
    engine = None
    try:
        engine, sink = H.make_engine(H.test_config())
        engine.start()
        if not _wait_state(sink, "RUNNING", 12):
            rep.check("识别为 RUNNING", False, f"实际 {sink.last_status().get('state')}")
            return
        rep.check("识别为 RUNNING", True, sink.last_status().get("state_detail", ""))
        rep.check("系统菜单「关闭」已被禁用（✕ 点不动）",
                  H.sc_close_disabled(hwnd) is True)
        rep.check("引擎决策为 BLOCK_RUNNING",
                  sink.last_status().get("decision") == "BLOCK_RUNNING",
                  str(sink.last_status().get("decision")))
        mode = _trigger_close(rep, engine, hwnd)
        rep.check("关闭意图已产生", mode != "failed", mode)
        # 不能立刻断言：引擎按 tick 消费意图（默认 200ms），这里必须等它跑到
        rep.check("关闭请求被拦截并进入保护流程",
                  bool(H.wait_until(lambda: sink.has(EventKind.CLOSE_REQUESTED), 6)))
        rep.check("发起过暂停", bool(H.wait_until(
            lambda: sink.has(EventKind.PAUSE_REQUESTED), 6)))
        rep.check("暂停成功（经重新识别确认）", bool(H.wait_until(
            lambda: sink.has(EventKind.PAUSE_SUCCEEDED), 20)),
            sink.pauses[-1].line() if sink.pauses else "无暂停记录")
        if sink.pauses:
            p = sink.pauses[-1]
            rep.check("暂停前后状态 RUNNING→PAUSED",
                      p.before.value == "RUNNING" and p.after.value == "PAUSED", p.line())
        names = H.mock_event_names()
        rep.check("靶机确实被调用过按钮", "mock_button_clicked" in names, f"{names[-6:]}")
        rep.check("靶机状态变为 PAUSED", H.read_mock_state() == "PAUSED",
                  f"靶机状态={H.read_mock_state()}")
        rep.check("暂停后放行并关闭", bool(H.wait_until(lambda: not H.alive(hwnd), 12)))
        rep.check("日志含 CLOSE_ALLOWED", sink.has(EventKind.CLOSE_ALLOWED))
    finally:
        if engine:
            engine.stop()
        proc.terminate()


def case_c(rep: H.Report) -> None:
    rep.section("测试 C：UNKNOWN → 关闭 → 阻止，且不做任何点击")
    proc, hwnd = H.start_mock("RUNNING", label="◆◆")
    engine = None
    try:
        engine, sink = H.make_engine(H.test_config())
        engine.start()
        if not _wait_state(sink, "UNKNOWN", 12):
            rep.check("识别为 UNKNOWN", False, f"实际 {sink.last_status().get('state')}")
            return
        rep.check("识别为 UNKNOWN", True, sink.last_status().get("state_detail", ""))
        rep.check("系统菜单「关闭」已被禁用（Fail Safe）", H.sc_close_disabled(hwnd) is True)
        rep.check("引擎决策为 BLOCK_UNKNOWN",
                  sink.last_status().get("decision") == "BLOCK_UNKNOWN",
                  str(sink.last_status().get("decision")))
        mode = _trigger_close(rep, engine, hwnd)
        rep.check("关闭意图已产生", mode != "failed", mode)
        rep.check("日志含 CLOSE_BLOCKED", bool(
            H.wait_until(lambda: sink.has(EventKind.CLOSE_BLOCKED), 6)))
        rep.check("弹出阻止提示", any("阻止关闭" in n.title for n in sink.notices),
                  sink.notices[-1].title if sink.notices else "无")
        time.sleep(1.5)
        rep.check("窗口仍然存活（关闭被阻止）", H.alive(hwnd))
        rep.check("靶机没有收到 WM_CLOSE", "mock_wm_close" not in H.mock_event_names())
        rep.check("UNKNOWN 下没有执行任何点击",
                  "mock_button_clicked" not in H.mock_event_names(),
                  f"{H.mock_event_names()[-6:]}")
        # —— 关键：拦截必须可逆 ——
        engine.stop()
        engine = None
        time.sleep(0.5)
        rep.check("停止保护后系统菜单「关闭」恢复可用",
                  H.sc_close_disabled(hwnd) is False)
        H.post_sc_close(hwnd)
        rep.check("停止保护后可以正常关闭（不会把用户的雷神锁死）",
                  bool(H.wait_until(lambda: not H.alive(hwnd), 6)))
    finally:
        if engine:
            engine.stop()
        proc.terminate()


def case_d(rep: H.Report) -> None:
    rep.section("测试 D：RUNNING + 暂停失败 → 阻止关闭（不自动放行）")
    proc, hwnd = H.start_mock("RUNNING", label="暂停时长")   # 文案可识别，但点击不改变状态
    engine = None
    try:
        engine, sink = H.make_engine(H.test_config())
        engine.start()
        if not _wait_state(sink, "RUNNING", 12):
            rep.check("识别为 RUNNING", False, f"实际 {sink.last_status().get('state')}")
            return
        rep.check("识别为 RUNNING", True, sink.last_status().get("state_detail", ""))
        mode = _trigger_close(rep, engine, hwnd)
        rep.check("关闭意图已产生", mode != "failed", mode)
        got = H.wait_until(lambda: sink.has(EventKind.PAUSE_FAILED), 30)
        rep.check("暂停失败被检出", bool(got),
                  sink.pauses[-1].line() if sink.pauses else "无")
        rep.check("日志含 CLOSE_BLOCKED", bool(
            H.wait_until(lambda: sink.has(EventKind.CLOSE_BLOCKED), 8)))
        # 决策必须**立刻**反映到状态上，不能等下一轮 tick 走完。
        # 本机一轮 tick 含整窗 OCR 要 1.3s 以上，所以「滞后一轮」与「即时」在
        # 耗时上差一个数量级 —— 用这一点做判别，既稳定又有区分力。
        # （原先的写法是固定 sleep(2) 再读，恰好卡在边界上 → 间歇性读到旧决策。）
        t_blocked = time.time()
        got_decision = H.wait_until(
            lambda: sink.last_status().get("decision") == "BLOCK_PAUSE_FAILED", 8)
        lag = time.time() - t_blocked
        time.sleep(2)
        rep.check("窗口仍然存活（未自动放行）", H.alive(hwnd))
        rep.check("系统菜单「关闭」保持禁用", H.sc_close_disabled(hwnd) is True)
        rep.check("引擎决策为 BLOCK_PAUSE_FAILED", bool(got_decision),
                  f"状态里读到 {sink.last_status().get('decision')}"
                  f"｜引擎内部 {engine.close.last_report.decision.value}")
        rep.check("决策即时上报（不等下一轮 tick，滞后 < 1s）", lag < 1.0,
                  f"滞后 {lag:.2f}s（一轮 tick 约 1.3s 以上）")
        if sink.pauses:
            rep.check("已按配置重试到上限", sink.pauses[-1].attempts >= 2,
                      f"尝试 {sink.pauses[-1].attempts} 次")
        clicks = H.mock_event_names().count("mock_button_clicked")
        rep.check("确实尝试过暂停（点过按钮）", clicks >= 1, f"点击 {clicks} 次")
        rep.check("靶机状态未被误改", H.read_mock_state() == "RUNNING")
    finally:
        if engine:
            engine.stop()
        proc.terminate()


def main() -> int:
    from core.logging_setup import setup_logging
    setup_logging("INFO", console=False)
    rep = H.Report("雷神时长保护伴侣 —— 关闭保护端到端验收（针对仿雷神靶机）")
    print(rep.title, flush=True)
    H.kill_stale_mocks()
    case_a(rep)
    case_b(rep)
    case_c(rep)
    case_d(rep)
    path = rep.save("e2e_report.txt")
    fails = sum(1 for l in rep.lines if "[FAIL]" in l)
    print(f"\n结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}；报告 {path}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
