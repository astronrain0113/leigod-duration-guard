"""雷神加速器总时长保护工具 —— 程序入口。

用法（常用）：
  python main.py                 正常启动（伴生面板 + 托盘 + 关闭保护）
  python main.py --launcher      先拉起雷神、等主窗口就绪，再启动保护
  python main.py --minimized     只驻留托盘，不显示面板
  python main.py --no-ui         无界面模式（控制台看日志，便于排障）
  python main.py --check         环境自检（窗口 / 权限 / 配置），然后退出
  python main.py --inspector     运行 Leigod UI Inspector（控件树 + 截图 + OCR）
  python main.py --calibrate     运行坐标校准（会真的点一次「暂停时长」并验证）
  python main.py --window-inspector  只做窗口层诊断

设计纪律：本文件只做「解析参数 + 按顺序装配」，任何业务判断都不在这里。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

__version__ = "1.0.0"
#: 构建标记。每次交付递增，启动时**第一行日志**就会打出来。
#:
#: 为什么值得单列一个常量：本项目吃过「不知道用户跑的是哪一版」的亏 ——
#: 用户报「待命没生效」，而当时到底是没触发、还是跑的是修复前的旧 exe，
#: 只能靠翻文件时间戳去猜。有了它，`logs/guard.log` 的第一行就是答案。
__build__ = "2026-10-01.6"


# ---------------------------------------------------------------- 无界面模式
class ConsoleSink:
    """无界面模式的输出：把通知直接打印出来（引擎自己的日志照常走 logger）。"""

    def on_event(self, event): pass

    def on_status(self, status): pass

    def on_pause(self, outcome):
        print(f"[暂停] {outcome.line()}", flush=True)

    def on_notice(self, notice):
        print(f"\n*** {notice.title}\n{notice.message}\n", flush=True)


def run_headless(cfg, logger, max_seconds: float = 0.0) -> int:
    from core.protection_engine import ProtectionEngine
    engine = ProtectionEngine(cfg, sink=ConsoleSink(), logger=logger)
    engine.start()
    deadline = (time.time() + max_seconds) if max_seconds > 0 else None
    logger.info("已进入无界面模式（Ctrl+C 退出%s）",
                f"，{max_seconds:.0f}s 后自动退出" if deadline else "")
    try:
        while True:
            time.sleep(0.5)
            if engine.quit_requested:
                logger.info("跟随模式：雷神已退出，结束")
                break
            if deadline and time.time() >= deadline:
                logger.info("到达 --headless-ms 指定时长，退出")
                break
    except KeyboardInterrupt:
        logger.info("收到中断信号，退出中……")
    finally:
        engine.stop()
    return 0


# ---------------------------------------------------------------- 环境自检
def run_check(cfg, logger) -> int:
    from diagnostics import window_inspector as wi
    from launcher import launcher

    data = wi.inspect(cfg)
    print(wi.render(data), flush=True)
    priv = launcher.privilege_report(cfg)
    print("\n== 权限自检 ==", flush=True)
    print(f"  本程序管理员身份 : {'是' if priv['self_elevated'] else '否'}", flush=True)
    print(f"  雷神运行中       : {'是' if priv['leigod_running'] else '否'}", flush=True)
    print(f"  雷神疑似管理员运行: {priv['leigod_elevated']}", flush=True)
    print(f"  结论             : {'通过' if priv['ok'] else '需要以管理员身份运行'}", flush=True)
    if priv["message"]:
        print(f"  提示             : {priv['message']}", flush=True)

    from core.config import LAST_ERROR
    print("\n== 配置 ==", flush=True)
    print(f"  文件             : {cfg.path}", flush=True)
    print(f"  解析错误         : {LAST_ERROR or '无'}", flush=True)
    print(f"  已校准暂停坐标   : {cfg.get('duration.coordinate.ratio') or '未校准'}", flush=True)
    from detection import ui_automation as uia_mod
    ok_uia, why_uia = uia_mod.probe()
    print(f"  UI Automation    : {'可用' if ok_uia else '不可用'} — {why_uia}", flush=True)
    from detection import ocr as ocr_mod
    ok_ocr = ocr_mod.engine_available()
    if ok_ocr:
        print("  OCR 引擎         : 可用", flush=True)
    else:
        # 必须把**真实原因**打出来。只说"未安装依赖"会把「模型没打进包」
        # 这种打包事故误报成用户环境问题 —— 真机踩过（2026-09-30）。
        print(f"  OCR 引擎         : 不可用 — {ocr_mod.last_error() or '未安装 rapidocr-onnxruntime'}", flush=True)
        print("                     后果：一旦 UI Automation 也读不到控件，"
              "时长状态将无法识别（会一直显示「无法确认」）", flush=True)
    from core import paths as paths_mod
    print(f"  程序运行形态     : {'打包 exe' if paths_mod.is_frozen() else '源码'}"
          f"（配置与日志基准目录：{paths_mod.base_dir()}）", flush=True)
    print(f"  配置目录可写     : {paths_mod.ensure_writable_hint(cfg.path)}", flush=True)
    return 0 if data["window"] else 2


# ---------------------------------------------------------------- 参数
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=f"雷神加速器总时长保护工具 v{__version__}"
                    "（窗口绑定 + 关闭保护，不碰任何账号接口）")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", default=None, help="指定 config.json 路径")
    ap.add_argument("--log-level", default=None,
                    help="日志级别 DEBUG/INFO/WARNING（默认取配置 general.log_level）")
    ap.add_argument("--launcher", action="store_true",
                    help="启动器模式：先启动雷神并等待主窗口，再启动保护")
    ap.add_argument("--wait", type=float, default=None,
                    help="启动器等待主窗口的秒数（默认 90）")
    ap.add_argument("--minimized", action="store_true", help="启动后只驻留托盘")
    ap.add_argument("--watcher", "--standby", dest="watcher", action="store_true",
                    help="待命守望：只驻留托盘并盯着雷神，雷神一出现就把本工具唤起")
    ap.add_argument("--no-ui", action="store_true", help="无界面模式（控制台日志）")
    ap.add_argument("--console", action="store_true", help="图形模式下也把日志打到控制台")
    ap.add_argument("--allow-multi", action="store_true", help="允许同时运行多个实例（调试用）")
    ap.add_argument("--check", action="store_true", help="环境自检后退出")
    ap.add_argument("--headless-ms", type=int, default=0,
                    help="调试用：无界面模式下运行指定毫秒后自动退出")

    g = ap.add_argument_group("诊断工具")
    g.add_argument("--inspector", action="store_true", help="运行 Leigod UI Inspector")
    g.add_argument("--window-inspector", action="store_true", help="只做窗口层诊断")
    g.add_argument("--calibrate", action="store_true", help="运行坐标校准")
    g.add_argument("--out", default=None, help="诊断产物目录")
    g.add_argument("--mode", default="auto", choices=["auto", "manual", "point"],
                   help="校准模式（配合 --calibrate）")
    g.add_argument("--point", default=None, help="校准用的屏幕坐标 X,Y（配合 --mode point）")
    g.add_argument("--delay", type=float, default=5.0, help="手动校准倒计时秒数")
    g.add_argument("--restore", action="store_true", help="校准成功后恢复计时")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # ---------- 诊断类入口：不启动保护，直接交给对应工具 ----------
    if args.window_inspector:
        from diagnostics import window_inspector as wi
        av = []
        if args.out:
            av += ["--save", os.path.join(args.out, "window_inspector.json")]
        return wi.main(av)

    if args.inspector:
        from diagnostics import ui_inspector
        from core.logging_setup import setup_logging
        setup_logging("INFO", console=False)
        av = ["--out", args.out] if args.out else []
        return ui_inspector.main(av)

    if args.calibrate:
        from diagnostics import calibration
        from core.logging_setup import setup_logging
        setup_logging("INFO", console=False)
        av = ["--mode", args.mode, "--delay", str(args.delay)]
        if args.point:
            av += ["--point", args.point]
        if args.out:
            av += ["--out", args.out]
        if args.restore:
            av += ["--restore"]
        return calibration.main(av)

    # ---------- 正常启动 ----------
    from core.config import LAST_ERROR, load_config
    from core.logging_setup import setup_logging

    cfg = load_config(args.config) if args.config else load_config()
    level = args.log_level or cfg.get("general.log_level", "INFO")
    logger = setup_logging(level, console=bool(args.no_ui or args.console))

    # 启动首行就把「哪一版、什么形态、什么参数」写清楚。排障时不必再猜。
    logger.info("启动 v%s（build %s）｜形态=%s｜参数=%s", __version__, __build__,
                "打包 exe" if getattr(sys, "frozen", False) else "源码",
                " ".join(argv if argv is not None else sys.argv[1:]) or "（无）")

    if LAST_ERROR:
        logger.error("配置有问题：%s", LAST_ERROR)
        if not args.no_ui:
            print(f"\n配置解析失败：{LAST_ERROR}\n已按默认值继续运行。", flush=True)

    if args.check:
        return run_check(cfg, logger)

    # ---------- 待命守望 ----------
    # 必须在守卫的单实例检查**之前**处理：守望自己不持有守卫的互斥体，
    # 否则它探测「守卫在不在」时永远会看到自己，守卫就再也起不来了。
    if args.watcher:
        from launcher.watch import watcher_single_instance
        from ui.standby import StandbyApp
        wsi = watcher_single_instance()
        if not args.allow_multi and not wsi.acquire():
            msg = "已有一个待命守望在运行，本次启动已退出。"
            logger.warning(msg)
            print(msg, flush=True)
            return 0
        try:
            logger.info("待命守望模式：不启动保护，只盯着雷神")
            return StandbyApp(cfg, logger=logger).run()
        finally:
            wsi.release()

    # ---------- 单实例 ----------
    from launcher.launcher import SingleInstance, ensure_leigod
    si = SingleInstance("LeigodDurationGuard")
    if not args.allow_multi:
        if not si.acquire():
            msg = "已有「雷神总时长保护」在运行，本次启动已退出。"
            logger.warning(msg)
            print(msg, flush=True)
            return 0
    else:
        logger.warning("已跳过单实例检查（--allow-multi）")

    try:
        if args.launcher:
            logger.info("启动器模式：准备雷神")
            res = ensure_leigod(cfg, on_step=lambda s: logger.info("启动器：%s", s),
                               logger=logger, timeout=args.wait)
            if res["window"] is None:
                logger.error("启动器失败：%s", res["message"])
                print(f"\n{res['message']}", flush=True)
                return 5

        if args.no_ui:
            return run_headless(cfg, logger, max_seconds=args.headless_ms / 1000.0)

        from ui.app import GuardApp
        app = GuardApp(cfg, logger, start_minimized=args.minimized)
        return app.run()
    except Exception as e:                 # 最后一道兜底：别把界面崩掉还不留日志
        logger.exception("启动失败: %s", e)
        print(f"\n启动失败：{e}\n详见日志 logs/guard.log", flush=True)
        return 1
    finally:
        si.release()


if __name__ == "__main__":
    raise SystemExit(main())
