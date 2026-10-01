"""把雷神窗口置前并读取当前「总时长」三态（供**提权**运行）。

为什么必须提权：把**提权窗口**置前（SetForegroundWindow/AttachThreadInput）
在不提权时会被 UIPI 拒绝；而截屏护栏要求雷神是前台窗口
（`detection.require_foreground_for_screen_grab`），所以不提权就拿不到截图、
OCR 自然也无从谈起 —— 这会让人误以为「识别不可靠」。

流程：找主窗口 → 还原到屏幕内 → 激活置前 → 复核 → 跑 UI Inspector。

用法：
    python tests/run_elevated.py --wait real_state/inspector.json -- \
        tests/get_real_state.py --out real_state
"""
import argparse
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

user32 = ctypes.windll.user32


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="real_state", help="诊断产物目录（相对项目根）")
    args = ap.parse_args()

    out_dir = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(out_dir, exist_ok=True)

    from core.config import load_config
    from detection import coordinate_fallback as cf
    from leigod import window as win_mod
    from diagnostics import ui_inspector

    cfg = load_config()
    win = win_mod.find_main_window(cfg)
    if not win:
        print("未找到雷神主窗口（雷神没在运行？）")
        return 2

    print(f"主窗口 HWND=0x{win.hwnd:X} rect={win.rect} size={win.size} dpi={win.dpi}")
    print(f"托盘最小化={win.minimized_to_tray}")

    rect = cf.ensure_visible(win.hwnd)          # 从 -25600 托盘态拉回屏幕
    print(f"ensure_visible -> rect={rect}")
    ok = cf.activate_window(win.hwnd)           # 抢前台
    fg = int(user32.GetForegroundWindow() or 0)
    print(f"activate_window -> {ok}；前台窗口=0x{fg:X}"
          f"{'（已是雷神 ✓）' if fg == win.hwnd else '（仍不是雷神 ✗）'}")

    win = win_mod.refresh(win)
    rep = ui_inspector.run(out_dir, config=cfg, verbose=True)
    state = (rep.get("result") or {}).get("state")
    print(f"\n>>> 当前总时长状态 = {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
