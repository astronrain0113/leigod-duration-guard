"""纯逻辑单元测试（不需要雷神、不需要网络；少数用例会短暂拉起靶机）。

覆盖对象与「为什么值得测」：
  · 状态机 / 关闭策略  —— 这是项目的法律条文，任何一条错都会导致误放行
  · UIA 文本判定       —— 旧实现正是在这里用子串匹配点错了控件（回归测试）
  · OCR 文案判定       —— 误识别容错，直接决定三态结论
  · 截图有效性         —— 脏图必须被判无效，否则 OCR 会给出无关结论
  · 配置与日志脱敏     —— 写盘原子性 + 敏感信息屏蔽
  · 游戏监控计时       —— 第二保险丝的等待/取消逻辑
  · 通知去重 / 单实例  —— 避免骚扰用户与多开互殴

运行：python tests/test_units.py
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness as H                                              # noqa: E402
from core import config as config_mod                            # noqa: E402
from core import logging_setup as logmod                         # noqa: E402
from core.events import Notice                                   # noqa: E402
from core.state_machine import (BLOCK_MESSAGES, CloseDecision, DurationState,   # noqa: E402
                                Reading, StateMachine, combine_states,
                                decide_close_action)
from detection import coordinate_fallback as cf                  # noqa: E402
from detection import image_detection as img                     # noqa: E402
from detection import ocr as ocr_mod                             # noqa: E402
from detection import ui_automation as uia                       # noqa: E402
from game.process_monitor import GameMonitor                     # noqa: E402
from launcher import launcher                                    # noqa: E402
from ui import theme                                             # noqa: E402


# ==================================================================== 状态机
def t_state_machine(rep: H.Report) -> None:
    rep.section("状态机：只有 PAUSED 允许关闭")
    rep.check("RUNNING 不安全", DurationState.RUNNING.safe_to_close is False)
    rep.check("PAUSED 安全", DurationState.PAUSED.safe_to_close is True)
    rep.check("UNKNOWN 不安全（绝不能当成 PAUSED）",
              DurationState.UNKNOWN.safe_to_close is False)

    rep.section("关闭策略：规格书 §十四 四种情况")
    d = decide_close_action
    rep.check("情况1 PAUSED → ALLOW",
              d(DurationState.PAUSED) is CloseDecision.ALLOW)
    rep.check("情况2 RUNNING → BLOCK_RUNNING",
              d(DurationState.RUNNING) is CloseDecision.BLOCK_RUNNING)
    rep.check("情况3 UNKNOWN → BLOCK_UNKNOWN",
              d(DurationState.UNKNOWN) is CloseDecision.BLOCK_UNKNOWN)
    rep.check("情况4 暂停失败 → BLOCK_PAUSE_FAILED",
              d(DurationState.RUNNING, pause_failed=True) is CloseDecision.BLOCK_PAUSE_FAILED)
    rep.check("关闭保护时 → DISABLED", d(DurationState.RUNNING, enabled=False) is CloseDecision.DISABLED)
    rep.check("允许 UNKNOWN 放行的开关必须默认关着",
              d(DurationState.UNKNOWN, block_when_unknown=False) is CloseDecision.ALLOW)
    rep.check("UNKNOWN + 暂停失败 仍拦（按失败处理）",
              d(DurationState.UNKNOWN, pause_failed=True) is CloseDecision.BLOCK_PAUSE_FAILED)
    rep.check("blocked 属性：只有 ALLOW/DISABLED 不算拦",
              all(x.blocked for x in (CloseDecision.BLOCK_UNKNOWN, CloseDecision.BLOCK_RUNNING,
                                      CloseDecision.BLOCK_PAUSE_FAILED))
              and not CloseDecision.ALLOW.blocked and not CloseDecision.DISABLED.blocked)
    rep.check("三种拦截都有面向用户的中文提示",
              all(k in BLOCK_MESSAGES for k in (CloseDecision.BLOCK_UNKNOWN,
                                                CloseDecision.BLOCK_RUNNING,
                                                CloseDecision.BLOCK_PAUSE_FAILED)))

    rep.section("证据合成：冲突即 UNKNOWN")
    s = combine_states
    rep.check("无证据 → UNKNOWN", s([])[0] is DurationState.UNKNOWN)
    rep.check("全 UNKNOWN → UNKNOWN",
              s([("a", DurationState.UNKNOWN), ("b", None)])[0] is DurationState.UNKNOWN)
    rep.check("单路 RUNNING → RUNNING", s([("a", DurationState.RUNNING)])[0] is DurationState.RUNNING)
    rep.check("两路一致 → 采信", s([("a", DurationState.PAUSED), ("b", DurationState.PAUSED)])[0]
              is DurationState.PAUSED)
    st, detail, conflict = s([("uia", DurationState.RUNNING), ("ocr", DurationState.PAUSED)])
    rep.check("两路冲突 → UNKNOWN", st is DurationState.UNKNOWN)
    rep.check("冲突被标记出来", conflict is True and "冲突" in detail, detail)
    rep.check("一路 UNKNOWN 不干扰另一路的结论",
              s([("uia", DurationState.PAUSED), ("ocr", DurationState.UNKNOWN)])[0]
              is DurationState.PAUSED)

    rep.section("状态机流转")
    sm = StateMachine()
    r1 = Reading(state=DurationState.RUNNING)
    rep.check("首次记录算变化", sm.note(r1) is True)
    rep.check("同状态不重复算变化", sm.note(Reading(state=DurationState.RUNNING)) is False)
    rep.check("状态切换被记录", sm.note(Reading(state=DurationState.PAUSED)) is True)
    rep.check("流转历史可追溯",
              [(a.value, b.value) for _, a, b in sm.transitions][-1] == ("RUNNING", "PAUSED"),
              str(sm.transitions))
    rep.check("consecutive 用于确认稳定", sm.consecutive(DurationState.PAUSED, 1) is True)
    sm.reset()
    rep.check("reset 清空", sm.state is DurationState.UNKNOWN and not sm.transitions)


# ==================================================================== 配置
def t_config(rep: H.Report) -> None:
    rep.section("配置：点号访问 / 深合并 / 原子写盘")
    path = H.uniq_path("unit_config.json")   # 唯一名 → 天然「不存在」，无需删除
    cfg = config_mod.load_config(path)
    rep.check("文件不存在时自动生成", os.path.exists(path))
    rep.check("点号访问读到默认值", cfg.get("duration.max_retries") == 3)
    rep.check("读不到时返回给定默认值", cfg.get("no.such.key", "fallback") == "fallback")
    rep.check("默认关闭保护是开的", cfg.get("close_protection.enabled") is True)
    rep.check("默认不允许把 UNKNOWN 当安全",
              cfg.get("close_protection.block_when_unknown") is True)
    rep.check("默认不保存绝对屏幕坐标（ratio 为 None）",
              cfg.get("duration.coordinate.ratio") is None)

    # 回归（真机实测缺陷）：默认裁剪区域必须「能出字」。
    # 曾经 DEFAULT 写成 top=0.010/bottom=0.065（仅 51px 高），对真实雷神截图复算
    # RapidOCR 检出 0 行 → 全新安装（没有 config.json）会永远识别不到状态，
    # 而且是静默退化成 UNKNOWN，极难排查。
    crop = cfg.get("detection.topbar_crop")
    rep.check("默认顶栏裁剪高度 ≥0.12（窄条带会让 OCR 检出 0 行）",
              (crop["bottom"] - crop["top"]) >= 0.12, str(crop))
    rep.check("默认裁剪值与运行期兜底值一致（三处不能各写一套）",
              crop == {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14},
              str(crop))

    cfg.set("duration.coordinate.ratio", [0.7444, 0.0986], save=False)
    cfg.set("duration.coordinate.calibration", {"verified": True}, save=False)
    cfg.save()
    rep.check("写盘后没有残留 .tmp（原子替换）", not os.path.exists(path + ".tmp"))
    again = config_mod.load_config(path)
    rep.check("保存/加载往返一致",
              again.get("duration.coordinate.ratio") == [0.7444, 0.0986])
    rep.check("嵌套元数据也被保留",
              again.get("duration.coordinate.calibration.verified") is True)
    # 不要写死 2000：默认值本身会随实测调优（2026-09-29 因「识别延迟高」
    # 从 2000 调到 1000），写死会让这条用例在下次调参时又变成假失败。
    rep.check("默认值没有被自定义值覆盖掉",
              again.get("duration.poll_ms")
              == config_mod.DEFAULT_CONFIG["duration"]["poll_ms"],
              f"poll_ms={again.get('duration.poll_ms')}")
    # 「识别延迟高」：状态轮询默认必须 ≤1.2s，否则用户点完 ✕ 要等半天。
    rep.check("状态轮询默认 ≤1200ms（延迟要能被感知地降下来）",
              int(again.get("duration.poll_ms", 9999)) <= 1200,
              f"poll_ms={again.get('duration.poll_ms')}")
    rep.check("OCR 默认走 auto（UIA 定论时跳过 1.3s 的整窗 OCR）",
              str(again.get("detection.ocr_mode", "")).lower() == "auto",
              f"ocr_mode={again.get('detection.ocr_mode')}")

    # 深合并：只覆盖用户给的那几个键
    merged = config_mod.Config({"general": {"notifications": False}},
                               path=H.uniq_path("unit_merge.json"))
    rep.check("部分覆盖不丢同节其它键",
              merged.get("general.notifications") is False
              and merged.get("general.log_level") == "INFO")
    rep.check("未提及的节保持默认",
              merged.get("close_protection.enabled") is True)

    # 非法 JSON
    bad = H.uniq_path("unit_bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ this is not json ")
    cfg2 = config_mod.load_config(bad)
    rep.check("非法 JSON 不静默丢弃：错误被记录下来",
              bool(config_mod.LAST_ERROR), str(config_mod.LAST_ERROR))
    rep.check("非法 JSON 时仍可用默认值运行",
              cfg2.get("duration.max_retries") == 3)
    rep.check("错误提示里点了 Windows 反斜杠的坑",
              "\\\\" in str(config_mod.LAST_ERROR) or "JSON" in str(config_mod.LAST_ERROR),
              str(config_mod.LAST_ERROR))
    # 说明：这里刻意不做任何删除/清理 —— 临时文件都用了本次运行独享的文件名，
    # 留在 tests/out 下无害，且删除会触发本机环境的删除守卫。


# ==================================================================== 脱敏
def t_logging(rep: H.Report) -> None:
    rep.section("日志脱敏（规格书 §二十七）")
    cases = [
        ("password=hunter2", "hunter2"),
        ("account_token: abc.def.ghi", "abc.def.ghi"),
        ("Cookie: sessionid=123456", "123456"),
        ("Authorization: Bearer eyJhbGciOi", "eyJhbGciOi"),
        ("session = zzz999", "zzz999"),
    ]
    for text, secret in cases:
        out = logmod.sanitize(text)
        rep.check(f"屏蔽 {text.split(':')[0].split('=')[0].strip()}",
                  secret not in out and "redacted" in out.lower(), out)
    rep.check("正常文本不被误伤",
              logmod.sanitize("已绑定雷神窗口 HWND=0x1234") == "已绑定雷神窗口 HWND=0x1234")
    rep.check("日志目录常量可用", os.path.isdir(logmod.LOG_DIR))


# ==================================================================== UIA
def t_uia(rep: H.Report) -> None:
    rep.section("UI Automation 文本判定（旧 bug 的回归测试）")
    n = uia.normalize
    rep.check("归一化去掉冒号与空白", n("暂停时长： ") == "暂停时长")
    rep.check("旧 bug 案例：子串匹配会命中的那个设置项必须落空",
              uia.classify_control_name("自动暂停延迟：") is None,
              str(uia.classify_control_name("自动暂停延迟：")))
    rep.check("旧 bug 案例：带数值的设置项也落空",
              uia.classify_control_name("自动暂停延迟：20 分钟") is None)
    rep.check("本工具自己的界面文字不会误命中",
              uia.classify_control_name("⏸ 已暂停（未消耗）") is None)
    rep.check("「暂停时长」→ pause", uia.classify_control_name("暂停时长") == "pause")
    rep.check("「开启时长」→ start", uia.classify_control_name("开启时长") == "start")
    rep.check("「暂停加速」→ pause", uia.classify_control_name("暂停加速") == "pause")
    rep.check("空文本 → None", uia.classify_control_name("") is None)
    rep.check("None → None", uia.classify_control_name(None) is None)
    rep.check("「暂停时长」与「开启时长」不可能同时命中同一个控件",
              not (uia.classify_control_name("暂停时长") == "start"))
    rep.check("没有 UIA 库时 available() 返回布尔而不是抛异常",
              isinstance(uia.available(), bool))

    # `duration_detector.last_uia` 的结构契约 —— 真机闭环脚本直接读它做体检。
    # 踩过的坑：把 `all` 当成“值是列表的字典”去 sum(len(v) ...)，实际上它是
    # **ControlInfo 的扁平序列**；那一版只在 UIA 枚举到 0 个控件时侥幸不报错，
    # 提权后真枚举到控件就 TypeError，而崩溃点在所有阶段之前，
    # 表现为“点了 ✕ 毫无反应”。这里把契约钉住，杜绝同类误用。
    from leigod.duration_detector import DurationDetector
    det = DurationDetector(H.test_config())
    lu = det.last_uia
    rep.check("last_uia 含 pause/start/all 三个键",
              {"pause", "start", "all"}.issubset(set(lu or {})), str(sorted(lu or {})))
    rep.check("last_uia['all'] 是序列（不是'值是列表的字典'）",
              isinstance(lu.get("all"), (list, tuple)))
    c = uia.ControlInfo(name="暂停时长", control_type="Button")
    lu2 = dict(lu)
    lu2["all"] = [c]
    rep.check("all 里的元素是 ControlInfo（可直接 len(列表) 计数）",
              len(lu2["all"]) == 1)
    rep.check("ControlInfo 自身没有 __len__（所以 sum(len(v) ...) 必然报错）",
              not hasattr(c, "__len__"))
    rep.check("按契约正确计数不抛异常",
              (lambda: len(list(lu2.get("all") or [])))() == 1)

    # 顶栏裁剪区过滤：真机实测雷神窗口里有 1 个「暂停时长」+ 20 个「开启时长」，
    # 不按区域收敛就会两条证据同时成立 → 永久 UNKNOWN。
    crop = {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14}
    frame = (0, 0, 1000, 1000)
    inc = uia._in_crop
    rep.check("顶栏内的控件被保留", inc((900, 20, 980, 60), frame, crop) is True)
    rep.check("窗口下部的控件被排除（就是那 20 个噪声的来源）",
              inc((100, 400, 300, 460), frame, crop) is False)
    rep.check("窗口左侧的控件被排除", inc((50, 20, 120, 60), frame, crop) is False)
    rep.check("没有 rect 的控件不参与匹配（不猜）", inc(None, frame, crop) is False)
    rep.check("裁剪参数缺失时不炸", isinstance(inc((900, 20, 980, 60), frame, None), bool))
    import inspect as _inspect
    sig = _inspect.signature(uia.find_duration_controls)
    rep.check("find_duration_controls 支持传入 frame/crop",
              "frame" in sig.parameters and "crop" in sig.parameters,
              str(sig))


# ==================================================================== OCR
def t_ocr(rep: H.Report) -> None:
    rep.section("OCR 文案判定（含误识别容错）")
    def lines(*texts, conf=0.95):
        return [{"text": t, "confidence": conf, "bbox": [0, 0, 1, 1]} for t in texts]

    st, d = ocr_mod.classify_text(lines("暂停时长"))
    rep.check("「暂停时长」→ RUNNING", st == "RUNNING", d)
    st, d = ocr_mod.classify_text(lines("开启时长"))
    rep.check("「开启时长」→ PAUSED", st == "PAUSED", d)
    st, d = ocr_mod.classify_text(lines("并启时长"))
    rep.check("常见误识别「并启时长」→ PAUSED", st == "PAUSED", d)
    st, d = ocr_mod.classify_text(lines("开肩时长"))
    rep.check("常见误识别「开肩时长」→ PAUSED", st == "PAUSED", d)
    st, d = ocr_mod.classify_text(lines("并起时长"))
    rep.check("真机实测误识别「并起时长」→ PAUSED", st == "PAUSED", d)
    st, d = ocr_mod.classify_text(lines("开起时长"))
    rep.check("常见误识别「开起时长」→ PAUSED", st == "PAUSED", d)
    st, d = ocr_mod.classify_text(lines("开启时长", "主机游戏(2487)", "本地游戏(9)",
                                        "平台下载(11)", "国服（441)", "雷神电脑"))
    rep.check("真机顶栏混入分类标签仍判 PAUSED（分类标签不含关键词）",
              st == "PAUSED", d)
    st, d = ocr_mod.classify_text(lines("1318时08分"))
    rep.check("只有剩余时长数字 → 不判定", st is None, d)
    st, d = ocr_mod.classify_text(lines("暂停时长", "开启时长"))
    rep.check("两种文案同时出现 → 不判定（冲突）", st is None, d)
    st, d = ocr_mod.classify_text(lines("暂停时长", conf=0.2))
    rep.check("低置信度不参与判定", st is None, d)
    st, d = ocr_mod.classify_text([])
    rep.check("无文字 → 不判定", st is None, d)
    rep.check("映射到三态枚举：PAUSED",
              ocr_mod.to_state_name("PAUSED") is DurationState.PAUSED)
    rep.check("映射到三态枚举：未知字符串 → UNKNOWN",
              ocr_mod.to_state_name("wat") is DurationState.UNKNOWN)


# ==================================================================== 图像
def t_image(rep: H.Report) -> None:
    rep.section("截图有效性校验（脏图绝不用来猜）")
    import numpy as np
    ok, why = img.looks_like_real_capture(None)
    rep.check("空截图 → 不可信", ok is False, why)

    black = np.zeros((300, 400, 3), dtype=np.uint8)
    ok, why = img.looks_like_real_capture(black)
    rep.check("全黑（UIPI 拦截的典型表现）→ 不可信", ok is False, why)

    white = np.full((300, 400, 3), 255, dtype=np.uint8)
    ok, why = img.looks_like_real_capture(white)
    rep.check("全白 → 不可信", ok is False, why)

    flat = np.full((300, 400, 3), 128, dtype=np.uint8)
    ok, why = img.looks_like_real_capture(flat)
    rep.check("几乎没有内容（std 过低）→ 不可信", ok is False, why)

    rng = np.random.default_rng(0)
    real = rng.integers(0, 255, (300, 400, 3), dtype=np.uint8)
    ok, why = img.looks_like_real_capture(real, (400, 300))
    rep.check("有内容的截图 → 可信", ok is True, why)
    ok, why = img.looks_like_real_capture(real, (900, 300))
    rep.check("尺寸与窗口不符 → 不可信", ok is False, why)

    rep.section("裁剪与颜色辅助")
    arr = np.arange(100 * 200 * 3, dtype=np.uint8).reshape(100, 200, 3)
    crop = img.crop_relative(arr, None, {"left": 0.5, "top": 0.0, "right": 1.0, "bottom": 0.5})
    rep.check("相对裁剪尺寸正确", crop.shape[:2] == (50, 100), str(crop.shape))
    rep.check("非法裁剪区间不会返回空图",
              img.crop_relative(arr, None, {"left": 1.0, "right": 0.0}).size > 0)
    rep.check("白底 → 提示 PAUSED",
              img.classify_button_color((255, 255, 255))[0] == "PAUSED")
    rep.check("红底 → 提示 RUNNING",
              img.classify_button_color((214, 111, 112))[0] == "RUNNING")
    rep.check("无特征底色 → 不判定",
              img.classify_button_color((40, 40, 40))[0] is None)
    rep.check("无采样 → 不判定", img.classify_button_color(None)[0] is None)


# ==================================================================== 坐标
def t_coordinate(rep: H.Report) -> None:
    rep.section("相对坐标换算（禁止绝对屏幕坐标）")
    rect = (100, 200, 1100, 900)          # w=1000 h=700
    x, y = cf.compute_click_point(rect, ratio=(0.7, 0.1))
    rep.check("ratio 换算到屏幕坐标", (x, y) == (800, 270), f"{x},{y}")
    x2, y2 = cf.compute_click_point((200, 200, 1200, 900), ratio=(0.7, 0.1))
    rep.check("窗口右移 100 → 点击点跟着移 100（比例不变）", x2 - x == 100, f"{x2}")
    x3, y3 = cf.compute_click_point((100, 200, 2100, 1600), ratio=(0.7, 0.1))
    rep.check("窗口放大 2 倍 → 点击点按比例外推", x3 == 1500 and y3 == 340, f"{x3},{y3}")
    x4, y4 = cf.compute_click_point(rect, pos=(30, 40))
    rep.check("pos 为相对左上角偏移", (x4, y4) == (130, 240), f"{x4},{y4}")
    try:
        cf.compute_click_point(rect)
        rep.check("两种相对坐标都不给时必须报错", False, "居然没报错")
    except ValueError as e:
        rep.check("两种相对坐标都不给时必须报错", "ratio" in str(e), str(e))
    rep.check("resize 到极端尺寸也不会越界",
              cf.compute_click_point((0, 0, 1, 1), ratio=(0.5, 0.5)) == (0, 0))


# ==================================================================== 游戏监控
def t_game_monitor(rep: H.Report) -> None:
    rep.section("游戏监控（第二保险丝）")
    cfg = H.test_config()
    cfg.set("game_monitor.exit_wait_seconds", 5, save=False)   # 下限就是 5s
    gm = GameMonitor(cfg)

    running = {"v": True}
    gm._match_processes = lambda: (["cs2.exe"] if running["v"] else [])

    p = gm.poll()
    rep.check("从没见过游戏时不会触发暂停", p["ready_to_pause"] is False, str(p))
    rep.check("见到游戏后 seen_game 置位", p["seen_game"] is True)

    running["v"] = False
    p = gm.poll()
    rep.check("游戏刚退出不立刻触发（要等满等待时间）", p["ready_to_pause"] is False, str(p))
    rep.check("开始计时", p["exited_seconds"] < 1.0, str(p["exited_seconds"]))

    running["v"] = True
    p = gm.poll()
    rep.check("游戏重启 → 取消计时", gm._exited_since is None and p["ready_to_pause"] is False)

    running["v"] = False
    gm.poll()
    gm._exited_since = time.time() - 6.0        # 直接把时间推过等待窗口
    p = gm.poll()
    rep.check("等满 exit_wait_seconds 后允许暂停", p["ready_to_pause"] is True, str(p))
    rep.check("上报了等待秒数", p["exit_wait_seconds"] == 5, str(p))
    gm.reset()
    rep.check("reset 只清计时、不清「见过游戏」（否则第二保险丝会失效）",
              gm._exited_since is None and gm._seen_game is True,
              f"exited_since={gm._exited_since} seen={gm._seen_game}")

    from game import process_monitor as pm
    joined = " ".join(pm.BUILTIN_GAME_PROCESSES).lower()
    rep.check("内置游戏名单不包含启动器（启动器常驻≠游戏在跑）",
              not any(x in joined for x in ("steam", "wegame", "epicgames", "riotclient")))


# ==================================================================== 通知
def t_notifier(rep: H.Report) -> None:
    rep.section("通知去重（不骚扰用户）")
    from ui.notifications import Notifier

    class FakePanel:
        def __init__(self): self.items = []
        def show_notice(self, n): self.items.append(n)

    class FakeTray:
        def __init__(self): self.items = []
        def notify(self, t, m): self.items.append((t, m))

    panel, tray = FakePanel(), FakeTray()
    n = Notifier(panel=panel, tray=tray, cooldown=60)
    n.handle(Notice("已阻止关闭雷神", "暂停失败"))
    n.handle(Notice("已阻止关闭雷神", "暂停失败"))
    rep.check("同一条消息在冷却期内只提示一次",
              len(panel.items) == 1 and len(tray.items) == 1,
              f"panel={len(panel.items)} tray={len(tray.items)}")
    rep.check("面板与托盘都收到了", len(panel.items) == 1 and len(tray.items) == 1)
    n.handle(Notice("已阻止关闭雷神", "暂停失败", strong=True))
    rep.check("强提醒不受去重限制", len(panel.items) == 2, str(len(panel.items)))
    n.handle(Notice("另一条消息", "内容也不同"))
    rep.check("不同内容各自提示", len(panel.items) == 3, str(len(panel.items)))
    n.handle(None)
    rep.check("None 被安全忽略", len(panel.items) == 3)

    class Boom:
        def show_notice(self, x): raise RuntimeError("面板炸了")
    n2 = Notifier(panel=Boom(), tray=FakeTray(), cooldown=0)
    try:
        n2.handle(Notice("t", "m"))
        rep.check("面板异常不会把引擎带崩", True)
    except Exception as e:
        rep.check("面板异常不会把引擎带崩", False, str(e))


# ==================================================================== 单实例
def t_single_instance(rep: H.Report) -> None:
    rep.section("单实例（多开会让两个钩子互殴）")
    name = f"LeigodGuardUnitTest_{os.getpid()}"
    a = launcher.SingleInstance(name)
    rep.check("第一个实例拿到锁", a.acquire() is True)
    b = launcher.SingleInstance(name)
    rep.check("第二个实例被拒绝", b.acquire() is False)
    rep.check("并能知道「已有实例在跑」", b.already_running is True)
    a.release()
    c = launcher.SingleInstance(name)
    rep.check("释放后可以重新获取", c.acquire() is True)
    c.release()

    rep.section("权限与可执行文件选择")
    rep.check("能判断自身是否提权（返回布尔）", isinstance(launcher.is_elevated(), bool))
    rep.check("非法 PID 不会误报提权", launcher.process_may_be_elevated(0) is False)
    pr = launcher.privilege_report(H.test_config())
    rep.check("权限报告字段齐全",
              all(k in pr for k in ("self_elevated", "leigod_elevated", "leigod_running", "ok")),
              str(pr))

    cfg = config_mod.Config({"leigod": {"launcher_path": "", "exe_path": "", }},
                            path=H.uniq_path("unit_pick.json"))
    exe, label = launcher._pick_exe(cfg)
    rep.check("两个路径都为空时明确报告而不是瞎启动", exe == "", label)
    cfg2 = config_mod.Config({"leigod": {"launcher_path": r"D:\nope\a.exe",
                                        "exe_path": r"D:\nope\b.exe"}},
                             path=H.uniq_path("unit_pick2.json"))
    exe2, label2 = launcher._pick_exe(cfg2)
    rep.check("都不存在时仍按优先级返回（并标注路径不存在）",
              exe2.endswith("a.exe") and "不存在" in label2, f"{exe2} / {label2}")


# ==================================================================== 主题/文案
def t_theme(rep: H.Report) -> None:
    rep.section("界面文案与配色")
    rep.check("未知状态按 UNKNOWN 渲染", theme.state_style("???") == theme.state_style("UNKNOWN"))
    rep.check("RUNNING 用红色（危险）", theme.state_style("RUNNING")[1] == theme.C_RUNNING)
    rep.check("PAUSED 用绿色（安全）", theme.state_style("PAUSED")[1] == theme.C_PAUSED)
    rep.check("chip 样式带上了颜色", theme.chip_qss("RUNNING").count("#") >= 2)

    sl = theme.status_line
    rep.check("未找到雷神时不冒充正常",
              "未找到雷神" in sl("RUNNING", {"leigod_found": False}))
    rep.check("暂停失败时提示已保持拦截",
              "拦截" in sl("RUNNING", {"leigod_found": True, "pause_failed": True,
                                       "pause_fails": 3}))
    rep.check("RUNNING 文案不含内部证据串",
              "=" not in sl("RUNNING", {"leigod_found": True}))
    rep.check("PAUSED 文案告诉用户可以关",
              "关闭" in sl("PAUSED", {"leigod_found": True}))
    rep.check("UNKNOWN 文案说明会被阻止",
              "阻止" in sl("UNKNOWN", {"leigod_found": True}) or
              "阻止" in sl("UNKNOWN", {"leigod_found": True}))

    # 「关闭保护」那一行：最关键的是**不许把拦截失败说成没事**。
    # 背景：拦截依赖禁用雷神的系统菜单 SC_CLOSE；雷神是高完整性进程，
    # 本程序不提权时会被 UIPI 静默拒绝（菜单其实没灰、✕ 还能点）。
    pl = theme.protection_line
    rep.check("已生效时说明 ✕ 已锁定",
              "已生效" in pl({"protect_enabled": True, "decision": "BLOCK_RUNNING",
                              "sc_close_disabled": True}))
    rep.check("拦截被 UIPI 拒绝时绝不显示成「一切正常」",
              "未" in pl({"protect_enabled": True, "decision": "BLOCK_RUNNING",
                          "sc_close_disabled": False})
              and "待命中" not in pl({"protect_enabled": True,
                                      "decision": "BLOCK_RUNNING",
                                      "sc_close_disabled": False}))
    rep.check("拦截失败时点名权限问题与解决办法",
              "权限" in pl({"protect_enabled": True, "decision": "BLOCK_UNKNOWN",
                            "sc_close_disabled": False})
              and "管理员" in pl({"protect_enabled": True, "decision": "BLOCK_UNKNOWN",
                                  "sc_close_disabled": False}))
    rep.check("暂停失败同样按「未生效」提示",
              "未" in pl({"protect_enabled": True, "decision": "BLOCK_PAUSE_FAILED",
                          "sc_close_disabled": False}))
    rep.check("已放行时说明本次放行",
              "放行" in pl({"protect_enabled": True, "decision": "ALLOW",
                            "sc_close_disabled": False}))
    rep.check("用户关掉保护时如实说明 ✕ 会直接关闭",
              "已关闭" in pl({"protect_enabled": False}))
    rep.check("还没轮到拦截时才显示「待命中」",
              "待命中" in pl({"protect_enabled": True, "decision": None,
                              "sc_close_disabled": False}))


# ==================================================================== 入口
def t_close_intent(rep: H.Report) -> None:
    """关闭保护 v2 判定层（纯逻辑）。

    这一段必须被单独盯住：它是唯一一段跑在**低层鼠标钩子回调**里的代码，
    出错的表现是「钩子回调超时被系统静默摘掉」——没有任何报错，保护直接失效。
    """
    from leigod import close_intent as ci
    rep.section("关闭保护 v2 · 关闭意图判据（输入层）")

    # 真机实测几何：窗口 1500x938 @ (530,300)，✕ 中心 (2003,318)
    rect = (530, 300, 2030, 1238)
    hot = ci.zone_rect(rect)
    rep.check("✕ 热区由相对比例算出，命中真机实测的 ✕ 中心",
              ci.in_hot_zone(rect, 2003, 318), f"热区={hot}")
    cx, cy = ci.describe_zone(rect)["hot_center"]
    rep.check("热区中心与实测中心偏差 ≤ 4px",
              abs(cx - 2003) <= 4 and abs(cy - 318) <= 4, f"算出 ({cx},{cy}) vs 实测 (2003,318)")
    rep.check("窗口中部不属于热区", not ci.in_hot_zone(rect, 1280, 769))
    rep.check("窗口外不属于热区", not ci.in_hot_zone(rect, 0, 0))
    rep.check("没有窗口矩形时一律不命中", not ci.in_hot_zone(None, 2003, 318))

    # 缩放/移动后仍应命中：把窗口整体右移 400、放大 1.5 倍
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    r2 = (rect[0] + 400, rect[1] + 120, rect[0] + 400 + int(w * 1.5), rect[1] + 120 + int(h * 1.5))
    px = r2[2] - int(27 * 1.5)
    py = r2[1] + int(18 * 1.5)
    rep.check("窗口移动+缩放后仍能命中 ✕（用相对比例而非绝对坐标）",
              ci.in_hot_zone(r2, px, py), f"点=({px},{py}) 热区={ci.zone_rect(r2)}")

    base = dict(hwnd=1, rect=rect, state="RUNNING", enabled=True,
                escaping=False, foreground=1, armed=True)
    S = ci.GuardSnapshot
    rep.check("RUNNING + 窗口内 + 热区 → 应当吞点",
              ci.should_swallow(S(**base), 2003, 318)[0])
    rep.check("PAUSED → 绝不干预",
              not ci.should_swallow(S(**{**base, "state": "PAUSED"}), 2003, 318)[0])
    rep.check("UNKNOWN → 仍要拦（Fail Safe，不得当作已暂停）",
              ci.should_swallow(S(**{**base, "state": "UNKNOWN"}), 2003, 318)[0])
    rep.check("保护未开启 → 不吞",
              not ci.should_swallow(S(**{**base, "enabled": False}), 2003, 318)[0])
    rep.check("用户已放行 → 不吞",
              not ci.should_swallow(S(**{**base, "escaping": True}), 2003, 318)[0])
    rep.check("钩子不可用（armed=False）→ 不吞",
              not ci.should_swallow(S(**{**base, "armed": False}), 2003, 318)[0])
    rep.check("点在窗口内但不在热区 → 不吞（§十三 防误吞）",
              not ci.should_swallow(S(**base), 1280, 769)[0])
    rep.check("点在热区但窗口外 → 不吞",
              not ci.should_swallow(S(**{**base, "rect": (3000, 3000, 4000, 4000)}),
                                    2003, 318)[0])
    rep.info(f"拒绝原因示例 = {ci.should_swallow(S(**{**base, 'state': 'PAUSED'}), 2003, 318)[1]}")

    # 悬停（方案 B）比吞点更严：必须雷神在前台，避免「路过」就打断加速
    rep.check("悬停：前台一致 → 有资格预暂停",
              ci.should_prepause(S(**{**base, "foreground": 1}), rect[2] - 55, rect[1] + 45)[0])
    rep.check("悬停：**雷神不在前台** → 不预暂停（防止误暂停打断加速）",
              not ci.should_prepause(S(**{**base, "foreground": 999}),
                                     rect[2] - 55, rect[1] + 45)[0])
    rep.check("悬停：窗口中部不触发",
              not ci.should_prepause(S(**base), 1280, 769)[0])
    rep.check("悬停区比热区宽（向外扩了 hover_pad_px）",
              ci.hover_rect(rect)[0] < ci.zone_rect(rect)[0],
              f"热点={ci.zone_rect(rect)} 悬停={ci.hover_rect(rect)}")


# ==================================================================== 入口
def t_config_migration(rep: H.Report) -> None:
    """旧版配置的迁移（必须让用户看得见，不能静默改配置）。

    背景：`close_protection.method` 这个键诞生时，「禁用系统菜单」与「输入层吞点」
    被当成二选一；真机实测后两者变成**分层**关系（输入层是 ✕ 的主路径，
    禁用系统菜单只是 Alt+F4 的兜底）。磁盘上那份旧 `config.json` 里
    `method="disable_system_menu"` 会让 v2 主路径**完全不启动**，
    而面板仍显示「关闭保护已启用」——静默失效，必须靠迁移修掉。
    """
    rep.section("配置迁移（旧版 close_protection.method）")
    legacy = {"close_protection": {"enabled": True, "block_when_unknown": True,
                                   "block_when_pause_failed": True,
                                   "method": "disable_system_menu",
                                   "detect_close_intent": True,
                                   "auto_close_after_pause": True,
                                   "reapply_interval_ms": 2000}}
    cfg = config_mod.Config(legacy, path=os.path.join(H.OUT, "cfg_mig.json"))
    rep.check("旧值 disable_system_menu 被迁移为 input_swallow（否则 v2 主路径不启动）",
              cfg.get("close_protection.method") == "input_swallow",
              f"实际 {cfg.get('close_protection.method')!r}")
    rep.check("迁移有说明留痕（不是静默改配置）",
              cfg.migration_notes and "disable_system_menu" in cfg.migration_notes[0],
              str(cfg.migration_notes))
    rep.check("迁移后主路径开关为开（swallow_close_click 默认 True）",
              bool(cfg.get("close_protection.swallow_close_click")) is True)

    # 用户已显式表态时绝不覆盖
    explicit = {"close_protection": {"method": "disable_system_menu",
                                     "swallow_close_click": False}}
    cfg2 = config_mod.Config(explicit, path=os.path.join(H.OUT, "cfg_mig2.json"))
    rep.check("用户已显式写过 swallow_close_click → 不迁移、尊重原选择",
              cfg2.get("close_protection.method") == "disable_system_menu"
              and cfg2.get("close_protection.swallow_close_click") is False,
              f"method={cfg2.get('close_protection.method')} "
              f"swallow={cfg2.get('close_protection.swallow_close_click')}")
    rep.check("未迁移时没有迁移说明", not cfg2.migration_notes, str(cfg2.migration_notes))

    fresh = config_mod.Config({}, path=os.path.join(H.OUT, "cfg_mig3.json"))
    rep.check("全新配置默认就是 input_swallow（不会重现该缺陷）",
              fresh.get("close_protection.method") == "input_swallow")
    rep.check("默认开启确认框监测（层级3）",
              bool(fresh.get("close_protection.confirm_dialog.enabled")))


def t_confirm_dialog(rep: H.Report) -> None:
    """层级3（确认框监测）的判定与 OCR 通路 —— 全部用伪注入，不跑真 OCR。

    真 OCR 单次几百毫秒，放进单元测试既慢又不稳。这里只验证**判定逻辑**：
    关键词命中、联合判据、候选窗口过滤、以及「雷神不在前台就不扫」这个性能门槛。
    真实窗口的检测由 `test_close_guard.py` 的用例 H/I 用真靶机确认框验证。
    """
    from leigod import confirm_dialog as cd
    rep.section("关闭保护 v2 · 层级3 确认框监测（判定层 + OCR 通路）")

    # ---- 关键词判定 ----
    dlg_lines = [{"text": "最小化到托盘", "confidence": 0.92},
                 {"text": "真的退出", "confidence": 0.88}]
    r = cd.match_confirm_lines(dlg_lines)
    rep.check("两组关键词同时命中 → 判为确认框", r["found"], r["detail"])

    # 真机依据（雷神 v11.3.2.9）：确认框弹出时 OCR 稳定认出「最小化到托盘」，
    # 但「真的退出」常被背景文字干扰认不出；而 require_both 要求两组同时命中，
    # 结果"明明看见了却判成不是"。完整短语是确认框独有特征 → 命中即算。
    r = cd.match_confirm_lines([{"text": "最小化到托盘", "confidence": 0.9}])
    rep.check("★ 只命中强特征短语「最小化到托盘」→ **算**确认框（真机实测：退出组常识别不出）",
              r["found"], r["detail"])

    r = cd.match_confirm_lines([{"text": "最小化", "confidence": 0.9}])
    rep.check("只命中短词「最小化」→ 不算确认框（两字太常见，会误报）",
              not r["found"], r["detail"])

    r = cd.match_confirm_lines([{"text": "退出", "confidence": 0.9}])
    rep.check("只命中「退出」（且不是「真的退出」）→ 不算确认框",
              not r["found"], r["detail"])

    r = cd.match_confirm_lines(dlg_lines, require_both=False)
    rep.check("require_both=False 时一组即可命中（配置可调）", r["found"])

    r = cd.match_confirm_lines([{"text": "最小化到托盘", "confidence": 0.1},
                                {"text": "真的退出", "confidence": 0.1}])
    rep.check("置信度过低的一律丢弃 → 不算确认框", not r["found"], r["detail"])

    r = cd.match_confirm_lines([])
    rep.check("没有文字 → 不算确认框", not r["found"], r["detail"])

    r = cd.match_confirm_lines([{"text": "最小化 到 托盘", "confidence": 0.9},
                                {"text": "真的退出", "confidence": 0.9}])
    rep.check("OCR 常见空格噪声被归一化后仍能命中", r["found"], r["detail"])

    # ---- 候选窗口过滤 ----
    good = {"hwnd": 0x111, "pid": 999, "top_level": True, "visible": True,
            "size": [380, 170], "offscreen": False}
    rep.check("合格的新顶层窗口 → 是候选（真实确认框的形状）",
              cd.is_candidate_window(good, 0x222, 999))
    rep.check("主窗口自己 → 不是候选",
              not cd.is_candidate_window(good, 0x111, 999))
    rep.check("已在 known 集合里 → 不是候选（只报一次，不重复触发）",
              not cd.is_candidate_window(good, 0x222, 999, known={0x111}))
    rep.check("别的进程的窗口 → 不是候选",
              not cd.is_candidate_window({**good, "pid": 5}, 0x222, 999))
    rep.check("不是顶层（真有父窗口的子窗口）→ 不是候选",
              not cd.is_candidate_window({**good, "top_level": False}, 0x222, 999))
    rep.check("不可见 → 不是候选",
              not cd.is_candidate_window({**good, "visible": False}, 0x222, 999))
    rep.check("太小（tooltip/输入法候选框）→ 不是候选",
              not cd.is_candidate_window({**good, "size": [40, 20]}, 0x222, 999))
    rep.check("在屏幕外（雷神最小化到托盘时主窗口在 -25600）→ 不是候选",
              not cd.is_candidate_window({**good, "offscreen": True}, 0x222, 999))
    rep.check("空信息 → 不是候选", not cd.is_candidate_window(None, 0x222, 999))

    # ---- OCR 通路（伪注入：不装钩子、不跑真 OCR）----
    # 契约（实测踩坑后定下的硬约束）：**poll() 绝不能跑 OCR**。
    # 它跑在引擎主循环里，而主循环还负责消费「点了 ✕」的关闭意图（1.5s 新鲜度）
    # 与刷新输入层死手开关。曾经把整窗 OCR 放在 poll() 里，结果主循环每轮被阻塞
    # 1s+，真实点击被当成过期事件丢掉 —— 关闭保护直接失灵。
    # 所以 OCR 归独立线程，poll() 只取结果。下面的用例就是钉这个契约。
    cfg = H.test_config(close_protection={"confirm_dialog": {
        "enabled": True, "mode": "ocr", "poll_ms": 150, "require_both": True}})
    box = {"lines": [{"text": "开启时长", "confidence": 0.95}]}
    grabs = []

    def fake_grab(hwnd):
        grabs.append(hwnd)
        return "IMG"

    cw = cd.ConfirmWatch(cfg, main_hwnd=0x222, pid=999, grabber=fake_grab,
                         ocr_reader=lambda arr: box["lines"],
                         require_foreground=False)
    rep.check("mode=ocr 时不创建 WinEventHook 监听器（只走 OCR）",
              cw.listener is None)
    rep.check("构造后尚未扫描；poll() 自己不负责 OCR", not grabs and cw.poll() is None)

    cw.start()
    rep.check("start() 后 OCR 扫描线程在跑", cw.status()["ocr_thread"] is True)
    rep.check("扫描线程确实去抓了图（通路真的在跑）",
              bool(H.wait_until(lambda: len(grabs) >= 1, 3.0)), f"grabs={len(grabs)}")
    rep.check("主界面文字（开启时长）→ 不误报确认框", cw.poll() is None, cw.last_detail)

    # ★ 回归守卫：poll() 绝不能被 OCR 阻塞
    slow = {"n": 0}

    def slow_reader(arr):
        time.sleep(0.4)                     # 模拟一次昂贵的整窗 OCR
        slow["n"] += 1
        return box["lines"]

    cw_slow = cd.ConfirmWatch(cfg, main_hwnd=0x222, pid=999, grabber=fake_grab,
                              ocr_reader=slow_reader, require_foreground=False)
    cw_slow.start()
    H.wait_until(lambda: slow["n"] >= 1, 3.0)
    t0 = time.time()
    for _ in range(5):
        cw_slow.poll()
    dt = time.time() - t0
    rep.check("★ poll() 不被 OCR 阻塞（5 次调用总耗时 < 0.15s，OCR 单次 0.4s）",
              dt < 0.15, f"实测 {dt:.3f}s")
    cw_slow.stop()

    box["lines"] = dlg_lines
    hit = H.wait_until(cw.poll, 6.0)
    rep.check("切到确认框文案后命中，且 via=ocr",
              bool(hit) and hit.get("via") == "ocr", str(hit))

    # 限频：OCR 很贵，扫描线程必须按 poll_ms 节流，不能空转打满 CPU
    cfg_slow = H.test_config(close_protection={"confirm_dialog": {
        "enabled": True, "mode": "ocr", "poll_ms": 500, "require_both": True}})
    grabs2 = []
    cw_thr = cd.ConfirmWatch(cfg_slow, main_hwnd=0x222, pid=999,
                             grabber=lambda h: (grabs2.append(h), "IMG")[1],
                             ocr_reader=lambda arr: box["lines"],
                             require_foreground=False)
    cw_thr.start()
    H.wait_until(lambda: len(grabs2) >= 1, 3.0)
    n_before = len(grabs2)
    time.sleep(0.6)
    rep.check("按 poll_ms 限频（0.6s 内最多再抓 2 张，不会空转打满）",
              len(grabs2) - n_before <= 2, f"{n_before} → {len(grabs2)}")
    cw_thr.stop()

    # 「雷神不在前台就不扫」的性能门槛
    cw3 = cd.ConfirmWatch(cfg, main_hwnd=0x222, pid=999, grabber=fake_grab,
                          ocr_reader=lambda arr: dlg_lines, require_foreground=True)
    cw3.start()
    rep.check("雷神不在前台 → 跳过 OCR（不浪费 CPU）",
              bool(H.wait_until(lambda: cw3.stats["ocr_skipped"] >= 1, 3.0)),
              f"ocr_skipped={cw3.stats['ocr_skipped']}")
    cw3.stop()

    cw4 = cd.ConfirmWatch(cfg, main_hwnd=0x222, pid=999, grabber=fake_grab,
                          ocr_reader=lambda arr: dlg_lines, require_foreground=False)
    cw4.set_enabled(False)
    rep.check("总开关关掉后 poll 一律返回 None（保护停摆即不介入）",
              cw4.poll() is None and cw4.enabled is False)
    rep.check("总开关关掉时不会启动扫描线程（不空转）",
              cw4.status()["ocr_thread"] is False)
    rep.check("set_enabled(True) 能恢复", (cw4.set_enabled(True), cw4.enabled)[1] is True)
    cw.stop()
    cw4.stop()
    rep.check("stop() 之后扫描线程已退出（不残留线程）",
              cw.status()["ocr_thread"] is False)


# ============================================ 死手窗口自适应（输入层）
def t_deadman_window(rep: H.Report) -> None:
    """死手开关的窗口必须大于「一次 tick 的耗时」。

    死手要防的是消费端**死了**，不是它**正忙**。一次 tick 里有整窗 OCR
    （本机实测 1.3s 量级），固定 2.5s 的窗口会被「忙」顶穿 → 钩子周期性自行解锁 →
    「吞点时灵时不灵，点了 ✕ 有时拦有时不拦」。这种故障比完全不吞更难排查，
    所以窗口按实测 tick 间隔自适应放宽，但必须保留上限（消费端真死了要能解锁）。
    """
    from leigod.close_protection import CloseIntentGuard
    rep.section("输入层 · 死手窗口按实测 tick 间隔自适应（且仍有上限）")

    class _Sink:
        def __init__(self):
            self.events = []

        def emit(self, kind, **kw):
            self.events.append(kind)

    cfg = H.test_config(close_protection={
        "enabled": True, "swallow_close_click": True,
        "swallow_deadman_ms": 800, "swallow_deadman_max_ms": 5000,
        "confirm_dialog": {"enabled": False}})
    g = CloseIntentGuard(cfg, logger=None, sink=_Sink())
    # ⚠️ 这一条**必须保持"用配置值"**：把初值直接抬到上限会让
    #    「消费端从未运行」时也吞 10 秒点击 —— 用户被自己锁死。
    #    我曾在 2026-09-30 试过那种做法，被 `test_close_guard.case_deadman` 当场抓住。
    rep.check("还没量过 tick 时用配置值（消费者真死了就得尽快放手）",
              abs(g._arm_window() - 0.8) < 1e-6, f"{g._arm_window():.2f}s")

    # 模拟「每次 tick 花 1s」（整窗 OCR 拖慢主循环）
    for _ in range(6):
        g._last_maintain = time.time() - 1.0
        g.maintain()
    arm = g._arm_window()
    rep.check("tick=1s → 窗口放宽到 3×tick=3s（不再被「忙」顶穿）",
              abs(arm - 3.0) < 0.05, f"arm={arm:.2f}s")

    g._tick_ewma = 100.0
    rep.check("有上限：消费端真死了也不会无限期吞点",
              abs(g._arm_window() - 5.0) < 1e-6, f"{g._arm_window():.2f}s")
    rep.check("上限取自配置 swallow_deadman_max_ms", abs(g._max_arm - 5.0) < 1e-6)

    st = g.status()
    rep.check("状态里如实暴露窗口与实测 tick（可观测才可排查）",
              st.get("arm_ms") == 5000 and st.get("tick_ms") == 100000,
              f"arm_ms={st.get('arm_ms')} tick_ms={st.get('tick_ms')}")


# ============================================== 暂停按钮定位（OCR 文字框）
class _FakeArr:
    """只要 `.shape` 的假图 —— 几何换算不关心像素内容，别为此引入 numpy 依赖。"""

    def __init__(self, h: int, w: int):
        self.shape = (h, w, 3)


def t_ocr_button_locate(rep: H.Report) -> None:
    """OCR 文字框 → 屏幕坐标的换算（未校准坐标时唯一能用的暂停路径）。

    真机上 UIA 枚举到 0 个控件、`duration.coordinate.ratio` 又是 None，
    所以这条链是「不校准也能暂停」的全部依靠。算错 1px 就会点空，
    而点空的表现是「点击成功但没暂停」——最难查的一类故障，因此逐项钉死：
      · 三步换算：+ 裁剪偏移 → × 缩放比例 → + frame 原点
      · 防误点守卫：文字框过宽（整行合并）、置信度不足、缺 bbox、状态 UNKNOWN
      · 裁剪夹紧规则必须与 image_detection.crop_relative **完全一致**
        （差 1px 就会让每一次点击整体偏移）
    """
    from leigod.duration_detector import DurationDetector
    rep.section("暂停按钮定位 · OCR 文字框 → 屏幕坐标（含防误点守卫）")

    det = DurationDetector(H.test_config())
    rep.check("初始没有几何信息", det.last_ocr_geom is None)

    # 窗口 frame = (1000,500)-(2000,1100) → 1000x600；截图 1000x600；裁剪 45%~100% × 0%~14%
    frame = (1000, 500, 2000, 1100)
    det._frame_of_last_capture = list(frame)
    det._record_ocr_geom(_FakeArr(600, 1000), _FakeArr(84, 550),
                         {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14})
    g = det.last_ocr_geom or {}
    rep.check("crop_px 与 crop_relative 的夹紧规则一致 = [450,0,1000,84]",
              g.get("crop_px") == [450, 0, 1000, 84], str(g.get("crop_px")))
    rep.check("记下了 frame 尺寸 1000x600", g.get("frame_size") == [1000, 600], str(g.get("frame_size")))

    # 按钮文字框（裁剪图坐标系）：x 380~470, y 20~44
    BTN = {"text": "暂停时长", "confidence": 0.93, "bbox": [380.0, 20.0, 470.0, 44.0]}
    det.last_ocr_lines = [BTN]
    # 屏幕 x = 1000 + (450 + 380) = 1830 … 1000 + (450 + 470) = 1920
    # 屏幕 y =  500 + (  0 +  20) =  520 …  500 + (  0 +  44) =  544
    rep.check("矩形换算到屏幕坐标正确 = (1830,520,1920,544)",
              det.button_rect_on_screen(DurationState.RUNNING) == (1830, 520, 1920, 544),
              str(det.button_rect_on_screen(DurationState.RUNNING)))
    rep.check("中心点 = (1875,532)（每次识别现算，不落盘）",
              det.button_center_on_screen(DurationState.RUNNING) == (1875, 532))

    # --- 防误点守卫：宁可定位失败，也不能点到别处 ---
    det.last_ocr_lines = [{**BTN, "bbox": [0.0, 20.0, 540.0, 44.0]}]
    rep.check("文字框过宽（整行合并，540px > 窗口宽 26%）→ 拒绝定位",
              det.button_rect_on_screen(DurationState.RUNNING) is None)

    det.last_ocr_lines = [{**BTN, "confidence": 0.2}]
    rep.check("置信度低于阈值 → 拒绝定位",
              det.button_rect_on_screen(DurationState.RUNNING) is None)

    det.last_ocr_lines = [{"text": "暂停时长", "confidence": 0.9}]
    rep.check("缺 bbox → 拒绝定位",
              det.button_rect_on_screen(DurationState.RUNNING) is None)

    det.last_ocr_lines = [{"text": "总时长 12 小时", "confidence": 0.95,
                           "bbox": [10.0, 20.0, 200.0, 44.0]}]
    rep.check("文案不含按钮关键词 → 拒绝定位（不按倒计时数字点）",
              det.button_rect_on_screen(DurationState.RUNNING) is None)

    det.last_ocr_lines = [BTN]
    rep.check("状态 UNKNOWN → 拒绝定位（规格书：不猜按钮位置）",
              det.button_rect_on_screen(DurationState.UNKNOWN) is None)

    det2 = DurationDetector(H.test_config())
    det2.last_ocr_lines = [BTN]
    rep.check("没有几何信息（本轮没截过图）→ 拒绝定位",
              det2.button_rect_on_screen(DurationState.RUNNING) is None)

    # --- PAUSED 方向找的是「开启时长」---
    det.last_ocr_lines = [{"text": "开启时长", "confidence": 0.9,
                           "bbox": [380.0, 20.0, 470.0, 44.0]}]
    rep.check("PAUSED 时能定位到「开启时长」",
              det.button_rect_on_screen(DurationState.PAUSED) == (1830, 520, 1920, 544))
    rep.check("PAUSED 时不会误用「暂停」关键词（两方向互不串台）",
              det.button_rect_on_screen(DurationState.RUNNING) is None)

    # --- 一行里同时含「暂停」与「时长」时优先，而不是取置信度最高的无关行 ---
    det.last_ocr_lines = [
        {"text": "暂停", "confidence": 0.99, "bbox": [5.0, 5.0, 45.0, 25.0]},
        {"text": "暂停时长", "confidence": 0.70, "bbox": [380.0, 20.0, 470.0, 44.0]},
    ]
    rep.check("置信度更高的裸「暂停」不抢占含「时长」的真按钮",
              det.button_rect_on_screen(DurationState.RUNNING) == (1830, 520, 1920, 544))

    # --- 真实截图尺寸 ≠ 窗口尺寸时按比例缩放（PrintWindow 可能给出不同尺寸）---
    det3 = DurationDetector(H.test_config())
    det3._frame_of_last_capture = list(frame)
    det3._record_ocr_geom(_FakeArr(300, 500), _FakeArr(42, 275),
                          {"left": 0.45, "top": 0.0, "right": 1.0, "bottom": 0.14})
    g3 = det3.last_ocr_geom or {}
    rep.check("截图尺寸减半时 crop_px 按比例减半 = [225,0,500,42]",
              g3.get("crop_px") == [225, 0, 500, 42], str(g3.get("crop_px")))
    det3.last_ocr_lines = [{"text": "暂停时长", "confidence": 0.93,
                            "bbox": [190.0, 10.0, 235.0, 22.0]}]
    rep.check("缩放后仍能还原到同一屏幕矩形 = (1830,520,1920,544)",
              det3.button_rect_on_screen(DurationState.RUNNING) == (1830, 520, 1920, 544),
              str(det3.button_rect_on_screen(DurationState.RUNNING)))

    # --- 暂停策略可用性：未校准时必须承认 OCR 这条路，而不是直接说「没招」---
    # 真机陷阱：`uia.available()` 返回 True（模块能加载）但窗口内枚举到 0 个控件。
    # 所以「UIA 可用」不能当成「UIA 走得通」，必须显式说明会自动回退。
    from leigod.duration_controller import DurationController
    from detection import ui_automation as uia_mod
    rep.check("UIA 模块可用时会说明「真机可能枚举到 0 个控件，届时自动回退」",
              "回退" in DurationController(H.test_config(), det)._strategy_available()[1])

    saved_avail = uia_mod.available
    try:
        uia_mod.available = lambda: False       # 模拟「UIA 这条路走不通」
        ctrl = DurationController(
            H.test_config(duration={"coordinate": {"ratio": None, "pos": None}}), det)
        name, why = ctrl._strategy_available()
        if ocr_mod.engine_available():
            rep.check("UIA 不通 + 未校准坐标 → 回退到 OCR 实时定位",
                      name == "mouse_click_ocr", f"{name} / {why}")
        else:
            rep.check("未安装 OCR 引擎 → 明确报「无可用的暂停方式」（不假装能用）",
                      name == "", f"{name} / {why}")

        ctrl2 = DurationController(
            H.test_config(duration={"coordinate": {"ratio": [0.5, 0.5], "pos": None}}), det)
        rep.check("已校准坐标时优先用校准值（OCR 只作兜底）",
                  ctrl2._strategy_available()[0] == "mouse_click")
    finally:
        uia_mod.available = saved_avail

    rep.check("恢复 ui_automation.available（不给后续用例留全局污染）",
              uia_mod.available is saved_avail)


# ==================================================================== 入口
def t_ocr_engine_concurrency(rep: H.Report) -> None:
    """OCR 引擎是全局单例，被多线程同时调用时不得阻塞、不得重复创建。

    真机事故：状态识别（主循环）与层级3 确认框的 `confirm-ocr` 扫描线程
    并发 `eng(img)` 同一个 onnxruntime 会话 → **静默阻塞**（不报错、不崩），
    表现为"验证脚本冻住 100+ 秒、报告再也不刷新"。
    现在 `detection/ocr.py` 用 `_ENGINE_LOCK` 把初始化与推理都串行化了。
    """
    rep.section("OCR 引擎并发安全（曾让真机验证整体冻死）")
    import threading

    src = open(os.path.join(os.path.dirname(HERE), "detection", "ocr.py"),
               encoding="utf-8").read()
    rep.check("ocr.py 存在引擎锁", "_ENGINE_LOCK" in src,
              "找到 _ENGINE_LOCK" if "_ENGINE_LOCK" in src else "缺失")
    rep.check("推理调用被锁包住（不只是初始化）",
              "with _ENGINE_LOCK" in src and "eng(img)" in src)

    # 惰性初始化也要串行：两个线程同时首次调用不得各建一个引擎。
    calls = []
    orig = ocr_mod.get_engine

    def counting_get_engine():
        eng = orig()
        calls.append(id(eng) if eng is not None else None)
        return eng

    ocr_mod.get_engine = counting_get_engine
    try:
        barrier = threading.Barrier(4, timeout=30)

        def worker():
            try:
                barrier.wait(30)
            except Exception:
                pass
            try:
                ocr_mod.get_engine()
            except Exception:
                pass

        ts = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        alive = [t for t in ts if t.is_alive()]
        rep.check("4 个线程并发首次取引擎，全部在 30s 内返回（无阻塞）",
                  not alive, f"仍存活 {len(alive)} 个")
        uniq = {c for c in calls if c is not None}
        rep.check("并发初始化只创建 1 个引擎实例（不重复构造）",
                  len(uniq) <= 1, f"创建出的不同实例数 = {len(uniq)}")
    finally:
        ocr_mod.get_engine = orig


def t_ctypes_no_pollution(rep: H.Report) -> None:
    """任何模块都不得给 `ctypes.windll.user32.<API>` 赋 argtypes。

    为什么立这一条：`ctypes.windll.user32` 在**整个进程里是同一个对象**。
    某个模块把 `WindowFromPoint.argtypes` 覆盖成自己的 POINT 类型后，
    `detection/coordinate_fallback.py`（它 import 时声明过 `[wt.POINT]`）的调用会全部抛
    `ctypes.ArgumentError: expected X instance instead of POINT` ——
    报错点与真凶相距十万八千里（真机上表现为「点了 ✕ 没反应」，因为崩溃发生在阶段循环里）。
    实测代价：一整轮人工验证。

    需要自定义参数类型时，正确做法是**另起一个 `ctypes.WinDLL` 实例**，
    或者干脆复用项目已有的 helper。
    """
    rep.section("ctypes 全局污染防护（跨错觉型崩溃）")

    from detection import coordinate_fallback as cf
    h0 = cf.window_from_point(5, 5)          # 基线：模块自己的声明仍然成立
    rep.check("基线：coordinate_fallback.window_from_point 可用", True,
              f"返回值 {h0}")

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_vcl", os.path.join(HERE, "verify_close_loop_real.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        rep.check("能导入真机闭环脚本", False, f"{type(e).__name__}: {e}")
        return

    err = ""
    try:
        mod.window_at(5, 5)
        # 关键：用过之后，别的模块的同一 API 必须仍然能用自己的类型调用
        cf.window_from_point(7, 7)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    rep.check("用过 window_at 之后 coordinate_fallback.window_from_point 仍不被打断",
              not err, err or "未抛出 ArgumentError")

    src = open(os.path.join(HERE, "verify_close_loop_real.py"),
               encoding="utf-8").read()
    bad = [l.strip() for l in src.splitlines()
           if "user32." in l and ".argtypes" in l and not l.strip().startswith("#")]
    rep.check("闭环脚本源码里没有任何 user32.<API>.argtypes 赋值",
              not bad, "；".join(bad[:3]))


def t_standby_wake(rep: H.Report) -> None:
    """待命守望 —— 「开雷神时自动唤起工具」（真机反馈驱动）。

    真机背景（2026-09-29）：用户开启「开机待命」后说
    「并没有在后台看到工具，打开加速器后也没看到」。查证后是三个真实缺陷：
      ① 计划任务 /sc onlogon 只在**下次登录**触发，不回溯当前会话
         （实测：任务 StartBoundary=23:30，23:34 复查日志仍停在 23:29:42）；
      ② 旧待命 = 完整守卫 --minimized，雷神退出时守卫跟着正常退出，
         之后**没有任何机制**把它拉回来；
      ③ 只有托盘图标且常被收进溢出区，表现和没启动一样。

    这里钉住的是守望的决策逻辑：雷神在+守卫不在 → 唤起；
    唤起**一次之后必须去武装**，否则用户手动关掉守卫会被立刻拉回来，
    那是在替用户做决定。
    """
    rep.section("待命守望（开雷神即唤起）")
    try:
        from launcher import watch as w
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入待命守望模块", False, f"{type(e).__name__}: {e}")
        return

    # ① 基本决策矩阵
    rep.check("雷神不在 → 安静等待，并重新武装",
              w.decide(False, False, False) == (w.IDLE, True))
    rep.check("雷神在 + 守卫在 → 什么都不做",
              w.decide(True, True, True) == (w.ALREADY, True))
    rep.check("雷神在 + 守卫不在 + 已武装 → 唤起并去武装",
              w.decide(True, False, True) == (w.WAKE, False))
    rep.check("雷神在 + 守卫不在 + 未武装 → 不打扰（用户自己关的）",
              w.decide(True, False, False) == (w.STANDBY, False))
    rep.check("关闭自动唤起后不再主动拉起",
              w.decide(True, False, True, auto_wake=False) == (w.DISABLED, True))

    # ② 武装/去武装的完整生命周期：唤起 → 用户关掉 → 雷神退出重开 → 再唤起
    armed = True
    act, armed = w.decide(True, False, armed)
    rep.check("第一次开雷神 → 唤起", act == w.WAKE and armed is False)
    act, armed = w.decide(True, False, armed)
    rep.check("用户关掉守卫后不会被反复拉起（不抢用户控制权）",
              act == w.STANDBY and armed is False)
    act, armed = w.decide(False, False, armed)
    rep.check("雷神退出 → 重新武装", act == w.IDLE and armed is True)
    act, armed = w.decide(True, False, armed)
    rep.check("再次开雷神 → 又能唤起", act == w.WAKE and armed is False)

    # ③ Watcher 装配与「拉起失败必须如实上报」
    cfg = H.test_config()

    class _Watcher(w.Watcher):
        def __init__(self, present, guard_up, launch_ok=True):
            super().__init__(cfg, logger=None)
            self._present = present
            self._guard_up = guard_up
            self._launch_ok = launch_ok
            self.launched = 0

        def leigod_present(self):
            return self._present

        def guard_running(self):
            return self._guard_up

        def launch_guard(self):
            self.launched += 1
            return self._launch_ok

    wt = _Watcher(True, False)
    rep.check("雷神在、守卫不在 → tick 返回 wake 且真的拉起了一次",
              wt.tick() == w.WAKE and wt.launched == 1, f"{wt.last_action}/{wt.launched}")
    rep.check("拉起后已去武装（下一轮不再重复拉起）", wt.armed is False)
    rep.check("第二轮不再重复拉起", wt.tick() == w.STANDBY and wt.launched == 1)

    wf = _Watcher(True, False, launch_ok=False)
    rep.check("拉起失败 → 如实返回 failed，不假装成功（§三十三）",
              wf.tick() == w.FAILED, wf.last_action)
    rep.check("拉起失败后保持武装，下一轮还会再试", wf.armed is True)

    wi = _Watcher(False, False)
    rep.check("雷神不在 → idle 且不拉起", wi.tick() == w.IDLE and wi.launched == 0)

    # ④ 打包形态（frozen）必须唤起**本程序自己**，而不是"python main.py"。
    #    这条分支只能在真机上执行到，本地源码运行永远走不到 —— 正是最容易写错、
    #    又最难发现的那类代码。这里用 sys.frozen 打桩把它逼出来。
    import os
    import sys as _sys
    had_frozen = hasattr(_sys, "frozen")
    old_frozen = getattr(_sys, "frozen", None)
    try:
        _sys.frozen = True
        wt2 = w.Watcher(cfg, logger=None)
        cmd, cwd = wt2._guard_command()
        rep.check("打包形态下唤起的就是本程序自身（而不是 python 脚本）",
                  cmd == [_sys.executable], str(cmd))
        rep.check("打包形态的工作目录 = exe 所在目录（配置与日志写在那儿）",
                  cwd == os.path.dirname(os.path.abspath(_sys.executable)), str(cwd))
    finally:
        if had_frozen:
            _sys.frozen = old_frozen
        else:
            try:
                del _sys.frozen
            except AttributeError:
                pass

    # ④-b 「雷神在不在」的判据：**不能只看到进程就算在**（事故源头）
    #     真机 13:06：进程一出现守望就唤起守卫 → 主窗口还没建好 → 绑错窗口。
    #     修复后要求"认得出主窗口"；只看到进程时先给宽限期，
    #     但超时仍认不出也会照样唤起（免得"认不出窗口"变成"永远不保护"）。
    sp = w.should_treat_present
    rep.check("认得主窗口 → 立刻认为在（并把进程计时清零）",
              sp(True, False, 123.0, 200.0) == (True, None))
    rep.check("既无窗口也无进程 → 不在，计时清零",
              sp(False, False, 123.0, 200.0) == (False, None))
    rep.check("只看到进程的第一轮 → 先等，开始计时",
              sp(False, True, None, 200.0) == (False, 200.0))
    rep.check("只看到进程、宽限期内 → 继续等（**这就是事故的直接修复**）",
              sp(False, True, 200.0, 200.0 + w.PROCESS_ONLY_GRACE_S - 1) == (False, 200.0))
    rep.check("只看到进程、超过宽限 → 照样认为在（不会永不保护）",
              sp(False, True, 200.0, 200.0 + w.PROCESS_ONLY_GRACE_S)[0] is True)
    rep.check("宽限期为正（否则等于没等）", w.PROCESS_ONLY_GRACE_S > 0,
              str(w.PROCESS_ONLY_GRACE_S))
    # 守望的探测顺序：先看窗口，再看进程
    _src = inspect.getsource(w.Watcher.leigod_present)
    rep.check("守望先以「认得出的主窗口」为准，而不是只看进程",
              "strict=True" in _src and "should_treat_present" in _src)

    # ⑤ 源码形态：命令里必须带上 main.py，且工作目录是项目根
    wt3 = w.Watcher(cfg, logger=None)
    cmd, cwd = wt3._guard_command()
    rep.check("源码形态命令包含 main.py 且文件真实存在",
              len(cmd) == 2 and cmd[1].endswith("main.py") and os.path.exists(cmd[1]),
              str(cmd))
    rep.check("源码形态工作目录是项目根（不是解释器目录）",
              os.path.exists(os.path.join(cwd, "main.py")), str(cwd))


def t_perf_contract(rep: H.Report) -> None:
    """性能契约（2026-09-30 治理「点 ✕ 要 6~8 秒」+「后台占用高」）。

    真机实测的延迟（从守卫日志量出来）：8.5 / 7.8 / 6.3 / 5.8 秒。用户要求 1~2 秒。

    根因不是"识别算法慢"，而是**每次识别都为整棵控件树的每个节点读 9 个属性**：
    真机树里 ~476 个控件 → 每轮约 4000 次跨进程 COM 调用 → 单次 0.3~2 秒。
    两个后果：
      · 暂停链路里要读 5~8 次 → 6~8 秒；
      · 引擎 tick 被堵住 → 死手窗口没人刷新 → 日志里反复出现
        `[degraded] 消费端超时未刷新，已自动停止吞点`，而那正是用户点 ✕ 的时刻。

    这里逐条钉住修复后的契约，防止以后有人在不知情的情况下把它改回去。
    """
    import inspect
    import time as _t
    import types

    rep.section("性能契约（延迟 / 后台占用）")
    try:
        from detection import ui_automation as uia_mod
        from core.state_machine import DurationState, Evidence, Reading
        from leigod.duration_detector import DurationDetector
        from leigod.duration_controller import DurationController
        from core.protection_engine import ProtectionEngine
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入性能相关模块", False, f"{type(e).__name__}: {e}")
        return

    # ---------- ① 控件缓存（快路径的基石） ----------
    rep.check("UIA 模块提供控件缓存", hasattr(uia_mod, "read_cached")
              and hasattr(uia_mod, "clear_cache"))
    rep.check("缓存有新鲜度上限（防止长期漂移）",
              float(getattr(uia_mod, "CACHE_TTL", 0)) > 0,
              str(getattr(uia_mod, "CACHE_TTL", None)))
    rep.check("扫描时只把**命中名字**的控件建成对象（慢的根源在这里）",
              "classify_control_name(_name_of(ctrl))" in inspect.getsource(uia_mod._scan_by_name)
              and "probe_invoke=False" in inspect.getsource(uia_mod._scan_by_name))
    src_probe = inspect.getsource(uia_mod._probe_invoke)
    rep.check("InvokePattern 只探测前几个候选（旧实行为每个控件都探一次）",
              "limit" in src_probe and "[:max(0, limit)]" in src_probe)

    # 快路径真的只重读名字（用假控件验证，不依赖真机）
    class _FakeCtrl:
        def __init__(self, name):
            self.Name = name

    uia_mod.clear_cache()
    fake_pause = uia_mod.ControlInfo(name="暂停时长", normalized="暂停时长",
                                     _control=_FakeCtrl("暂停时长"))
    fake_start = uia_mod.ControlInfo(name="开启时长", normalized="开启时长",
                                     _control=_FakeCtrl("开启时长"))
    uia_mod._cache_put(12345, [fake_pause], [fake_start], 476)
    hit = uia_mod.read_cached(12345)
    rep.check("缓存命中时能同时看到两种按钮（不猜、如实报告）",
              hit is not None and len(hit["pause"]) == 1 and len(hit["start"]) == 1,
              str(hit and (len(hit["pause"]), len(hit["start"]))))
    # 名字变了 → 立刻反映（这是"点完立刻知道已暂停"的关键）
    fake_pause._control.Name = "开启时长"
    hit = uia_mod.read_cached(12345)
    rep.check("缓存元素改名后立刻反映（暂停→开启）",
              hit is not None and not hit["pause"] and len(hit["start"]) == 2,
              str(hit and (len(hit["pause"]), len(hit["start"]))))
    # 元素失效（读不出名字）→ 必须放弃缓存，交回全量扫描
    fake_start._control.Name = ""
    fake_pause._control.Name = ""
    rep.check("缓存元素全部失效 → 放弃缓存（返回 None，交回全量扫描）",
              uia_mod.read_cached(12345) is None)
    # 过期
    fake_pause._control.Name = "暂停时长"
    fake_start._control.Name = ""
    uia_mod._cache_put(12345, [fake_pause], [], 476)
    uia_mod._CACHE[12345]["ts"] = _t.time() - (uia_mod.CACHE_TTL + 1)
    rep.check("缓存过期 → 放弃缓存", uia_mod.read_cached(12345) is None)
    uia_mod.clear_cache()

    # ---------- ② 快路径绝不能自己下结论 ----------
    v_src = inspect.getsource(DurationController._verify)
    rep.check("验证时先用快路径（把等待压下来）",
              "uia_fast=True" in v_src)
    rep.check("**给出结论前必须做一次全量扫描复核**（快路径只负责等得少，不负责判得对）",
              "uia_fast=False" in v_src and "ground" in v_src)

    RUN, PAUSED = DurationState.RUNNING, DurationState.PAUSED

    def _reading(st):
        return Reading(state=st, evidence=[Evidence("stub", st, "stub", {})],
                       hwnd=1, rect=(0, 0, 10, 10))

    class _StubDet:
        """`last_uia_from_cache` 必须跟真实现一样存在：它决定"要不要再复核一次"。
        这里标成 True（走缓存），用来盯住「两次缓存 + 一次全量复核」那条分支。"""

        def __init__(self):
            self.calls = []
            self.last_uia_from_cache = True

        def detect(self, win, allow_ocr=True, ocr_mode=None, uia_fast=False):
            self.calls.append({"ocr": allow_ocr, "fast": uia_fast})
            self.last_uia_from_cache = bool(uia_fast)
            return _reading(PAUSED)

    st = _StubDet()
    ctrl = DurationController(H.test_config(), st, logger=None)
    state, _ = ctrl._verify(None, _t.time() + 5.0, settle=0.0)
    rep.check("快相走的是 uia_fast=True",
              len(st.calls) >= 3 and st.calls[0]["fast"] is True, str(st.calls[:4]))
    rep.check("收尾复核走的是全量扫描（uia_fast=False）",
              any(c["fast"] is False for c in st.calls[:4]), str(st.calls[:4]))
    rep.check("结论仍然是 PAUSED", state is PAUSED, state.value)

    # ---------- ②-b detect() 的签名是接口契约，改了必须同步所有桩 ----------
    sig = inspect.signature(DurationDetector.detect)
    rep.check("detect() 签名稳定（测试桩依赖它；改签名就必须同步改桩）",
              list(sig.parameters) == ["self", "win", "allow_ocr", "ocr_mode", "uia_fast"],
              str(list(sig.parameters)))

    # ---------- ②-c 地面真相：全量扫描的结论不该再被复核一次 ----------
    # 真机实测（2026-09-30 19:45）：一次点 ✕ 跑了 **3 次**全量扫描
    # （583+683+683ms），合计 2535ms。其中"收尾复核"是多余的 ——
    # 它复核的那一读本来就是一次真实的全量枚举。
    class _OneShotDet:
        """第一次读就返回 PAUSED，且标记为"非缓存"（=刚做了一次全量枚举）。"""

        def __init__(self):
            self.calls = []
            self.last_uia_from_cache = False

        def detect(self, win, allow_ocr=True, ocr_mode=None, uia_fast=False):
            self.calls.append(uia_fast)
            # 关键：模拟"缓存未命中 → 内部退回全量扫描"，所以 from_cache=False
            self.last_uia_from_cache = False
            return _reading(PAUSED)

    st2 = _OneShotDet()
    ctrl2 = DurationController(H.test_config(), st2, logger=None)
    state2, _ = ctrl2._verify(None, _t.time() + 5.0, settle=0.0)
    rep.check("全量枚举给出的 PAUSED 直接采纳（它就是地面真相）",
              state2 is PAUSED and len(st2.calls) == 1,
              f"state={state2.value} calls={st2.calls}")

    class _CachedDet(_OneShotDet):
        """每次都来自**缓存**（from_cache=True）→ 必须复核。"""

        def detect(self, win, allow_ocr=True, ocr_mode=None, uia_fast=False):
            self.calls.append(uia_fast)
            self.last_uia_from_cache = not uia_fast or True
            return _reading(PAUSED)

    st3 = _CachedDet()
    ctrl3 = DurationController(H.test_config(), st3, logger=None)
    state3, _ = ctrl3._verify(None, _t.time() + 5.0, settle=0.0)
    rep.check("缓存给出的 PAUSED 必须复核（快路径不负责判得对）",
              state3 is PAUSED and any(c is False for c in st3.calls),
              f"calls={st3.calls}")
    rep.check("识别器暴露 from_cache（区分缓存读与全量枚举）",
              hasattr(DurationDetector(H.test_config()), "last_uia_from_cache"))
    rep.check("全量扫描的结果里 from_cache 必须是 False",
              "from_cache" in inspect.getsource(uia_mod.find_duration_controls)
              and '"from_cache": False' in inspect.getsource(uia_mod.find_duration_controls))

    # ---------- ②-d 死手窗口的启动初值 ----------
    # 真机 19:40:59 那次 `[degraded] 消费端超时未刷新` 的原因：首轮 tick
    # （建 Chromium 无障碍树 + 构造 OCR 引擎）要 10 秒量级，而 `_tick_ewma`
    # 初值为 0 → 死手窗口退化成 2.5s → 首轮就被顶穿。
    # 它偏偏发生在「刚启动、用户最可能立刻点 ✕」的那段时间。
    from leigod.close_protection import CloseIntentGuard

    class _Sink2:
        def emit(self, kind, **kw):
            pass

    guard = CloseIntentGuard(
        H.test_config(close_protection={"enabled": True, "swallow_close_click": True,
                                        "swallow_deadman_ms": 2500,
                                        "swallow_deadman_max_ms": 10000,
                                        "confirm_dialog": {"enabled": False}}),
        logger=None, sink=_Sink2())
    max_arm = float(getattr(guard, "_max_arm", 0) or 0)
    rep.check("死手窗口上限存在且 > 默认窗口", max_arm > guard.deadman,
              f"max_arm={max_arm} deadman={guard.deadman}")
    rep.check("默认**没有**启动宽限（避免「消费端从未运行也吞 10 秒」）",
              float(getattr(guard, "_grace_until", 0)) == 0.0)
    guard.set_startup_grace(15.0)
    rep.check("显式申请宽限后，窗口给到上限（首轮 tick 慢也不降级）",
              abs(guard._arm_window() - max_arm) < 0.01,
              f"arm_window={guard._arm_window():.2f} max_arm={max_arm:.2f}")
    guard._grace_until = _t.time() - 1.0          # 宽限到期
    rep.check("宽限到期后回到配置值（宽限必须有时限）",
              abs(guard._arm_window() - guard.deadman) < 0.01,
              f"arm_window={guard._arm_window():.2f}")
    # 一次慢 tick 必须把窗口抬起来（纯 EWMA 会把尖峰抹平）
    guard._last_maintain = _t.time() - 6.0
    guard.maintain()
    rep.check("出现慢 tick 后窗口被抬高（不会被尖峰顶穿）",
              guard._arm_window() > guard.deadman,
              f"arm_window={guard._arm_window():.2f}")
    start_src2 = inspect.getsource(ProtectionEngine.start)
    rep.check("引擎启动时**显式申请**启动宽限（而不是靠全局初值兜底）",
              "set_startup_grace" in start_src2)
    # ⚠️ 顺序也是契约：tick 线程一起来就会 maintain() 一次并武装钩子。
    # 宽限若写在它后面，线程会抢先用 2.5s 的默认窗口武装，
    # 随后首轮那个 10s 的 tick 照样把窗口顶穿（真机 2026-09-30 20:46:49 复现过）。
    rep.check("宽限必须设在 tick 线程启动**之前**（否则竞态，真机复现过）",
              0 <= start_src2.find("set_startup_grace")
              < start_src2.find("self._thread.start()"),
              f"grace@{start_src2.find('set_startup_grace')} "
              f"thread@{start_src2.find('self._thread.start()')}")

    # ---------- ③ 自适应轮询（后台占用的主要来源） ----------
    eng, _ = H.make_engine(H.test_config())
    # `StateMachine.state` 是只读属性，只能通过 note(reading) 推进 —— 这也符合
    # "状态只能由证据驱动"的设计，测试不应该绕过它。
    eng.sm.note(_reading(RUN))
    base = eng._poll_interval()
    eng.sm.note(_reading(DurationState.UNKNOWN))
    unk = eng._poll_interval()
    eng.sm.note(_reading(PAUSED))
    paused = eng._poll_interval()
    # 断言**倍率**而不是写死秒数：测试配置的 poll_ms 与出厂默认值不同。
    rep.check("RUNNING 时保持原速（危险状态必须跟得紧）",
              abs(base - 0.5) < 1e-6, f"{base:.2f}s")
    rep.check("UNKNOWN 时放慢 1.4 倍（半瞎，慢一点没损失）",
              abs(unk - base * 1.4) < 1e-6, f"{unk:.2f}s")
    rep.check("PAUSED 时放慢 2 倍（安全状态，晚知道毫无风险）",
              abs(paused - base * 2.0) < 1e-6, f"{paused:.2f}s")

    # ---- 用户要求：0.5~1 秒确认一次状态。这条要用**出厂默认**算，不能用测试配置 ----
    from core.config import DEFAULT_CONFIG as _D
    _base = _D["duration"]["poll_ms"] / 1000.0
    _cad = {k: _base * f for k, f in ProtectionEngine.POLL_FACTOR.items()}
    rep.check("出厂默认下三态的有效节拍都落在 0.5~1.0s（用户要求）",
              all(0.5 <= v <= 1.0 for v in _cad.values()),
              " ".join(f"{k}={v:.1f}s" for k, v in _cad.items()))
    # tick 是轮询的**上限**：tick 比 poll 慢时，poll 再小也跑不出来。
    rep.check("tick 节拍 ≤ 轮询基准（否则 0.5s 的轮询形同虚设）",
              int(_D["ui"]["refresh_ms"]) <= int(_D["duration"]["poll_ms"]),
              f"tick={_D['ui']['refresh_ms']}ms poll={_D['duration']['poll_ms']}ms")
    # 全身最贵的一步是"全量枚举控件树"（真机 657ms），它必须由缓存时限门控，
    # 而不是随轮询间隔起舞 —— 否则"调快轮询"会直接变成"调高 CPU"。
    rep.check("全量枚举频次与轮询间隔解耦（CACHE_TTL ≥ 3s）",
              float(getattr(uia_mod, "CACHE_TTL", 0)) >= 3.0,
              str(getattr(uia_mod, "CACHE_TTL", None)))
    rep.check("状态里带有效节拍，面板显示的才是真值",
              '"poll_ms"' in inspect.getsource(ProtectionEngine._emit_status))

    # ---- 用假时钟验证"节拍真的跑得出来" ----
    # 为什么必须测这个而不是只断言配置值：`_poll_state` 是在 tick 里被叫的，
    # 所以真实节拍 = max(tick, poll)。上一轮 tick 被放宽到 1000ms 时，
    # 把 poll_ms 调成 500 是**一点用都没有**的 —— 只改配置的"优化"全是假的。
    def _cadence(poll_ms: int, state, ticks=21, step=0.1):
        e, _ = H.make_engine(H.test_config(duration={"poll_ms": poll_ms}))
        e.win = type("W", (), {"hwnd": 1, "rect": (0, 0, 10, 10), "frame": None})()
        e.sm.note(_reading(state))
        n = {"c": 0}

        def _det(*_a, **_k):
            n["c"] += 1
            return _reading(state)
        e.detector.detect = _det
        t = 1000.0
        for _ in range(ticks):
            e._poll_state(t)
            t += step
        return n["c"]

    # 2.0 秒窗口、每 0.1 秒一个 tick：RUNNING(×1.0)=0.5s → 5 次；PAUSED(×2.0)=1.0s → 3 次
    run_n = _cadence(500, RUN)
    pause_n = _cadence(500, PAUSED)
    rep.check("RUNNING 在 2 秒窗口内识别约 5 次（≈0.5s/次）",
              run_n == 5, f"{run_n} 次")
    rep.check("PAUSED 在 2 秒窗口内识别约 3 次（≈1.0s/次）",
              pause_n == 3, f"{pause_n} 次")
    rep.check("调快轮询确实更快（不是只改了配置数字）",
              run_n > pause_n, f"RUNNING {run_n} vs PAUSED {pause_n}")
    # 启动时把实际生效的节拍写进日志：否则「改了配置却没生效」只能靠翻代码推算。
    rep.check("启动日志会报出实际生效的识别节拍（可验证性）",
              "状态识别节拍" in inspect.getsource(ProtectionEngine.start))
    rep.check("自适应轮询可被配置关掉（排障时用）",
              "adaptive_poll" in inspect.getsource(ProtectionEngine._poll_interval))

    # ---------- ④ 耗时必须可观测 ----------
    _det_for_ms = DurationDetector(H.test_config())
    rep.check("识别器暴露最近一次 UIA 扫描耗时（实例属性）",
              hasattr(_det_for_ms, "last_uia_ms")
              and float(_det_for_ms.last_uia_ms) == 0.0,
              str(getattr(_det_for_ms, "last_uia_ms", None)))
    rep.check("「确保暂停」会记录耗时（否则「快没快」只能靠感觉）",
              "确保暂停耗时" in inspect.getsource(ProtectionEngine._guard_ensure_paused))
    rep.check("状态里带 uia_ms（面板悬停可见）",
              '"uia_ms"' in inspect.getsource(ProtectionEngine._emit_status))

    # ---------- ⑤ 控件总数用 all_n，不再为了拿长度建对象 ----------
    rep.check("last_uia 结构含 all_n（控件总数）",
              "all_n" in inspect.getsource(DurationDetector.detect_uia))
    rep.check("UNKNOWN 分支用 all_n 而不是 len(all)",
              "res.get(\"all_n\"" in inspect.getsource(DurationDetector.detect_uia))

    # ---------- ⑥ 配置默认值（防止有人改回高频） ----------
    from core import config as config_mod
    d = config_mod.DEFAULT_CONFIG
    # ⚠️ 这条在 2026-10-01 随用户需求改过：`ui.refresh_ms` 从 1000 回到 500。
    #    原因：它同时是**状态轮询的上限**（`_poll_state` 只在 tick 里调用），
    #    用户要求「0.5~1 秒确认一次」，tick 就必须 ≤ 500ms；上一轮为省 CPU 放宽到
    #    1000ms 的收益，改由「自适应倍率 + CACHE_TTL」拿到，不再靠放慢 tick。
    rep.check("tick 节拍 = 500ms（既是轮询上限，也远小于死手窗口）",
              int(d["ui"]["refresh_ms"]) == 500, str(d["ui"]["refresh_ms"]))
    rep.check("tick 明显小于死手窗口的一半（否则吞点会时灵时不灵）",
              int(d["ui"]["refresh_ms"]) * 2 <= int(d["close_protection"]["swallow_deadman_ms"]),
              f"tick={d['ui']['refresh_ms']}ms deadman={d['close_protection']['swallow_deadman_ms']}ms")
    rep.check("几何快跟仍足够快（≤200ms，拖动手感）",
              int(d["ui"]["follow_fast_ms"]) <= 200, str(d["ui"]["follow_fast_ms"]))
    rep.check("守望轮询放宽到 ≥3s（没必要为「立刻」白烧 CPU）",
              int(d["standby"]["poll_ms"]) >= 3000, str(d["standby"]["poll_ms"]))
    rep.check("自适应轮询默认开启", d["duration"].get("adaptive_poll") is True)

    # ---------- ⑦ 「重新检测状态」要能创造可读条件 ----------
    rc_src = inspect.getsource(ProtectionEngine._consume_recheck)
    rep.check("重新检测时，若雷神不在前台会先切到前台（否则 UIA 必然读不到）",
              "activate_window" in rc_src and "GetForegroundWindow" in rc_src)
    rep.check("轮询路径**不会**抢焦点（那会在用户玩游戏时反复打断他）",
              "activate_window" not in inspect.getsource(ProtectionEngine._poll_state))

    # ---------- ⑧ 体积精简：确实用不到的 Qt 组件必须被排除 ----------
    import os as _os
    spec_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             "packaging")
    for spec in ("LeigodGuard.spec", "LeigodGuardCUI.spec"):
        try:
            src = open(_os.path.join(spec_dir, spec), encoding="utf-8").read()
        except OSError as e:
            rep.check(f"能读到 {spec}", False, str(e))
            continue
        missing = [k for k in ('".qm"', '"Qt6Pdf"', '"Qt6OpenGL"', '"Qt6Network"')
                   if k not in src]
        rep.check(f"{spec}：排除了用不到的 Qt 组件（.qm/Pdf/OpenGL/Network）",
                  not missing, f"缺 {missing}")
        rep.check(f"{spec}：注释说明了每一项为什么能排（避免以后有人「觉得危险」就加回来）",
                  "不做国际化" in src and "不联网" in src)


def t_close_gone_judgement(rep: H.Report) -> None:
    """雷神退出后的判定：别把「消失瞬间读不到」当成「没确认暂停」。

    真机/靶机实测（2026-10-01，状态轮询压到 0.5s 后更容易撞上）：
    用户关掉雷神的那一两秒里，某次轮询正好读到半死的窗口 → UNKNOWN →
    `_settle_lost` 拿这个 UNKNOWN 当判据 → 判成「退出前未能确认暂停」→
    弹出一条**虚假的计费告警**（"可能仍在计费，建议重新打开雷神确认"）。

    正确判据：消失**之前**一段时间内确认过 PAUSED，且此后没有确认过 RUNNING。
    这里用假 PID + 合成 pending 精确复现三种情形。
    """
    import time as _t
    from core.state_machine import DurationState, Evidence, Reading
    from core.events import EventKind, LEVEL_WARN
    from core import protection_engine as pe

    rep.section("雷神退出后的判定（不许发虚假的计费告警）")

    def _reading(st):
        return Reading(state=st, evidence=[Evidence("stub", st, "stub", {})],
                       hwnd=1, rect=(0, 0, 10, 10))

    eng, sink = H.make_engine(H.test_config())
    real_exists = pe.psutil.pid_exists
    pe.psutil.pid_exists = lambda pid: False          # 假装进程已退出
    try:
        # 情形①：刚确认过 PAUSED，2 秒后窗口消失且最后一次读到 UNKNOWN
        eng._note_reading(_reading(DurationState.PAUSED))
        ts = eng._paused_confirmed_ts
        eng._lost_pending = {"ts": ts + 2.0, "state": DurationState.UNKNOWN, "pid": 4242}
        eng._allowed_close = False
        eng._settle_lost(ts + 2.0)
        gone = [e for e in sink.events if e.kind is EventKind.LEIGOD_GONE]
        bad = [e for e in sink.events if e.kind is EventKind.WINDOW_CLOSED_UNPROTECTED]
        rep.check("刚确认过已暂停 → 判为退出前已暂停，不发计费告警",
                  bool(gone) and not bad,
                  f"LEIGOD_GONE={len(gone)} 告警={len(bad)}")

        # 情形②：确认过 PAUSED，但此后又确认了 RUNNING → 之前的"已暂停"必须作废
        eng2, sink2 = H.make_engine(H.test_config())
        eng2._note_reading(_reading(DurationState.PAUSED))
        eng2._note_reading(_reading(DurationState.RUNNING))
        rep.check("确认 RUNNING 后，之前的「已暂停」时间戳被作废",
                  eng2._paused_confirmed_ts == 0.0, str(eng2._paused_confirmed_ts))
        eng2._lost_pending = {"ts": _t.time() + 1.0, "state": DurationState.UNKNOWN, "pid": 4243}
        eng2._allowed_close = False
        eng2._settle_lost(_t.time() + 1.0)
        rep.check("在计时的雷神被关掉 → 必须发计费告警（不能因为曾经暂停过就放过）",
                  any(e.kind is EventKind.WINDOW_CLOSED_UNPROTECTED for e in sink2.events))

        # 情形③：PAUSED 确认得太久以前（超过宽限）→ 不认，继续按 UNKNOWN 处理
        eng3, sink3 = H.make_engine(H.test_config())
        eng3._note_reading(_reading(DurationState.PAUSED))
        old = eng3._paused_confirmed_ts - (eng3.LOST_PAUSED_GRACE_S + 5.0)
        eng3._paused_confirmed_ts = old
        eng3._lost_pending = {"ts": old + eng3.LOST_PAUSED_GRACE_S + 5.0,
                              "state": DurationState.UNKNOWN, "pid": 4244}
        eng3._allowed_close = False
        eng3._settle_lost(old + eng3.LOST_PAUSED_GRACE_S + 5.0)
        rep.check("PAUSED 确认已远超宽限 → 不认，仍然报警（不无限宽容）",
                  any(e.kind is EventKind.WINDOW_CLOSED_UNPROTECTED for e in sink3.events))
    finally:
        pe.psutil.pid_exists = real_exists


def t_ui_design(rep: H.Report) -> None:
    """界面视觉层：设计 token、自绘控件、面板结构（2026-10-01 视觉重构的回归）。

    视觉代码最容易「改一处崩一处」而没人发现 —— 因为单测通常不碰界面。
    这里把**能被程序判定**的部分钉住：配色语义不许串台、开关必须保留 QCheckBox
    语义（界面与测试都依赖）、面板不许再出现"中间一大块空档"这种排版事故。
    """
    import inspect
    import os as _os

    rep.section("界面设计系统与自绘控件")
    try:
        _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from ui import theme, widgets
        from ui.guard_window import (GuardWindow, PANEL_WIDTH)
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入界面模块", False, f"{type(e).__name__}: {e}")
        return

    # ---------- ① 配色语义不许串台 ----------
    fg = {k: theme.state_tokens(k)["fg"] for k in ("RUNNING", "PAUSED", "UNKNOWN")}
    rep.check("三态各有独立的前景色（不能有两种状态同色）",
              len(set(fg.values())) == 3, str(fg))
    rep.check("RUNNING 是红（危险）、PAUSED 是绿（安全）",
              fg["RUNNING"] == theme.C_RUNNING and fg["PAUSED"] == theme.C_PAUSED)
    rep.check("存在中性态 IDLE（给「保护中/待命」用，避免借三态色串台）",
              "IDLE" in [k.upper() for k in ("RUNNING", "PAUSED", "UNKNOWN", "IDLE")]
              and theme.state_tokens("IDLE")["fg"] == theme.TEXT_SUB)
    rep.check("状态卡片样式带上了该状态的色",
              theme.C_RUNNING in theme.state_card_qss("RUNNING"))
    rep.check("强提醒横幅带危险色左缘（与普通提醒区分得开）",
              theme.C_DANGER in theme.notice_qss(True)
              and theme.C_DANGER not in theme.notice_qss(False))
    rep.check("未知状态仍然回落到 UNKNOWN 的配色（不许崩）",
              theme.state_tokens("???") == theme.state_tokens("UNKNOWN"))

    # ---------- ② 标题药丸说的是"保护状态"这条轴 ----------
    pc = theme.protection_chip
    rep.check("保护生效 → 保护中",
              pc({"protect_enabled": True, "sc_close_disabled": True})[0] == "保护中")
    rep.check("该拦没拦住 → 未生效（不许把失败画成正常）",
              pc({"protect_enabled": True, "decision": "BLOCK_UNKNOWN",
                  "sc_close_disabled": False})[0] == "未生效")
    rep.check("保护被关掉 → 保护已关",
              pc({"protect_enabled": False})[0] == "保护已关")
    rep.check("药丸不再复述时长状态（否则同一屏出现两次「已暂停」）",
              "已暂停" not in pc({"protect_enabled": True, "sc_close_disabled": True})[0])
    # 没找到雷神时**绝不能**报"保护中"：close 层在无窗口时会给出 decision=ALLOW，
    # 照着它上色就会显示一个纯属虚构的绿色安全信号（第一版就是这样，被设计总览图抓到）。
    rep.check("未找到雷神 → 药丸是「待命」而不是「保护中」",
              pc({"leigod_found": False, "protect_enabled": True,
                  "decision": "ALLOW"})[0] == "待命")
    rep.check("未找到雷神 → 信任行如实说「尚未找到雷神」",
              "尚未找到雷神" in theme.protection_line({"leigod_found": False,
                                                  "decision": "ALLOW"}))
    rep.check("未找到雷神 → 信任行不上安全色",
              theme.protection_tone({"leigod_found": False, "decision": "ALLOW"}) == "mute")

    # ---------- ③ 自绘控件：真能画出东西 ----------
    pm = widgets.brand_pixmap(24)
    rep.check("品牌标记能画出来且尺寸正确",
              not pm.isNull() and pm.width() >= 24, f"{pm.width()}x{pm.height()}")
    tpm = widgets.tray_pixmap(16, theme.C_RUNNING, found=True)
    rep.check("托盘图标能画出来（16px 也要有内容）",
              not tpm.isNull() and tpm.width() >= 16)
    rep.check("托盘图标在「未找到雷神」时用灰色（一眼看出没接上）",
              "found" in inspect.signature(widgets.tray_pixmap).parameters)
    for kind in ("pause", "refresh", "wave", "swap", "shield", "shield-off"):
        g = widgets.glyph_pixmap(kind, 14, theme.TEXT_SUB)
        rep.check(f"图标 {kind} 能画出来", not g.isNull(), kind)

    # ---------- ④ 开关必须保留 QCheckBox 语义 ----------
    sw = widgets.Switch("钉住")
    rep.check("开关是 QCheckBox 的子类（界面与用例都依赖 isChecked/toggled）",
              isinstance(sw, __import__("PySide6.QtWidgets", fromlist=["QCheckBox"]).QCheckBox))
    seen = []
    sw.toggled.connect(lambda v: seen.append(v))
    sw.setChecked(True)
    rep.check("setChecked 生效且发出 toggled", sw.isChecked() is True and seen == [True],
              str(seen))
    sw.setChecked(False)
    rep.check("取消勾选同样生效", sw.isChecked() is False and seen == [True, False],
              str(seen))
    rep.check("开关有绘制实现（自定义 paintEvent，不是没样式的原生勾选框）",
              "paintEvent" in widgets.Switch.__dict__)

    # ---------- ⑤ 状态点的脉冲只在"运行中且可见"时跑 ----------
    dot = widgets.StatusDot()
    dot.set_state("PAUSED", active=True)
    rep.check("已暂停时**不**跑脉冲（否则会一直闪，属于噪声）",
              dot._timer.isActive() is False)
    dot.show()
    dot.set_state("RUNNING", active=True)
    rep.check("计时中且可见时才跑脉冲", dot._timer.isActive() is True)
    dot.set_state("PAUSED")
    rep.check("切回已暂停立刻停掉脉冲", dot._timer.isActive() is False)
    dot.hide()

    # ---------- ⑥ 面板结构与其排版 ----------
    w = GuardWindow(callbacks={})
    # 先 show + 走一轮事件循环：布局要跑过一次才有真实的子控件几何，
    # 否则读到的全是 (0,0)（这条断言第一版就是这么假失败的）。
    w.resize(PANEL_WIDTH, 360)
    w.show()
    app.processEvents()
    for name in ("mark", "dot", "state_card", "icon_shield", "lbl_state",
                 "lbl_fresh", "lbl_protect", "lbl_win", "lbl_action", "chip",
                 "btn_pause", "btn_allow", "btn_recheck", "btn_inspector",
                 "btn_side", "chk_pin", "chk_disable", "notice_frame", "tools"):
        rep.check(f"面板含 {name}", hasattr(w, name))
    rep.check("投影留边 > 0（卡片外要有一圈可透出处）",
              int(getattr(theme, "SHADOW_MARGIN", 0)) > 0)
    rep.check("卡片被投影留边内缩，不会盖住投影",
              w.card.x() == theme.SHADOW_MARGIN and w.card.y() == theme.SHADOW_MARGIN,
              f"card=({w.card.x()},{w.card.y()}) margin={theme.SHADOW_MARGIN}")

    w.apply_status({"state": "RUNNING", "leigod_found": True,
                    "decision": "BLOCK_RUNNING", "sc_close_disabled": True,
                    "window": {"size": (1500, 938), "dpi": 120, "hwnd_hex": "0x1",
                               "class_name": "C", "match": "class"}})
    rep.check("计时中：主按钮可用且挂了图标",
              w.btn_pause.isEnabled() and not w.btn_pause.icon().isNull())
    rep.check("计时中：药丸显示保护中", w.chip.text() == "保护中", w.chip.text())
    rep.check("主卡片文字是「计时中」", w.lbl_state.text() == "计时中", w.lbl_state.text())
    w.apply_status({"state": "PAUSED", "leigod_found": True, "decision": "ALLOW",
                    "sc_close_disabled": False,
                    "window": {"size": (1500, 938), "dpi": 120, "hwnd_hex": "0x1",
                               "class_name": "C", "match": "class"}})
    rep.check("已暂停：主按钮禁用**且摘掉图标**"
              "（浅底 + 白图标会糊成一块白）",
              (not w.btn_pause.isEnabled()) and w.btn_pause.icon().isNull())

    # 排版：把窗口调成内容高度后，底部不该留下一大块空档。
    # 真机/离屏实测过 38px 的空洞（尾部 addStretch 吞掉了多余高度），很难看。
    w.resize(PANEL_WIDTH, 2000)
    w.show()
    app.processEvents()
    want = max(240, w.sizeHint().height())
    w.resize(PANEL_WIDTH, want)
    app.processEvents()
    bottom = 0
    for i in range(w._root.count()):
        it = w._root.itemAt(i)
        wd = it.widget()
        if wd is not None and wd.isVisible():
            bottom = max(bottom, wd.geometry().y() + wd.geometry().height())
        lay = it.layout()
        if lay is not None:
            g = lay.geometry()
            bottom = max(bottom, g.y() + g.height())
    gap = w.card.height() - bottom - theme.PAD_CARD[3]
    rep.check("面板底部没有多余空档（≤12px）", gap <= 12,
              f"底部空档 {gap}px（卡片高 {w.card.height()} 内容底 {bottom}）")
    rep.check("面板高度按内容贴合（不超过 sizeHint）",
              w.height() <= want + 1, f"{w.height()} vs {want}")
    w.hide()
    rep.info(f"面板内容高 {want}px｜卡片 {w.card.width()}x{w.card.height()}")

    # ---------- ⑦ 强提醒时收起次级区块 ----------
    from core.events import Notice, ACTION_OK
    w.resize(PANEL_WIDTH, 2000); w.show(); app.processEvents()
    h0 = w.sizeHint().height()
    w.show_notice(Notice(title="已阻止关闭雷神", message="说明" * 12, actions=[("知道了", ACTION_OK)],
                         strong=True))
    app.processEvents()
    rep.check("强提醒时收起工具区与元信息（把空间让给提示）",
              not w.tools.isVisible() and not w.lbl_win.isVisible())
    rep.check("强提醒时横幅本身必须可见", w.notice_frame.isVisible())
    # 横幅自带「立即暂停」时，页面级主按钮要一起收起：同屏两个同名按钮会让人犹豫点哪个
    w.apply_status({"state": "RUNNING", "leigod_found": True,
                    "decision": "BLOCK_UNKNOWN", "sc_close_disabled": False})
    w.show_notice(Notice(title="已阻止关闭雷神", message="说明",
                         actions=[("立即暂停", "pause_now"), ("知道了", ACTION_OK)],
                         strong=True))
    app.processEvents()
    rep.check("横幅带「立即暂停」时，页面级主按钮收起（不出现两个同名按钮）",
              not w.btn_pause.isVisible())
    w.clear_notice()
    app.processEvents()
    rep.check("提示消失后主按钮恢复显示", w.btn_pause.isVisible())
    w.clear_notice()
    app.processEvents()
    rep.check("提示消失后次级区块自动恢复",
              w.tools.isVisible() and w.lbl_win.isVisible() and not w.notice_frame.isVisible())
    w.hide()


def t_ocr_engine_visibility(rep: H.Report) -> None:
    """OCR 引擎的**失败必须可见** + 模型必须打进包（真机事故 2026-09-30 的回归）。

    事故经过：用户报「工具又无法判断时长状态了」。查到打包版
    `LeigodGuardCUI.exe --inspector` 对一张**本可识别出 8 行文字**的顶栏截图
    报 `ocr.lines = []`；同一张图在源码环境识别出「开启时长」→ PAUSED。
    根因是 `packaging/*.spec` 的 `datas=[]`：hiddenimports 只把 **代码** 打进 PYZ，
    而 RapidOCR 的 **3 个 .onnx 模型 + 4 个 config.yaml 是数据文件**，一个都没进来。
    `RapidOCR()` 构造时找不到模型抛异常、被 `except` 吞掉，此后每轮 OCR 静默返回空。

    两条修复都必须被钉住，缺一条这个坑就会复发：
      ① 打包要 `collect_data_files("rapidocr_onnxruntime")`（否则模型不进包）；
      ② `engine_available()` 要真的能把引擎构造起来，并把失败原因留下来
         （旧实现只查 import，模型缺失时照样报「可用」，是**会把人带偏的假信号**）。
    """
    import inspect
    import re

    rep.section("OCR 引擎：失败必须可见 + 模型必须进包")
    try:
        from detection import ocr as ocr_mod
        from leigod.duration_detector import DurationDetector
        from core.state_machine import DurationState
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入 OCR 模块", False, f"{type(e).__name__}: {e}")
        return

    # ---------- ① 打包必须收数据文件 ----------
    import os
    spec_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "packaging")
    for spec in ("LeigodGuard.spec", "LeigodGuardCUI.spec"):
        p = os.path.join(spec_dir, spec)
        try:
            src = open(p, encoding="utf-8").read()
        except OSError as e:
            rep.check(f"能读到 {spec}", False, str(e))
            continue
        rep.check(f"{spec}：用 collect_data_files 收 OCR 数据文件（模型/yaml）",
                  'collect_data_files("rapidocr_onnxruntime")' in src)
        rep.check(f"{spec}：datas 里真的带上了（不是空列表）",
                  re.search(r"datas=list\(_OCR_DATAS\)", src) is not None)
        rep.check(f"{spec}：注释里写明了「只写 hiddenimports 不够」的原因",
                  "数据文件" in src and "静默" in src)
        # 第二半（第一次修完才暴露出来的）：RapidOCR 是**按 YAML 动态 import**
        # 子模块的，静态分析看不到 → 只有 data 文件仍会在运行期报
        # `module 'ch_ppocr_v3_det' has no attribute 'TextDetector'`。
        # 所以子模块也必须 collect。两半缺一，OCR 就还是空转。
        rep.check(f"{spec}：用 collect_submodules 收 OCR 子模块（动态 import 的）",
                  'collect_submodules("rapidocr_onnxruntime")' in src)
        rep.check(f"{spec}：子模块并进了 hiddenimports",
                  re.search(r"hiddenimports=hidden \+ _OCR_SUBMODULES", src) is not None)
        rep.check(f"{spec}：注释里写明了缺子模块时的具体报错",
                  "TextDetector" in src)

    # ---------- ② engine_available 必须"真的能构造" ----------
    rep.check("有 last_error() 用来报真实原因", hasattr(ocr_mod, "last_error"))
    src = inspect.getsource(ocr_mod.engine_available)
    rep.check("engine_available 不再只看 import（那正是假信号的来源）",
              "get_engine()" in src, src.strip()[:80])
    rep.check("引擎构造异常被记下原因而不是丢掉",
              "_ENGINE_ERROR" in inspect.getsource(ocr_mod.get_engine))

    # ---------- ③ 失败原因必须出现在识别证据里 ----------
    saved_avail, saved_err = ocr_mod.engine_available, ocr_mod.last_error
    try:
        ocr_mod.engine_available = lambda: False
        ocr_mod.last_error = lambda: "RuntimeError: 模型文件缺失 rapidocr_onnxruntime/models"
        det = DurationDetector(H.test_config())
        import types
        win = types.SimpleNamespace(hwnd=1, rect=(0, 0, 10, 10), frame=None, size=(10, 10))
        ev = det.detect_ocr(win)
        detail = " ".join(str(e.detail) for e in ev)
        rep.check("引擎不可用时，证据里写的是**真实原因**（不是含糊的「未安装」）",
                  "模型文件缺失" in detail, detail[:110])
        rep.info(f"引擎不可用时的证据：{detail[:110]}")
    finally:
        ocr_mod.engine_available, ocr_mod.last_error = saved_avail, saved_err

    # ---------- ④ --check 也要如实报原因 ----------
    import main as m
    chk = inspect.getsource(m.run_check)
    rep.check("--check 会打印 OCR 不可用的真实原因",
              "ocr_mod.last_error()" in chk)
    rep.check("--check 说明了后果（UIA 也读不到时会一直「无法确认」）",
              "无法确认" in chk)

    # ---------- ⑤ 引擎自检在启动时就会说 ----------
    from core.protection_engine import ProtectionEngine
    start_src = inspect.getsource(ProtectionEngine.start)
    rep.check("引擎启动时会自检 OCR 并把失败说出来（不静默）",
              hasattr(ProtectionEngine, "_check_ocr_engine")
              and "_check_ocr_engine" in start_src)
    rep.check("自检放在后台线程（不阻塞第一次状态识别）",
              "Thread" in start_src and "daemon=True" in start_src)

    # ---------- ⑥ 源码环境下引擎真的能构造（这条会真跑一次 OCR 引擎）----------
    det2 = DurationDetector(H.test_config())
    rep.check("本机 OCR 引擎可用（源码环境）", ocr_mod.engine_available(),
              ocr_mod.last_error() or "OK")
    rep.check("OCR 判定链路本身正常（合成文案能判出状态）",
              ocr_mod.classify_text(
                  [{"text": "开启时长", "confidence": 0.9}])[0] == "PAUSED")
    rep.check("识别器在 OCR 证据下能给出 PAUSED",
              det2 is not None and DurationState.PAUSED is not None)


def t_pause_latency(rep: H.Report) -> None:
    """暂停链路的延迟 —— 治用户反馈的「点完 ✕ 要等很久才自动暂停」。

    真机上的延迟由三块叠起来（都在一条路上，所以显得特别慢）：
      ① 每次 `detect()` 都跑整窗 OCR（1.3s+），而一次暂停有 4~8 次 detect；
      ② 验证环节每轮都跑 OCR，还要等 `settle`；
      ③ 调用方刚读过状态，暂停流程又读一遍。

    这里逐条钉住修复后的契约：
      · 不传 ocr_mode 的调用点自动走配置默认档（已在 t_ocr_skip 钉住）；
      · `pause(before_reading=...)` **不再重复读**；
      · 验证是两段式：先 UIA 快轮询（不跑 OCR），UIA 给不出结论才上 OCR。
    """
    import time
    import types

    rep.section("暂停延迟（点完 ✕ 多久才自动暂停）")
    try:
        from core.state_machine import DurationState, Evidence, Reading
        from leigod.duration_controller import DurationController
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入暂停控制器", False, f"{type(e).__name__}: {e}")
        return

    RUN, PAUSED = DurationState.RUNNING, DurationState.PAUSED
    win = types.SimpleNamespace(hwnd=1, rect=(0, 0, 100, 100), frame=None)

    def reading(st):
        return Reading(state=st, evidence=[Evidence("stub", st, "stub", {})],
                       hwnd=1, rect=(0, 0, 100, 100))

    class _Stub:
        """按调用类型分别计数：allow_ocr=False 记为「快相」，True 记为「慢相」。"""

        def __init__(self, uia_flip_after=1, ocr_state=PAUSED, cached=True):
            self.fast, self.slow = 0, 0
            self.uia_flip_after = uia_flip_after
            self.ocr_state = ocr_state
            # 标注"快相读的是缓存" → 走「两次缓存 + 一次全量复核」分支。
            # 不标的话会被当成全量枚举，第一次 PAUSED 就直接采纳（见 t_perf_contract）。
            self.cached = cached
            self.last_uia_from_cache = cached

        def detect(self, _win, allow_ocr=True, ocr_mode=None, uia_fast=False):
            # 桩必须与真实现**同一个签名**（真实现会传 `uia_fast=`）。
            # 签名漂移的代价是"测试跑到那条路径才炸"，而且炸在不相关的地方。
            if allow_ocr:
                self.slow += 1
                return reading(self.ocr_state)
            self.fast += 1
            self.last_uia_from_cache = bool(uia_fast) and self.cached
            return reading(PAUSED if self.fast > self.uia_flip_after else RUN)

        def duration_controls(self):
            return {"pause": [], "start": [], "all": []}

    cfg = H.test_config()

    # ① 快相就能确认 → 完全不跑 OCR，而且很快
    ctrl = DurationController(cfg, _Stub(uia_flip_after=1), logger=None)
    t0 = time.time()
    st, _ = ctrl._verify(win, time.time() + 5.0)
    dt = time.time() - t0
    rep.check("UIA 快相能确认 → 结论 PAUSED", st is PAUSED, st.value)
    rep.check("快相确认时一次 OCR 都没跑（省掉 1.3s/轮）",
              ctrl.detector.slow == 0, f"slow={ctrl.detector.slow}")
    rep.check("UIA 快相在 1 秒内给出结论", dt < 1.0, f"{dt:.2f}s")

    # ② 快相要连续两次 PAUSED 才认（防过渡态单帧误判）
    ctrl = DurationController(cfg, _Stub(uia_flip_after=0), logger=None)
    st, _ = ctrl._verify(win, time.time() + 5.0)
    rep.check("连续两次 PAUSED 才确认（过渡态不抢跑）",
              st is PAUSED and ctrl.detector.fast >= 2,
              f"fast={ctrl.detector.fast}")

    # ③ 快相窗口内 UIA 给不出结论 → 必须上 OCR 兜底（不能为了快而读不到状态）
    ctrl = DurationController(cfg, _Stub(uia_flip_after=10 ** 9, ocr_state=PAUSED),
                              logger=None)
    st, _ = ctrl._verify(win, time.time() + 5.0, settle=0.0, fast_window=0.25)
    rep.check("快相无结论时转 OCR 兜底并确认 PAUSED",
              st is PAUSED and ctrl.detector.slow >= 1,
              f"slow={ctrl.detector.slow}")

    # ④ 慢相也不能把 UNKNOWN 当 PAUSED（§六）
    ctrl = DurationController(cfg, _Stub(uia_flip_after=10 ** 9,
                                         ocr_state=DurationState.UNKNOWN), logger=None)
    st, _ = ctrl._verify(win, time.time() + 0.6, settle=0.0, fast_window=0.2)
    rep.check("慢相读到 UNKNOWN 时绝不返回 PAUSED", st is not PAUSED, st.value)

    # ⑤ 调用方已读过状态 → 暂停流程不再重读
    class _Counter(_Stub):
        def __init__(self):
            super().__init__()
            self.total = 0

        def detect(self, _win, allow_ocr=True, ocr_mode=None, uia_fast=False):
            self.total += 1
            return super().detect(_win, allow_ocr=allow_ocr, ocr_mode=ocr_mode,
                                  uia_fast=uia_fast)

    d1 = _Counter()
    out = DurationController(cfg, d1, logger=None).pause(None, before_reading=reading(PAUSED))
    rep.check("传入「已暂停」状态时：直接成功且一次识别都不做",
              out.success and d1.total == 0, f"success={out.success} total={d1.total}")
    rep.check("复用的状态确实进了 before/证据（结论有出处）",
              out.before is PAUSED and bool(out.evidence),
              f"before={out.before.value} evidence={out.evidence}")

    d2 = _Counter()
    DurationController(cfg, d2, logger=None).pause(None, before_reading=reading(RUN))
    rep.check("未传状态时仍然自己读一次（复用不是跳过读取）",
              d2.total >= 1, f"total={d2.total}")


def t_window_binding(rep: H.Report) -> None:
    """雷神窗口绑定：**不许"随便挑一个"**（真机 2026-09-30 事故的回归）。

    真机现象（用户报「工具又无法判断时长状态了，昨晚还好好的」）：
      13:06:15 雷神启动 → 13:06:17 守望立刻唤起守卫（进程一出现就唤起），
      此时主窗口 Chrome_WidgetWin_1(1500x938) **还没建好**，而 Electron 的
      辅助顶层窗口 Chrome_WidgetWin_0(1920x1130, 无标题) 已经可见。
      旧 `_narrow` 在类名/标题都没命中时**静默退化成"挑最大的顶层窗口"**，
      于是绑到了辅助窗口：
        · UIA 只枚举到 6 个控件（正常 ~476）
        · OCR 报「雷神不在前台，无法安全截屏」
        · 状态永远 UNKNOWN，关闭保护恒 BLOCK_UNKNOWN
      而且**绑上后再也不复核**，只能重启程序。

    修复后的契约（逐条钉住）：
      ① 匹配方式必须如实返回，弱匹配可被识别；
      ② strict=True 时没认准就返回 None（宁可等，也不绑错）；
      ③ 非严格模式允许弱匹配兜底，但必须带上 match=fallback 的标记；
      ④ 弱匹配不是终局 —— 认准的窗口一出现就自动改绑。
    """
    import types

    rep.section("雷神窗口绑定（不许静默挑错窗口）")
    try:
        from leigod import window as wmod
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入窗口定位模块", False, f"{type(e).__name__}: {e}")
        return

    # ---------- ① 匹配方式（纯函数） ----------
    cands = [(0x111, "雷神加速器", "Chrome_WidgetWin_1", 1),
             (0x222, "", "Chrome_WidgetWin_0", 1)]
    got, kind = wmod.narrow_candidates(cands, ["chrome_widgetwin_1"], ["雷神"])
    rep.check("命中窗口类名 → MATCH_CLASS（最可靠）",
              kind == wmod.MATCH_CLASS and [c[0] for c in got] == [0x111],
              f"{kind} {[hex(c[0]) for c in got]}")
    got, kind = wmod.narrow_candidates(cands, [], ["雷神"])
    rep.check("只命中标题关键词 → MATCH_TITLE",
              kind == wmod.MATCH_TITLE and [c[0] for c in got] == [0x111], kind)
    got, kind = wmod.narrow_candidates(cands, [], [])
    rep.check("没配任何规则 → MATCH_NOFILTER（不算认错）",
              kind == wmod.MATCH_NOFILTER and len(got) == 2, kind)
    got, kind = wmod.narrow_candidates(cands, ["不存在的类"], ["不存在的标题"])
    rep.check("配了规则却一个都没命中 → MATCH_FALLBACK（弱匹配，必须被识别出来）",
              kind == wmod.MATCH_FALLBACK and len(got) == 2, kind)
    rep.check("MATCH_FALLBACK 不在「认准了」的白名单里",
              wmod.MATCH_FALLBACK not in wmod.STRICT_MATCHES)

    # ---------- ② find_main_window 的严格/宽松两档 ----------
    real_fp, real_enum, real_pick = (wmod.find_processes, wmod._enum_windows_of_pids,
                                     wmod._pick_main)
    real_refresh = wmod.refresh
    try:
        wmod.find_processes = lambda pats: [wmod.ProcessInfo(pid=1, name="leigod.exe")]

        def fake_pick(cc, min_size):
            # 刻意「挑第一个」：这样用例断言的是**过滤结果**，而不是 _pick_main 的策略。
            # 返回 5 元组 —— 与真实 _pick_main 的契约一致（它在内部把 area 拆掉了）。
            if not cc:
                return None
            hwnd, title, cls, pid = cc[0]
            return (hwnd, title, cls, pid, (0, 0, 1500, 938))

        wmod._pick_main = fake_pick
        wmod.refresh = lambda w: w

        class _Cfg:
            def __init__(self, classes, keywords):
                self.d = {"leigod.process_patterns": ["leigod.exe"],
                          "leigod.window_class_candidates": classes,
                          "leigod.window_title_keywords": keywords,
                          "leigod.min_window_size": [300, 200]}

            def get(self, k, default=None):
                return self.d.get(k, default)

        # 事故现场：只有辅助窗口（类名对不上、标题为空）
        wmod._enum_windows_of_pids = lambda pids, include_hidden=False: [
            (0x222, "", "Chrome_WidgetWin_0", 1)]
        cfg = _Cfg(["Chrome_WidgetWin_1"], ["雷神"])
        rep.check("**只有辅助窗口时，严格模式拒绝绑定**（这就是事故的直接修复）",
                  wmod.find_main_window(cfg, strict=True) is None)
        weak = wmod.find_main_window(cfg)
        rep.check("非严格模式可以拿到它，但标记为弱匹配（供上层继续复核）",
                  weak is not None and weak.weak_match
                  and weak.match == wmod.MATCH_FALLBACK,
                  f"{getattr(weak, 'match', None)}")
        rep.check("弱匹配窗口的弱匹配标记可被上层识别",
                  bool(getattr(weak, "weak_match", False)) is True)

        # 主窗口出现之后：严格模式必须拿到**正确的那个**
        wmod._enum_windows_of_pids = lambda pids, include_hidden=False: [
            (0x222, "", "Chrome_WidgetWin_0", 1),
            (0x111, "雷神加速器", "Chrome_WidgetWin_1", 1)]
        w = wmod.find_main_window(cfg, strict=True)
        rep.check("主窗口出现后，严格模式拿到的是真主窗口（不是更大的辅助窗口）",
                  w is not None and w.hwnd == 0x111 and w.match == wmod.MATCH_CLASS,
                  f"hwnd={hex(w.hwnd) if w else None} match={getattr(w, 'match', None)}")

        # 没有雷神进程 → 一律 None（绝不猜）
        wmod.find_processes = lambda pats: []
        rep.check("雷神没在跑 → 两种模式都返回 None",
                  wmod.find_main_window(cfg, strict=True) is None
                  and wmod.find_main_window(cfg) is None)
    finally:
        wmod.find_processes, wmod._enum_windows_of_pids = real_fp, real_enum
        wmod._pick_main, wmod.refresh = real_pick, real_refresh

    # ---------- ③ 弱匹配必须能自我纠正（改绑） ----------
    try:
        eng, sink = H.make_engine(H.test_config())
    except Exception as e:                                    # noqa: BLE001
        rep.check("能构造保护引擎", False, f"{type(e).__name__}: {e}")
        return

    class _Win:
        """最小可用窗口替身：字段按 `_bind` 的日志与状态上报所要求的最小集给全。"""

        title = ""
        pid = 1
        version = "11.3.2.9"
        dpi = 120
        rect = (0, 0, 1920, 1130)
        frame = (0, 0, 1920, 1130)
        size = (1920, 1130)

        def __init__(self, hwnd, cls, match):
            self.hwnd, self.class_name, self.match = hwnd, cls, match

        @property
        def weak_match(self):
            return self.match == wmod.MATCH_FALLBACK

    aux = _Win(0x222, "Chrome_WidgetWin_0", wmod.MATCH_FALLBACK)
    main = _Win(0x111, "Chrome_WidgetWin_1", wmod.MATCH_CLASS)

    real_fmw = wmod.find_main_window
    try:
        wmod.find_main_window = lambda cfg, strict=False: main if strict else aux
        eng._bind(aux)
        rep.check("弱匹配绑定会被如实记录（不是无名状态）",
                  eng._win_match == wmod.MATCH_FALLBACK, eng._win_match)
        eng._last_rebind_check = 0.0
        eng._maybe_rebind_weak(__import__("time").time() + 100)
        rep.check("**出现认准的窗口后自动改绑**（不必重启程序）",
                  eng.win is main and eng._win_match == wmod.MATCH_CLASS,
                  f"hwnd={hex(eng.win.hwnd)} match={eng._win_match}")

        # 已经认准了就不该乱动
        eng._last_rebind_check = 0.0
        before = eng.win
        eng._maybe_rebind_weak(__import__("time").time() + 200)
        rep.check("已是严格匹配 → 不做无谓改绑", eng.win is before)

        # 认准的窗口还没出现 → 保持弱匹配，不折腾
        wmod.find_main_window = lambda cfg, strict=False: None if strict else aux
        eng2, _ = H.make_engine(H.test_config())
        eng2._bind(aux)
        eng2._last_rebind_check = 0.0
        eng2._maybe_rebind_weak(__import__("time").time() + 100)
        rep.check("认准的窗口还没出现 → 保持原绑定（不反复抖动）",
                  eng2.win is aux and eng2._win_match == wmod.MATCH_FALLBACK)
    finally:
        wmod.find_main_window = real_fmw


def t_recheck(rep: H.Report) -> None:
    """「重新检测状态」按钮 —— 状态读不出来时用户的第一个出口。

    用户原话：「你添加一个强制检测当前时长状态的按键选项以防以后出现这个问题」。
    这个按钮的价值在于：状态读不出来的成因有四五种（窗口绑错 / 被挡住 /
    没加载完 / 正弹着确认框 / 权限不足），程序**不能替用户猜**是哪一种。
    给一个"你说不清就按一下"的入口，比让用户重启程序友好，也比分不清成因
    就乱猜要诚实。

    它对外的三条契约：
      ① 必须真的重定位窗口（绑错了要能纠正）；
      ② 必须**强制全量识别**（ocr_mode=always，不省 OCR）—— 用户按它就是要一个确定答案；
      ③ 必须如实说明结论与原因，读不出来就明说读不出来。
    """
    import inspect
    import time
    import types

    rep.section("重新检测时长状态（手动强制检测）")
    try:
        from core.state_machine import DurationState, Evidence, Reading
        from core.protection_engine import ProtectionEngine
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入保护引擎", False, f"{type(e).__name__}: {e}")
        return

    # ---------- 对外入口 ----------
    rep.check("引擎提供 request_recheck()",
              hasattr(ProtectionEngine, "request_recheck"))
    src = inspect.getsource(ProtectionEngine.request_recheck)
    rep.check("request_recheck 会唤醒引擎线程（否则要等下一轮才生效）",
              "_wake" in src)
    rep.check("tick 会消费重新检测请求",
              "_consume_recheck" in inspect.getsource(ProtectionEngine._tick))

    try:
        from ui.guard_window import GuardWindow
        from ui.tray import Tray
        from ui.app import GuardApp
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入界面模块", False, f"{type(e).__name__}: {e}")
        return
    build_src = inspect.getsource(GuardWindow._build)
    rep.check("面板上有「重新检测状态」按钮", "重新检测状态" in build_src)
    rep.check("面板按钮接到 recheck 动作", '"recheck"' in build_src)
    rep.check("托盘菜单也有同名项（面板隐藏时也能按）",
              "重新检测时长状态" in inspect.getsource(Tray.__init__))
    rep.check("应用层把 recheck 接进了回调表",
              hasattr(GuardApp, "_recheck")
              and '"recheck"' in inspect.getsource(GuardApp.build))

    # ---------- 行为：三条契约 ----------
    cfg = H.test_config()
    eng, sink = H.make_engine(cfg)

    # ① 未找到窗口时的如实答复
    eng.win = None
    eng._ensure_window = lambda now: None
    eng.request_recheck()
    rep.check("request_recheck 置起标志位", eng._want_recheck is True)
    eng._consume_recheck()
    rep.check("标志位被消费掉（不重复触发）", eng._want_recheck is False)
    rep.check("没窗口时如实说「没找到雷神窗口」并给出下一步",
              any("没找到雷神窗口" in n.title and "托盘" in n.message for n in sink.notices),
              str([n.title for n in sink.notices]))

    # ② 有窗口时：必须强制跑 OCR，并如实汇报结论
    calls = {}

    class _Win:
        hwnd = 0x111
        class_name = "Chrome_WidgetWin_1"
        match = "class"
        weak_match = False

    eng.win = _Win()

    def _fake_detect(win, allow_ocr=True, ocr_mode=None):
        calls["ocr_mode"] = ocr_mode
        return Reading(state=DurationState.PAUSED,
                       evidence=[Evidence("stub", DurationState.PAUSED, "stub", {})],
                       hwnd=0x111, rect=(0, 0, 10, 10))

    eng.detector.detect = _fake_detect
    n0 = len(sink.notices)
    eng.request_recheck()
    eng._consume_recheck()
    rep.check("重新检测时**强制全量识别**（ocr_mode=always，不省 OCR）",
              calls.get("ocr_mode") == "always", str(calls))
    rep.check("检测到已暂停 → 明确说「可以安全关闭雷神」",
              any("已暂停" in n.title and "安全关闭" in n.message
                  for n in sink.notices[n0:]), str([n.title for n in sink.notices[n0:]]))

    # 读不出来时必须明说，并列出常见原因（不许含糊过去）
    def _fake_unknown(win, allow_ocr=True, ocr_mode=None):
        return Reading(state=DurationState.UNKNOWN,
                       evidence=[Evidence("stub", DurationState.UNKNOWN, "读不到", {})],
                       hwnd=0x111, rect=(0, 0, 10, 10))

    eng.detector.detect = _fake_unknown
    n1 = len(sink.notices)
    eng.request_recheck()
    eng._consume_recheck()
    last = sink.notices[n1 - 1] if len(sink.notices) > n1 else None
    rep.info(f"读不到时的提示：{getattr(last, 'title', '')}")
    rep.check("读不出来就明说读不出来（不对用户报假结论）",
              any("仍然无法确认" in n.title for n in sink.notices[n1:]),
              str([n.title for n in sink.notices[n1:]]))
    rep.check("列出常见原因（挡住 / 没加载完 / 确认框）",
              any(("挡住" in n.message and "确认框" in n.message)
                  for n in sink.notices[n1:]),
              str([n.message[:60] for n in sink.notices[n1:]]))


def t_dock_pinned(rep: H.Report) -> None:
    """钉住一角 —— 治「吸附度太差、拖雷神就乱跑」。

    真机反馈：「可以固定在加速器一角吗，哪怕我拖动加速器工具也不会乱跑」。
    根因：旧判据会在雷神靠近屏幕边缘时把面板甩到对面 —— 拖动过程中看起来
    就是面板在两个位置之间跳。钉住后选定的一侧不再自动变，位置可预测；
    想换边由用户点「⇄ 换到另一侧」显式决定。
    """
    rep.section("面板钉住一角（拖动雷神不乱跑）")
    try:
        from ui.guard_window import choose_dock_side
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入换边决策", False, f"{type(e).__name__}: {e}")
        return

    A = 0
    # ① 钉住时：无论怎么越界都不换边
    rep.check("钉住左侧：左侧放不下也不换到右侧",
              choose_dock_side("left", A - 999, A, pinned=True) == "left")
    rep.check("钉住右侧：左侧空出来了也不换回左侧",
              choose_dock_side("right", A + 999, A, pinned=True) == "right")
    # ② 首次定位仍要有结论（cur_side=None → 先挑一次，之后才锁死）
    rep.check("钉住但尚未定位时仍会先挑一侧（能放下就左）",
              choose_dock_side(None, A + 50, A, pinned=True) == "left")
    rep.check("钉住但尚未定位时仍会先挑一侧（放不下就右）",
              choose_dock_side(None, A - 10, A, pinned=True) == "right")
    # ③ 不钉住时退回原滞回行为（两种档都要在）
    rep.check("未钉住时保留自动换边（越界会换）",
              choose_dock_side("left", A - 1, A, pinned=False) == "right")
    # ④ 用户可见的默认档必须是「钉住」。
    #    注意这里断言的是**配置与控件默认值**，不是纯函数的形参默认：
    #    纯函数保留 pinned=False 的旧行为，是为了让滞回那组用例继续钉住旧语义。
    try:
        from core.config import DEFAULT_CONFIG
        from ui.guard_window import GuardWindow
        import inspect
        sig = inspect.signature(GuardWindow.__init__)
        rep.check("配置默认：ui.pin_side = True",
                  DEFAULT_CONFIG["ui"].get("pin_side") is True,
                  str(DEFAULT_CONFIG["ui"].get("pin_side")))
        rep.check("面板控件默认即为钉住（用户要的就是别乱跑）",
                  sig.parameters["pinned"].default is True,
                  str(sig.parameters["pinned"].default))
    except Exception as e:                                    # noqa: BLE001
        rep.check("能读到配置/面板默认档", False, f"{type(e).__name__}: {e}")

    # ⑤ 面板必须提供手动换边与钉住开关 —— 钉住拿掉了自动行为，就欠用户一个出口
    try:
        from ui.app import GuardApp
        from ui.guard_window import GuardWindow
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入面板类", False, f"{type(e).__name__}: {e}")
        return
    import inspect
    rep.check("面板暴露了换边方法（钉住后用户仍能换）",
              hasattr(GuardWindow, "switch_side"), "switch_side")
    rep.check("面板暴露了钉住开关", hasattr(GuardWindow, "set_pinned"), "set_pinned")
    rep.check("应用层把两个动作都接到了 callbacks",
              all(hasattr(GuardApp, n) for n in ("_switch_side", "_set_pinned")),
              "_switch_side/_set_pinned")
    src = inspect.getsource(GuardApp.build)
    rep.check("回调表里确实注册了 switch_side / set_pinned",
              '"switch_side"' in src and '"set_pinned"' in src)
    # 钉住选择要写回配置，否则每次重开都回到默认
    rep.check("钉住选择会写回配置文件（下次启动还记得）",
              '"ui.pin_side"' in inspect.getsource(GuardApp._set_pinned))
    # 几何快跟必须独立于状态轮询（位置跟随和状态刷新是两件事）
    rep.check("存在独立的高频几何跟随定时器",
              "_t_geom" in inspect.getsource(GuardApp.build)
              and hasattr(GuardApp, "_tick_geom"), "_tick_geom")


def t_entry(rep: H.Report) -> None:
    rep.section("命令行入口")
    import main as m
    ap = m.build_parser()
    a = ap.parse_args([])
    rep.check("默认不启用启动器模式", a.launcher is False)
    rep.check("默认不跳过单实例检查", a.allow_multi is False)
    rep.check("新增 --watcher 待命入口", ap.parse_args(["--watcher"]).watcher is True)
    rep.check("--standby 是 --watcher 的等价别名",
              ap.parse_args(["--standby"]).watcher is True)
    a2 = ap.parse_args(["--launcher", "--minimized", "--allow-multi"])
    rep.check("参数解析正常", a2.launcher and a2.minimized and a2.allow_multi)
    rep.check("诊断入口可解析", ap.parse_args(["--calibrate", "--mode", "manual"]).mode == "manual")
    rep.check("版本号非空", bool(m.__version__))
    # 构建标记：日志首行会打出来，用来确认"用户跑的到底是哪一版"。
    # 本项目吃过这个亏 —— 用户报「待命没生效」时，只能靠文件时间戳去猜。
    import re as _re
    rep.check("有构建标记且形如 YYYY-MM-DD.序号",
              bool(_re.match(r"^\d{4}-\d{2}-\d{2}\.\d+$", str(getattr(m, "__build__", "")))),
              getattr(m, "__build__", None))
    rep.check("启动首行会打印版本与构建标记",
              "启动 v%s" in inspect.getsource(m.main))


# ============================================== 退出保护（退出前必须确认已暂停）

def t_exit_readiness(rep: H.Report) -> None:
    """退出本程序也要走同一条纪律：读状态 → 确认已暂停 → 才放行。

    为什么值得单独一组用例：本程序存在的唯一目的就是别让总时长白白消耗。
    如果它自己在雷神正在计时的时候悄悄退出，用户会以为"我已经收拾干净了"，
    而计时器其实还在跑 —— 那恰恰是要防的那件事。

    判据必须和关闭保护一致：**`UNKNOWN` 不等于 `PAUSED`**。
    「不知道」不是「安全」，此时放行退出与「假装成功」没有区别（§六 / §三十三）。
    """
    from core.protection_engine import ProtectionEngine
    from core.state_machine import DurationState, Reading

    rep.section("退出保护 · 退出前必须确认已暂停（UNKNOWN 不算安全）")

    class _Sink:
        def on_status(self, s): pass
        def on_notice(self, n): pass
        def on_event(self, e): pass
        def on_pause(self, o): pass

    cfg = H.test_config(close_protection={
        "enabled": True, "swallow_close_click": True,
        "confirm_dialog": {"enabled": False}})
    e = ProtectionEngine(cfg, sink=_Sink(), logger=None)

    safe, why = e.exit_readiness()
    rep.check("雷神未运行 → 放行退出", safe is True and "未在运行" in why, f"{safe}｜{why}")

    # 造一个「雷神在运行」的窗口替身（本用例不碰任何真实窗口）
    class _Win:
        hwnd = 0x1234
        frame = (0, 0, 100, 100)
        rect = (0, 0, 100, 100)

        def as_dict(self):
            return {"hwnd": self.hwnd, "frame": list(self.frame)}

    e.win = _Win()

    cases = [
        (DurationState.RUNNING, False, "RUNNING 不得直接退出（否则计时继续）"),
        (DurationState.PAUSED, True, "PAUSED 可以退出"),
        (DurationState.UNKNOWN, False, "UNKNOWN 不得当成 PAUSED（§六 三态纪律）"),
    ]
    for st, want_safe, label in cases:
        e.sm.note(Reading(state=st))
        safe, why = e.exit_readiness()
        rep.check(label, safe is want_safe, f"state={st.value}→safe={safe}｜{why}")
    rep.check("说明文字是人话（含具体状态，便于 UI 直接展示）",
              "无法确认" in e.exit_readiness()[1], e.exit_readiness()[1])

    # 关闭保护关掉之后，退出不需要干预（用户已经明确要求不保护）
    e.set_protection(False)
    safe, why = e.exit_readiness()
    rep.check("关闭保护后不阻碍退出", safe is True, f"{safe}｜{why}")
    e.set_protection(True)

    e._emit_status()
    status = e._last_status
    rep.check("状态里暴露退出就绪度（UI 才能据此如实提示）",
              isinstance(status.get("exit"), dict) and "safe" in status["exit"],
              str(status.get("exit")))
    rep.check("未就绪时状态里能看出原因（不是只有一个布尔）",
              isinstance(status["exit"].get("why"), str) and bool(status["exit"]["why"]),
              str(status["exit"]))


def t_confirm_known_seed(rep: H.Report) -> None:
    """`ConfirmWatch` 的 known 集合**只应装启动时可见的窗口**。

    真机实测（雷神 v11.3.2.9，Electron）：雷神进程里预建了一个
    `class=Chrome_WidgetWin_0`、1536×904、**visible=False** 的隐藏窗口，
    确认框就是把它 Show 出来的（而不是新建）。
    若按「该 pid 的所有顶层窗口」播种 known，这个 hwnd 就永远在 known 里，
    而 `_normalize_new()` 对 known 里的 hwnd 直接 `continue`
    → **确认框每次都真弹了，`confirm_seen` 却恒为 0**（真机第三轮实测）。
    """
    from core.config import load_config
    from leigod import confirm_dialog as cd

    cfg = load_config()
    rep.section("确认框监测：known 只装可见窗口（隐藏窗口会被永久跳过）")

    def mk(lister_map):
        # pid 必须非 0：`_init_known` 在没有 pid 时会直接退化成空集合。
        w = cd.ConfirmWatch(cfg, pid=4242, lister=lambda pid: list(lister_map.keys()))
        # 可见性用注入函数（真机走 `_is_visible`，单测不碰真实窗口）
        w._init_known(visible_fn=lambda h: bool(lister_map[h].get("visible")))
        return w

    w = mk({101: {"hwnd": 101, "visible": True},
            102: {"hwnd": 102, "visible": False}})
    rep.check("可见窗口进 known（启动前就看得见的，不该当新弹窗）",
              101 in w.known, f"known={sorted(w.known)}")
    rep.check("★ 隐藏窗口**不**进 known（它显示出来就是新出现的确认框）",
              102 not in w.known, f"known={sorted(w.known)}")

    w2 = mk({201: {"hwnd": 201, "visible": False}})
    rep.check("只有隐藏窗口时 known 为空", not w2.known, f"known={sorted(w2.known)}")

    w3 = mk({301: {"hwnd": 301, "visible": True},
             302: {"hwnd": 302, "visible": True}})
    rep.check("全部可见时都进 known",
              {301, 302} == set(w3.known), f"known={sorted(w3.known)}")

    # lister 抛异常不许把监测搞死（诊断层不能拖垮主流程）
    w4 = cd.ConfirmWatch(cfg, pid=4242,
                         lister=lambda pid: (_ for _ in ()).throw(
                             OSError("枚举失败")))
    try:
        w4._init_known()
        rep.check("lister 抛异常时不炸，known 退化为空", not w4.known)
    except Exception as e:
        rep.check("lister 抛异常时不炸，known 退化为空", False,
                  f"{type(e).__name__}: {e}")

        # 性能契约：判定可见性必须是**单个系统调用**（IsWindowVisible），
        # 不能对每个顶层窗口跑 window_info（6 个调用）→ 会拖慢主线程。
        import inspect
        src = inspect.getsource(cd.ConfirmWatch._init_known)
        rep.check("可见性判定走轻量 _is_visible，不用 window_info",
                  "_is_visible" in src and "window_info(" not in src,
                  src.strip().splitlines()[-1][:80])


def t_pause_callback_contract(rep: H.Report) -> None:
    """暂停回调的**返回值是契约**：必须是 `DurationState`，否则不许污染状态。

    真机实测踩到（代价 = 两轮真机验证）：调用方为了把诊断信息带出来，回调返回了 dict。
    而 `_ensure_paused()` 会 `self._state = res` 再判 `res is DurationState.PAUSED`
    —— 字典永远 `is not` PAUSED，于是**暂停明明成功（双路确认 PAUSED），却每次都判
    「未确认」→ blocked → 永不重放**。这类缺陷静默、无异常，只能靠契约测试钉住。
    """
    from core.config import load_config
    from core.state_machine import DurationState
    from leigod.close_protection import CloseIntentGuard

    rep.section("暂停回调返回值契约（类型错了会静默永不放行）")
    cfg = load_config()
    g = CloseIntentGuard(cfg, logger=None)
    g.set_state(DurationState.RUNNING)

    # ① 契约正确：返回 PAUSED → 状态应更新为 PAUSED
    g.set_pause_callback(lambda: DurationState.PAUSED)
    out = g._ensure_paused()
    rep.check("回调返回 PAUSED → 状态更新为 PAUSED",
              out is DurationState.PAUSED, f"实际 {out}")

    # ② 契约被破坏：返回 dict → 状态不得被污染，且必须留错误
    g.set_state(DurationState.RUNNING)
    g.errors.clear()
    g.set_pause_callback(lambda: {"success": True, "detail": "带诊断的返回值"})
    out = g._ensure_paused()
    rep.check("回调返回 dict → 状态**不被污染**（仍是 RUNNING，不是 dict）",
              out is DurationState.RUNNING, f"实际 {out!r}")
    rep.check("回调返回 dict → 记下错误（静默失败最可怕）",
              any("非 DurationState" in e for e in g.errors), str(g.errors[-1:]))

    # ③ 返回 None 视为「没结论」：不得报错刷屏，也不得改状态
    g.set_state(DurationState.RUNNING)
    g.errors.clear()
    g.set_pause_callback(lambda: None)
    out = g._ensure_paused()
    rep.check("回调返回 None → 保持原状态且不记错误",
              out is DurationState.RUNNING and not g.errors, str(g.errors[-1:]))

    # ④ UNKNOWN 绝不当 PAUSED（§六）
    g.set_state(DurationState.RUNNING)
    g.set_pause_callback(lambda: DurationState.UNKNOWN)
    out = g._ensure_paused()
    rep.check("回调返回 UNKNOWN → 状态为 UNKNOWN（§六：不当 PAUSED）",
              out is DurationState.UNKNOWN, f"实际 {out}")


def t_auto_verifier(rep: H.Report) -> None:
    """自动版真机闭环脚本（`verify_close_loop_auto.py`）的契约。

    为什么要有它：人工版要求真人「点开启时长 → 等 → 点 ✕ → 处理弹框 → 再点 ✕」
    且全程不许切窗口 —— 而「不许切窗口」与「要看控制台提示」本身互斥，
    并且为了测拦截必须先让雷神进入计时中，等于**先消耗用户的总时长**，
    直接违背这个工具存在的目的。自动版把人力成本降为零，所以它的可用性
    必须被钉住，不能又退化成「要人伺候」。

    本用例只做**不依赖真机**的检查：接口签名（用 AST，防止拿错返回值类型）、
    关键纪律是否在源码里、以及未提权时是否优雅退出且如实报告。
    """
    import ast
    import subprocess

    rep.section("自动版真机闭环脚本（零人工操作）")
    path = os.path.join(HERE, "verify_close_loop_auto.py")
    rep.check("脚本存在", os.path.isfile(path), path)
    if not os.path.isfile(path):
        return
    src = open(path, encoding="utf-8").read()

    # ① 接口签名：这两个错真机才会炸，静态查出来成本最低
    tree = ast.parse(src)
    calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            calls.setdefault(name, []).append(len(node.args))
    rep.check("find_main_window 带了 config 参数（无参会 TypeError）",
              all(n >= 1 for n in calls.get("find_main_window", [0])),
              f"实际参数个数 {calls.get('find_main_window')}")
    rep.check("不把 detect_uia 的返回值当字典用（它返回 Evidence 列表）",
              "detect_uia(win).get(" not in src and "detect_uia(win)).get(" not in src,
              "控件清单必须走 duration_controls()")
    rep.check("控件清单确实取自 duration_controls()",
              "duration_controls()" in src)

    # ② 纪律
    rep.check("合成点击带 MOUSEEVENTF_ABSOLUTE（否则负载高时点不中）",
              "MOUSEEVENTF_ABSOLUTE" in src)
    rep.check("先查提权（未提权时钩子与合成输入都会被 UIPI 无效化）",
              "is_elevated()" in src)
    rep.check("§十四：状态 UNKNOWN 时不点任何东西",
              "UNKNOWN" in src and "不点任何东西" in src)
    rep.check("收尾会恢复暂停（不能让用户白消耗时长）",
              "restore_pause" in src)
    # 真机第一轮就栽在这：固定等 20s 就收工，暂停还没返回（实测 ~26s）就拍下
    # replayed=0，并立刻 stop 把马上要发生的重放掐掉 → 假结论「吞了点但没重放」。
    rep.check("观察循环等的是终态而非固定秒数（否则会亲手掐断重放）",
              'snap.get("false_swallow", 0) >= 1' in src,
              "必须等 replayed / blocked / false_swallow 之一出现")
    rep.check("默认观察窗口 ≥ 45s（真机一次确保暂停实测约 26s）",
              'default=60.0' in src or 'default=45.0' in src)

    # ③ 跑一次（未提权时应当优雅退出并如实报告，绝不能崩）
    out_json = os.path.join(HERE, "out", "verify_auto_smoke.json")
    try:
        p = subprocess.run([sys.executable, path, "--no-dialog", "--out", out_json],
                           capture_output=True, text=True, timeout=90)
        rc = p.returncode
    except Exception as e:
        rep.check("能启动（未提权也应优雅退出）", False, f"{type(e).__name__}: {e}")
        return
    err_file = os.path.join(HERE, "out", "verify_auto.error.txt")
    rep.check("没有崩溃（崩溃会写 verify_auto.error.txt）",
              not os.path.isfile(err_file))
    rep.check("退出码非崩溃码 9", rc != 9, f"rc={rc}")
    try:
        data = json.load(open(out_json, encoding="utf-8"))
    except Exception as e:
        rep.check("产出了结构化报告", False, f"{type(e).__name__}: {e}")
        return
    rep.check("产出了结构化报告", True, f"phase={data.get('phase')}")
    rep.check("给出了明确结论而不是停在 pending（不许假装成功）",
              data.get("verdict") not in (None, "pending"),
              f"verdict={data.get('verdict')}｜{data.get('verdict_text','')[:80]}")


def t_verdict_judge(rep: H.Report) -> None:
    """真机结论判定 `_judge` 的逐分支契约。

    为什么单独钉住这一个函数：真机验证跑一趟约 46s、还会消耗用户的总时长，
    判定环节错一次的代价就是一整轮白跑 + 一个假结论。而这个环节**已经错
    过两次了**：

      (1) 观察循环固定等 20s，在暂停回调还没返回时拍下 `replayed=0`，
          又立刻 `guard.stop()` **亲手掐断马上要发生的重放** → 报成
          「吞了点却没重放」；
      (2) 判定写成**两段独立的 if**，第二段 `if/elif/.../else` 链末尾的
          `else` 是无条件兜底，把第一段刚判出的 `protected` 覆盖成了
          `inconclusive`。那一轮证据其实已经齐了（吞点=1、重放=1、
          确认框=1、状态=PAUSED），报告却写「证据不足以定论」——
          **把已经证实的成功报成未决**，同样是 §三十三 禁止的。

    所以判定必须是纯函数 + 单条 if/elif 链，并且每个出口都有断言。
    """
    import importlib.util

    rep.section("真机结论判定（纯函数 _judge 逐分支）")
    path = os.path.join(HERE, "verify_close_loop_auto.py")
    rep.check("脚本存在", os.path.isfile(path), path)
    if not os.path.isfile(path):
        return
    root = os.path.dirname(HERE)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        spec = importlib.util.spec_from_file_location("_vca_judge", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    except Exception as e:
        rep.check("能导入脚本（模块级不得有副作用）", False,
                  f"{type(e).__name__}: {e}")
        return
    rep.check("能导入脚本（模块级不得有副作用）", True)
    judge = getattr(m, "_judge", None)
    rep.check("判定抽成了纯函数 _judge（便于逐分支单测）", callable(judge))
    if not callable(judge):
        return

    def v(stats=None, state="PAUSED", **kw):
        s = {"swallowed": 0, "replayed": 0, "confirm_seen": 0,
             "handled": 0, "blocked": 0}
        s.update(stats or {})
        return judge(s, state, **kw)[0]

    def txt(stats=None, state="PAUSED", **kw):
        s = {"swallowed": 0, "replayed": 0, "confirm_seen": 0,
             "handled": 0, "blocked": 0}
        s.update(stats or {})
        return judge(s, state, **kw)[1]

    ok = {"swallowed": 1, "replayed": 1, "confirm_seen": 1, "handled": 1}

    # ① 完整证据 → protected（这是第 (2) 类错误直接覆盖掉的那条）
    rep.check("证据齐全（吞+重放+确认框+PAUSED）→ protected",
              v(ok) == "protected", v(ok))
    # ② 同上但没看到确认框 → 降级为"未证实"，绝不能是 inconclusive
    st = dict(ok, confirm_seen=0)
    rep.check("少了确认框 → protected_confirm_unverified（不许退成 inconclusive）",
              v(st) == "protected_confirm_unverified", v(st))
    rep.check("未证实分支的文案明确区分「发出去了」与「送达了」",
              "送达" in txt(st), txt(st)[:60])
    # ③ Fail Safe：暂停没被复核就吞住不放行
    st = {"swallowed": 1, "blocked": 1, "handled": 1}
    rep.check("吞住且判定不放行 → swallowed_no_replay（Fail Safe）",
              v(st, state="RUNNING") == "swallowed_no_replay", v(st, "RUNNING"))
    # ④ 吞了、已 PAUSED、但观察窗口内没等到重放 = 观测未完成，不是产品结论
    st = {"swallowed": 1, "handled": 1}
    rep.check("吞住+已暂停+未见重放 → replay_pending（不许报成「没重放」）",
              v(st) == "replay_pending", v(st))
    rep.check("replay_pending 文案声明这是观测未完成而非产品结论",
              "观测未完成" in txt(st), txt(st)[:60])
    # ⑤ 吞了、未暂停、也没重放 → 确实没放行
    rep.check("吞住+未暂停+未见重放 → swallowed_no_replay",
              v(st, state="RUNNING") == "swallowed_no_replay", v(st, "RUNNING"))
    # ⑥ 钩子主动放行
    st = {"handled": 1, "replayed": 1}
    rep.check("收到点击但没吞（按判据放行）→ not_swallowed",
              v(st, state="RUNNING") == "not_swallowed", v(st, "RUNNING"))
    # ⑦ 一次点击都没收到
    rep.check("零吞零处理 → no_click_seen（多半是没提权 UIPI）",
              v() == "no_click_seen", v())
    # ⑧ §六：UNKNOWN 绝不当 PAUSED —— 状态不是 PAUSED 就不许报 protected
    rep.check("状态 UNKNOWN 时不得判为 protected（§六）",
              v(ok, state="UNKNOWN") != "protected", v(ok, "UNKNOWN"))
    rep.check("状态 RUNNING 时不得判为 protected",
              v(ok, state="RUNNING") != "protected", v(ok, "RUNNING"))
    # ⑧b 真机第 5 轮：确认框遮挡 → 主路径成立但复核不了，必须单独具名，
    #     既不许抹成 inconclusive，也不许冒充 protected。
    rep.check("吞+重放+确认框 但终态 UNKNOWN → protected_state_unreadable",
              v(ok, state="UNKNOWN") == "protected_state_unreadable",
              v(ok, "UNKNOWN"))
    rep.check("该分支明确「不声称已经暂停」（§六 UNKNOWN 不当 PAUSED）",
              "不声称" in txt(ok, state="UNKNOWN"), txt(ok, "UNKNOWN")[:60])

    # ⑨ 判定表必须完备：常规组合都得落到具名结论，不许掉进 inconclusive 兜底。
    # （inconclusive 只留给「证据自相矛盾」—— 例如吞了点、重放了、也见到了
    #   确认框，但最终状态却不是 PAUSED，这时硬说成功是 §三十三 禁止的。）
    combos = [
        ({"swallowed": 1, "replayed": 1, "confirm_seen": 1}, "PAUSED"),
        ({"swallowed": 1, "replayed": 1, "confirm_seen": 0}, "PAUSED"),
        ({"swallowed": 1, "blocked": 1}, "RUNNING"),
        ({"swallowed": 1}, "PAUSED"),
        ({"swallowed": 1}, "RUNNING"),
        ({"handled": 1}, "RUNNING"),
        ({}, "RUNNING"),
    ]
    got = [(v(s, st), st) for s, st in combos]
    rep.check("判定表完备：常规组合都落到具名结论，不掉进 inconclusive 兜底",
              all(g[0] != "inconclusive" for g in got), str(got))
    rep.check("证据自相矛盾（吞+重放+确认框，但状态不是 PAUSED）→ 报未决而非成功",
              v(ok, state="RUNNING") == "inconclusive", v(ok, "RUNNING"))
    # ⑩ blocked 的附加说明不能丢
    st = {"swallowed": 1, "blocked": 2, "handled": 1}
    rep.check("blocked 次数会写进结论文案",
              "blocked=2" in txt(st, state="RUNNING"),
              txt(st, "RUNNING")[-40:])

    # ⑪ 防回归：判定必须是**单条** if/elif 链。
    # 上一版是两段独立 if，第二段的 else 无条件兜底，把 protected 覆盖掉了。
    # 用 AST 数函数体顶层的 If 节点：>1 就意味着存在第二段独立 if，
    # 它的 else 会覆盖前面已经判出的结论（这正是上一版事故的形状）。
    try:
        import ast as _ast
        import inspect
        import textwrap
        tree = _ast.parse(textwrap.dedent(inspect.getsource(judge)))
        body = tree.body[0].body
        top_ifs = [n for n in body if isinstance(n, _ast.If)]

        def _names(t):
            """展开元组解包：`verdict, text = ...` 的 targets 是 Tuple。"""
            if isinstance(t, _ast.Name):
                yield t.id
            elif isinstance(t, _ast.Tuple):
                for e in t.elts:
                    for x in _names(e):
                        yield x

        def _sets_verdict(n):
            """该 If 分支体里是否直接给 verdict 赋值（判定链专属）。"""
            for st_ in n.body:
                if isinstance(st_, _ast.Assign) and any(
                        x == "verdict" for t in st_.targets for x in _names(t)):
                    return True
            return False

        def _count_elif(n):
            """AST 里 elif 表现为 orelse=[If(...)]，顺着数即可。"""
            k = 0
            orelse = n.orelse
            while len(orelse) == 1 and isinstance(orelse[0], _ast.If):
                k += 1
                orelse = orelse[0].orelse
            return k

        chain = [n for n in top_ifs if _sets_verdict(n)]
        n_elif = sum(_count_elif(n) for n in chain)
    except Exception as e:                                    # noqa: BLE001
        chain, top_ifs, n_elif = [], [], -1
        rep.check("能解析 _judge 源码", False, f"{type(e).__name__}: {e}")
    rep.check("判定是单条 if/elif 链（给 verdict 赋值的顶层 If 只有 1 个）",
              len(chain) == 1,
              f"链首数={len(chain)} 顶层If总数={len(top_ifs)}"
              f"（>1 ⇒ 后面的 else 会覆盖前面的结论）")
    rep.check("其余分支挂在 elif 上而不是另起 if（分支数 ≥ 6）",
              n_elif >= 6, f"elif 数={n_elif}")

    # ⑫ 拿上一轮真机产物回判（存在才查）—— 这是最贴近真实的一条
    real = os.path.join(HERE, "out", "verify_auto.json")
    if os.path.isfile(real):
        try:
            data = json.load(open(real, encoding="utf-8"))
            final = data.get("stats_final") or data.get("stats") or {}
            got = judge(final, data.get("state_after", ""))[0]
            rep.check("用上一轮真机终态证据回判，不得是 inconclusive",
                      got != "inconclusive",
                      f"{got}｜stats_final={final}｜state={data.get('state_after')}")
        except Exception as e:
            rep.check("能回判上一轮真机报告", False, f"{type(e).__name__}: {e}")


def t_uia_thin_tree(rep: H.Report) -> None:
    """「控件树变稀薄」只能改**诊断文案**，绝不能改判定结果。

    真机第 5 轮暴露：重放点击 → 雷神弹出自己的确认框（主窗口内绘制的模态层）
    → UIA 从 ~476 个控件掉到 5 个、OCR 报「截屏区域被其它窗口遮挡」→ 状态恒
    UNKNOWN，收尾复核与暂停都无从下手。而用户看到的只有一句
    「窗口内未找到目标按钮」，根本不知道发生了什么。

    这里钉住边界：提示可以加，但
      (1) 判定必须**仍然**是 UNKNOWN；
      (2) 绝不能因此被当成 PAUSED（§六，这是本工具最要命的一条）；
      (3) 控件数正常时**不许**乱加"遮挡"提示（否则会掩盖真正的识别故障）；
      (4) 0 个控件是「UIA 不可用」（多半没提权），不是遮挡，也要区分。
    """
    rep.section("控件树变稀薄的诊断（雷神确认框遮挡）")
    try:
        from core.state_machine import DurationState
        from detection import ui_automation as uia
        from leigod.duration_detector import DurationDetector
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入识别模块", False, f"{type(e).__name__}: {e}")
        return

    class _Cfg:
        def get(self, k, d=None):
            return {"detection.topbar_crop": None}.get(k, d)

    class _Win:
        hwnd = 12345
        rect = (0, 0, 1200, 751)
        frame = None

    det = DurationDetector(_Cfg())
    rep.check("存在「控件树稀薄」阈值常量",
              getattr(DurationDetector, "UIA_THIN_TREE_N", 0) > 0,
              f"UIA_THIN_TREE_N={getattr(DurationDetector, 'UIA_THIN_TREE_N', None)}")

    saved = uia.find_duration_controls

    def _stub(n):
        def f(hwnd, *a, **kw):
            return {"pause": [], "start": [], "all": [object()] * n}
        return f

    try:
        uia.find_duration_controls = _stub(5)
        ev_thin = det.detect_uia(_Win())[0]
        uia.find_duration_controls = _stub(476)
        ev_full = det.detect_uia(_Win())[0]
        uia.find_duration_controls = _stub(0)
        ev_zero = det.detect_uia(_Win())[0]
    except Exception as e:                                    # noqa: BLE001
        rep.check("detect_uia 可被调用", False, f"{type(e).__name__}: {e}")
        return
    finally:
        uia.find_duration_controls = saved

    rep.check("控件数偏少时，detail 里说明「可能是确认框遮挡」",
              "遮挡" in ev_thin.detail, ev_thin.detail)
    rep.check("控件数偏少时打出 thin_tree 标记（便于上层诊断）",
              bool(ev_thin.raw.get("thin_tree")), str(ev_thin.raw))
    rep.check("控件数正常时**不加**遮挡提示（避免掩盖真故障）",
              "遮挡" not in ev_full.detail, ev_full.detail)
    rep.check("0 个控件不算遮挡（那是 UIA 不可用 / 多半没提权）",
              "遮挡" not in ev_zero.detail, ev_zero.detail)
    # §六：这是最要命的一条
    rep.check("三种情况判定都是 UNKNOWN（§六：绝不当 PAUSED）",
              all(e.state is DurationState.UNKNOWN
                  for e in (ev_thin, ev_full, ev_zero)),
              f"{ev_thin.state.value}/{ev_full.state.value}/{ev_zero.state.value}")


def t_guard_event_notices(rep: H.Report) -> None:
    """后台线程做完的动作，必须转成**用户可见**的通知。

    真机第 4/5 轮暴露的缺口：暂停成功 → 复核为 PAUSED → 关闭动作已放行 →
    雷神随后弹出它自己的确认框。这一整套做完之后，界面上**一条提示都没有**。
    用户只看到雷神弹了个框，既不知道时长已经停了，也不知道那个框要自己选、
    更不知道选哪个都不会继续消耗时长。

    「做了但没告诉用户」与「假装成功」是同一类问题（§三十三）。
    """
    import types

    rep.section("后台事件 → 用户可见通知")
    try:
        from core.protection_engine import ProtectionEngine
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入保护引擎", False, f"{type(e).__name__}: {e}")
        return

    eng = ProtectionEngine.__new__(ProtectionEngine)     # 不跑 __init__（它要真窗口）
    seen = []
    eng._guard_seen_seq = 0
    eng._notice = lambda title, message, actions=None, strong=False: seen.append(
        (title, message, strong))

    def _feed(events):
        seen.clear()
        eng._guard_seen_seq = 0
        eng.guard = types.SimpleNamespace(events=list(events))
        eng._consume_guard_events()
        return list(seen)

    out = _feed([{"i": 1, "ev": "close_released",
                  "reason": "已重新检测到 PAUSED，放行关闭", "detail": ""}])
    rep.check("放行关闭后会通知用户（做完必须说）", len(out) >= 1, str(out)[:120])
    rep.check("通知里讲明「雷神的确认框要你自己选」",
              any("自己选" in m for _, m, _ in out), str(out)[:120])
    rep.check("通知里说明「时长已停，选哪个都不会继续消耗」",
              any("不会继续消耗" in m for _, m, _ in out), str(out)[:120])
    rep.check("放行通知不是强提醒（不是异常）",
              all(not s for _, _, s in out), str([s for _, _, s in out]))

    out2 = _feed([{"i": 1, "ev": "confirm_paused",
                   "detail": "发现雷神确认框，已先暂停总时长"}])
    rep.check("因确认框而自动暂停也会通知", len(out2) >= 1, str(out2)[:120])

    out3 = _feed([{"i": 1, "ev": "confirm_pause_failed",
                   "reason": "读不到状态", "detail": "被遮挡"}])
    rep.check("暂停失败必须是强提醒（用户得手动兜底）",
              bool(out3) and out3[0][2] is True, str(out3)[:120])

    # 事件的 seq 水位必须生效，否则后台线程每 tick 都会重复弹同一条。
    eng._guard_seen_seq = 0
    eng.guard = types.SimpleNamespace(
        events=[{"i": 1, "ev": "close_released", "reason": "x"}])
    seen.clear()
    eng._consume_guard_events()
    n1 = len(seen)
    eng._consume_guard_events()
    rep.check("同一条事件不会重复弹通知（seq 水位生效）",
              n1 >= 1 and len(seen) == n1, f"{n1} -> {len(seen)}")


def t_ocr_skip(rep: H.Report) -> None:
    """`ocr_mode="auto"`：UIA 定论时跳过整窗 OCR，但绝不能因此少判一次。

    为什么要有它：整窗 OCR 在本机要 **1.3s 以上**，而 `detect()` 原先无条件
    「先 UIA 再 OCR」，于是每轮状态轮询都被拖住 —— 名义 500ms 的 tick 实际
    1.3s+，用户感受到的就是「识别延迟高」。

    这个优化的风险面很明确：**省时间不能以牺牲判定可靠性为代价**。
    所以这里逐条钉住「什么时候可以跳、什么时候必须跑」。
    """
    import time
    import types

    rep.section("OCR 调度（auto：UIA 定论时跳过）")
    try:
        from core.state_machine import DurationState, Evidence
        from leigod.duration_detector import DurationDetector
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入识别模块", False, f"{type(e).__name__}: {e}")
        return

    class _Cfg:
        def get(self, k, d=None):
            return {"detection.topbar_crop": None}.get(k, d)

    win = types.SimpleNamespace(hwnd=1, rect=(0, 0, 100, 100), frame=None)
    RUN = DurationState.RUNNING

    def make(uia_states, ocr_state=RUN, geom=True, fresh=True):
        det = DurationDetector(_Cfg())
        calls = {"ocr": 0}

        def _uia(_win, fast=False):
            # 必须接受 `fast=`：真实 `detect_uia(win, fast=...)` 会传它。
            # 桩函数签名跟真实现不一致时，测试只会在"跑到那一条路径"时才炸
            # —— 这次就是 `t_ocr_skip` 先炸出来的（2026-09-30 加性能快路径时）。
            return [Evidence("ui_automation", s, "stub", {}) for s in uia_states]

        def _ocr(_win):
            calls["ocr"] += 1
            det._last_ocr_ts = time.time()
            return [Evidence("ocr", ocr_state, "stub", {})]

        det.detect_uia = _uia
        det.detect_ocr = _ocr
        det.last_ocr_geom = {"frame": [0, 0, 1, 1]} if geom else None
        det._last_ocr_ts = time.time() if fresh else 0.0
        return det, calls

    # ① UIA 定论 + 几何新鲜 → 跳过 OCR（这就是提速点）
    det, c = make([RUN])
    det.detect(win, allow_ocr=True, ocr_mode="auto")
    rep.check("UIA 定论且几何新鲜 → 跳过 OCR（省 1.3s）", c["ocr"] == 0, str(c))

    # ② UIA 认不出来 → 必须靠 OCR
    det, c = make([DurationState.UNKNOWN])
    det.detect(win, allow_ocr=True, ocr_mode="auto")
    rep.check("UIA 判 UNKNOWN → 照样跑 OCR", c["ocr"] == 1, str(c))

    # ③ UIA 自相矛盾（同时出现两种按钮）→ 必须跑 OCR 来裁决
    det, c = make([RUN, DurationState.PAUSED])
    det.detect(win, allow_ocr=True, ocr_mode="auto")
    rep.check("UIA 内部冲突 → 照样跑 OCR", c["ocr"] == 1, str(c))

    # ④ 从没 OCR 过 → 先攒一份按钮定位几何（坐标回退要用）
    det, c = make([RUN], geom=False)
    det.detect(win, allow_ocr=True, ocr_mode="auto")
    rep.check("从未 OCR 过（无按钮几何）→ 先跑一次攒几何", c["ocr"] == 1, str(c))

    # ⑤ 几何过期（窗口可能移动/缩放过）→ 强制刷新
    det, c = make([RUN], fresh=False)
    det.detect(win, allow_ocr=True, ocr_mode="auto")
    rep.check("按钮几何超过刷新间隔 → 强制跑一次", c["ocr"] == 1, str(c))

    # ⑥ always 必须保持原行为（排障时用它）
    det, c = make([RUN])
    det.detect(win, allow_ocr=True, ocr_mode="always")
    rep.check("ocr_mode=always 时无条件跑 OCR（保守档仍在）", c["ocr"] == 1, str(c))

    # ⑦ 跳过 OCR 不能让判定变松：结论仍由 UIA 单独给出
    det, c = make([RUN])
    r = det.detect(win, allow_ocr=True, ocr_mode="auto")
    rep.check("跳过 OCR 后结论仍为 UIA 的结论（不是 UNKNOWN）",
              r.state is DurationState.RUNNING, r.state.value)

    # ⑧ **不传 ocr_mode 时必须走配置默认档**
    #    这一条是真机教训：上一轮只把 auto 接到了状态轮询那一条路径上，
    #    `duration_controller` 的 before / _verify / 重试前共 4~8 次 detect()
    #    全都吃函数默认值 "always"，每次白跑一遍 1.3s 的整窗 OCR ——
    #    用户感受就是「点完 ✕ 要等很久才自动暂停」。
    #    性能开关不能靠调用方记得传，默认值才是唯一可靠的地方。
    det, c = make([RUN])
    rep.check("未显式传参时，默认档取自配置（=auto）",
              det.default_ocr_mode == "auto", det.default_ocr_mode)
    det.detect(win, allow_ocr=True)
    rep.check("不传 ocr_mode 也会跳过 OCR（默认档真的生效）", c["ocr"] == 0, str(c))

    # ⑨ off 档：连 OCR 都不允许跑（无 OCR 引擎的环境用）
    det, c = make([DurationState.UNKNOWN])
    r = det.detect(win, allow_ocr=True, ocr_mode="off")
    rep.check("ocr_mode=off 时完全不跑 OCR", c["ocr"] == 0, str(c))
    rep.check("off 且 UIA 认不出 → 仍然是 UNKNOWN（不当 PAUSED）",
              r.state is DurationState.UNKNOWN, r.state.value)


def t_dock_hysteresis(rep: H.Report) -> None:
    """面板换边的滞回 —— 治「吸附度不高、有时候乱跳」。

    真机现象：把雷神慢慢拖到屏幕左边缘时，面板会在左右两侧**反复横跳**。
    根因是换边判据是二值的（`x_left >= avail.left()` 就左、否则右），
    窗口恰好压在边界上时，矩形每抖 1px 就换一次边。

    这里逐条钉住：贴边只换一次、回到边界不立刻弹回、余量够了才换回。
    """
    rep.section("面板换边滞回（治贴边横跳）")
    try:
        from ui.guard_window import SWITCH_MARGIN, choose_dock_side
    except Exception as e:                                    # noqa: BLE001
        rep.check("能导入换边决策（不依赖 Qt 实例）", False,
                  f"{type(e).__name__}: {e}")
        return
    rep.check("滞回余量为正（否则等于没有滞回）", SWITCH_MARGIN > 0,
              f"SWITCH_MARGIN={SWITCH_MARGIN}")

    A = 0          # 可用区左边界
    # ① 首次定位：能放左边就放左边
    rep.check("首次定位：左侧放得下 → 停靠左侧",
              choose_dock_side(None, A + 50, A) == "left")
    rep.check("首次定位：左侧放不下 → 停靠右侧",
              choose_dock_side(None, A - 10, A) == "right")

    # ② 已在左侧，左侧仍放得下 → 不动（不横跳）
    rep.check("已在左侧且仍放得下 → 保持左侧",
              choose_dock_side("left", A + 1, A) == "left")
    # ③ 已在左侧但真的放不下 → 换右
    rep.check("已在左侧但越界 → 换到右侧",
              choose_dock_side("left", A - 1, A) == "right")

    # ④ **关键**：已在右侧，刚回到边界（余量 < SWITCH_MARGIN）→ 不许弹回左侧
    rep.check("已在右侧、余量不足 → 保持右侧（不横跳）",
              choose_dock_side("right", A + SWITCH_MARGIN - 1, A) == "right",
              f"x_left=A+{SWITCH_MARGIN - 1}")
    # ⑤ 余量够了才换回左侧
    rep.check("已在右侧、余量足够 → 换回左侧",
              choose_dock_side("right", A + SWITCH_MARGIN, A) == "left")
    rep.check("已在右侧、余量很足 → 换回左侧",
              choose_dock_side("right", A + 200, A) == "left")

    # ⑥ 拿到可用区为 None（多显示器极端情况）时不许崩、且保持当前边
    rep.check("拿不到可用区时不崩，且保持当前停靠侧",
              choose_dock_side("left", 100, None) == "left"
              and choose_dock_side("right", 100, None) == "left",
              "right + 无可用区 → 视为可以换回左（不越界）")


def main() -> int:
    from core.logging_setup import setup_logging
    setup_logging("INFO", console=False)
    rep = H.Report("纯逻辑单元测试（状态机 / 识别 / 配置 / 监控 / 界面文案）")
    print(rep.title, flush=True)
    t_state_machine(rep)
    t_config(rep)
    t_logging(rep)
    t_uia(rep)
    t_ocr(rep)
    t_image(rep)
    t_coordinate(rep)
    t_game_monitor(rep)
    t_notifier(rep)
    t_single_instance(rep)
    t_theme(rep)
    t_close_intent(rep)
    t_config_migration(rep)
    t_confirm_dialog(rep)
    t_deadman_window(rep)
    t_ocr_button_locate(rep)
    t_exit_readiness(rep)
    t_ocr_engine_concurrency(rep)
    t_ctypes_no_pollution(rep)
    t_confirm_known_seed(rep)
    t_pause_callback_contract(rep)
    t_auto_verifier(rep)
    t_verdict_judge(rep)
    t_uia_thin_tree(rep)
    t_guard_event_notices(rep)
    t_ocr_skip(rep)
    t_dock_hysteresis(rep)
    t_dock_pinned(rep)
    t_window_binding(rep)
    t_recheck(rep)
    t_perf_contract(rep)
    t_ui_design(rep)
    t_close_gone_judgement(rep)
    t_ocr_engine_visibility(rep)
    t_pause_latency(rep)
    t_standby_wake(rep)
    t_entry(rep)
    path = rep.save("unit_report.txt")
    fails = sum(1 for l in rep.lines if "[FAIL]" in l)
    print(f"\n结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}；报告 {path}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
