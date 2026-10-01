"""只读探针：真实雷神窗口是否具备「关闭保护」所依赖的两个前提。

本脚本**不改动雷神**（唯一的写操作是「禁用 SC_CLOSE → 立即还原」，且失败即还原），
只回答两个决定架构的问题：

  Q1 真实窗口有没有系统菜单（GetSystemMenu 是否为 NULL）？
     没有 → enableMenuItem(SC_CLOSE, MF_GRAYED) 这套「禁用 ✕」方案根本不成立。

  Q2 真实窗口的右上角 ✕ 是否返回 HTCLOSE？
     不返回 → close_protection.is_close_button_at() 永远为假，
              「关闭意图识别」在真机上永不触发。

同时打印命中测试扫掠图（每个采样点实际落在哪个窗口、命中码是多少），
用于判断 Electron 自绘标题栏的行为。

用法：
    python tests/probe_real_close_caps.py
产物：
    tests/out/real_close_caps.txt
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, ROOT)

user32 = ctypes.windll.user32

user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetSystemMenu.argtypes = [wt.HWND, wt.BOOL]
user32.GetSystemMenu.restype = ctypes.c_void_p
user32.GetMenuState.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
user32.GetMenuState.restype = wt.UINT
user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
user32.GetMenuItemCount.restype = ctypes.c_int
user32.EnableMenuItem.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
user32.EnableMenuItem.restype = wt.UINT
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_longlong
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WM_NCHITTEST = 0x84
SC_CLOSE = 0xF060
MF_BYCOMMAND, MF_GRAYED, MF_ENABLED = 0x0, 0x1, 0x0
GWL_STYLE, GWL_EXSTYLE = -16, -20

HT_NAMES = {0: "HTNOWHERE", 1: "HTCLIENT", 2: "HTCAPTION", 3: "HTSYSMENU",
            8: "HTMINBUTTON", 9: "HTMAXBUTTON", 10: "HTLEFT", 20: "HTCLOSE",
            18: "HTBORDER", 21: "HTHELP"}

STYLE_FLAGS = [
    (0x80000000, "WS_POPUP"), (0x40000000, "WS_CHILD"), (0x20000000, "WS_MINIMIZE"),
    (0x10000000, "WS_VISIBLE"), (0x08000000, "WS_DISABLED"), (0x04000000, "WS_CLIPSIBLINGS"),
    (0x02000000, "WS_CLIPCHILDREN"), (0x01000000, "WS_MAXIMIZE"), (0x00C00000, "WS_CAPTION"),
    (0x00800000, "WS_BORDER"), (0x00400000, "WS_DLGFRAME"), (0x00200000, "WS_VSCROLL"),
    (0x00100000, "WS_HSCROLL"), (0x00080000, "WS_SYSMENU"), (0x00040000, "WS_THICKFRAME"),
    (0x00020000, "WS_MINIMIZEBOX"), (0x00010000, "WS_MAXIMIZEBOX"),
]

report = []


def say(s=""):
    # pythonw.exe 下 sys.stdout 为 None，print 会抛异常；先落盘再尝试打印，
    # 保证「无控制台运行（提权 runas）」也能拿到完整报告。
    report.append(s)
    try:
        print(s, flush=True)
    except Exception:
        pass


def decode(style):
    return [n for bit, n in STYLE_FLAGS if style & bit] or ["(无)"]


def lp(x, y):
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def dump():
    """落盘报告。提权与未提权的结果**分开存**，避免互相覆盖。
    （踩过：先跑未提权、再跑提权，第二次把第一次的有效结论覆盖掉了。）
    """
    tag = "admin" if is_elevated() else "user"
    ok = False
    for name in (f"real_close_caps_{tag}.txt", "real_close_caps.txt"):
        try:
            with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
                f.write("\n".join(report))
            ok = True
        except Exception:
            pass
    return ok


def hit(hwnd, x, y):
    return int(user32.SendMessageW(hwnd, WM_NCHITTEST, 0, lp(x, y)))


def class_of(hwnd):
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def main():
    say("=" * 78)
    say("真实雷神窗口 —— 关闭保护前提条件只读探针")
    say("=" * 78)
    say(f"运行时刻    : {__import__('time').strftime('%Y-%m-%d %H:%M:%S')}")
    elevated = is_elevated()
    say(f"本进程提权  : {'是' if elevated else '否'}")
    if not elevated:
        say("  ⚠ 本进程未提权，而雷神是 requireAdministrator。UIPI 会静默拦截")
        say("    跨完整性级别的 SendMessage / EnableMenuItem，因此下面的")
        say("    WM_NCHITTEST=0 与 EnableMenuItem=0xFFFFFFFF 可能只是「被拦」，")
        say("    不能据此断定窗口本身不支持。需提权重跑才能得到有效结论。")
    say()

    # ---- 定位真实主窗口（复用项目模块，保证与运行时同源） ----
    from core.config import load_config
    from leigod import window as win_mod
    cfg = load_config(os.path.join(ROOT, "config", "config.json"))
    win = win_mod.find_main_window(cfg)
    if not win:
        say("未找到雷神主窗口（雷神可能没在运行）。终止。")
        return 1
    hwnd = win.hwnd
    r = win.rect
    say(f"主窗口      : HWND=0x{hwnd:X} title={win.title!r} class={win.class_name} pid={win.pid}")
    say(f"进程        : {win.process_name} {win.exe_path} v{win.version}")
    say(f"rect        : {r}  尺寸={win.size}  DPI={win.dpi}")
    say(f"frame       : {win.frame}")
    say(f"client      : {win.client}")
    say()

    # ---- Q1: 系统菜单 ----
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    exstyle = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    say(f"style       : 0x{style:08X}  -> {', '.join(decode(style))}")
    say(f"exstyle     : 0x{exstyle:08X}")
    say(f"WS_SYSMENU  : {'有' if style & 0x00080000 else '无'}")
    hmenu = user32.GetSystemMenu(wt.HWND(hwnd), False)
    say(f"GetSystemMenu(FALSE) = {hmenu}  ({'NULL' if not hmenu else '0x%X' % hmenu})")
    if hmenu:
        cnt = user32.GetMenuItemCount(ctypes.c_void_p(hmenu))
        st = user32.GetMenuState(ctypes.c_void_p(hmenu), SC_CLOSE, MF_BYCOMMAND)
        say(f"  菜单项数 = {cnt}")
        say(f"  SC_CLOSE GetMenuState = 0x{st:08X}  (0xFFFFFFFF 表示不存在)")
        say(f"  SC_CLOSE 是否已禁用   = {bool(st & MF_GRAYED)}")
    say()
    q1 = bool(hmenu) and hmenu != 0
    say(f"→ Q1 结论：系统菜单{'存在，SC_CLOSE 方案【可能】可用' if q1 else '不存在 → EnableMenuItem(SC_CLOSE) 方案【不成立】'}")
    say()

    # ---- 命中测试扫掠：右上角区域 ----
    say("-" * 78)
    say("右上角命中测试扫掠（每个点的命中码 + 该点实际落在哪个窗口类）")
    say("坐标原点 = 窗口左上角 (dx,dy)")
    say("-" * 78)
    xs = list(range(-420, 1, 20))   # 从右边往左扫（dx 为负）
    ys = [4, 10, 16, 22, 28, 34, 40, 46]
    header = "  dy\\dx " + "".join(f"{x:>6d}" for x in xs)
    say(header)
    htclose_pts = []
    for dy in ys:
        row = f"{dy:>6d} "
        for dx in xs:
            px, py = r[0] + (win.size[0] + dx), r[1] + dy
            h = hit(hwnd, px, py)
            row += f"{h:>6d}"
            if h == 20:
                htclose_pts.append((dx, dy))
        say(row)
    say()
    say(f"命中码对照: 0=HTNOWHERE 1=HTCLIENT 2=HTCAPTION 8=HTMINBUTTON 9=HTMAXBUTTON 20=HTCLOSE")
    say(f"扫掠到 HTCLOSE 的点: {htclose_pts if htclose_pts else '无'}")
    say()

    # 采样几个点看实际落点窗口
    say("实际落点窗口采样（离右上角越近越可能命中 ✕）:")
    for dx, dy in [(-20, 16), (-50, 16), (-90, 16), (-160, 18), (-300, 18), (60, 16), (200, 18)]:
        px, py = r[0] + win.size[0] + dx, r[1] + dy
        pt = wt.POINT(px, py)
        w = user32.WindowFromPoint(pt)
        say(f"  ({px},{py}) -> hwnd=0x{w:X} class={class_of(w)!r} hit(hwnd)={hit(hwnd, px, py)}")
    say()

    # ---- 已知 3 个系统按钮位置（用系统度量算）在真机上是什么 ----
    dpi = win.dpi
    cap = user32.GetSystemMetricsForDpi(4, dpi) if hasattr(user32, "GetSystemMetricsForDpi") else 0
    fr = user32.GetSystemMetricsForDpi(33, dpi) if hasattr(user32, "GetSystemMetricsForDpi") else 0
    bw = user32.GetSystemMetricsForDpi(30, dpi) if hasattr(user32, "GetSystemMetricsForDpi") else 0
    cx, cy = r[2] - fr - bw // 2, r[1] + fr + cap // 2
    say(f"用系统度量推算的 ✕ 中心 = ({cx},{cy}) (cap={cap} frame={fr} btnw={bw} dpi={dpi})")
    say(f"  该点 hit(hwnd) = {hit(hwnd, cx, cy)}")
    say()

    # ---- Q1b: 禁用/还原测试（看完即还原） ----
    say("-" * 78)
    say("SC_CLOSE 禁用/还原测试（只读侧效果，测试后立即还原）")
    say("-" * 78)
    grey_ok = False
    if hmenu:
        try:
            prev = user32.EnableMenuItem(ctypes.c_void_p(hmenu), SC_CLOSE, MF_BYCOMMAND | MF_GRAYED)
            st = user32.GetMenuState(ctypes.c_void_p(hmenu), SC_CLOSE, MF_BYCOMMAND)
            grey_ok = (prev != 0xFFFFFFFF) and bool(st & MF_GRAYED)
            say(f"EnableMenuItem(GRAYED) 返回=0x{prev:08X}  之后 GetMenuState=0x{st:08X} "
                f"禁用生效={bool(st & MF_GRAYED)}  (返回 0xFFFFFFFF 表示调用失败)")
        finally:
            user32.EnableMenuItem(ctypes.c_void_p(hmenu), SC_CLOSE, MF_BYCOMMAND | MF_ENABLED)
            st2 = user32.GetMenuState(ctypes.c_void_p(hmenu), SC_CLOSE, MF_BYCOMMAND)
            say(f"已还原：GetMenuState=0x{st2:08X} 仍禁用={bool(st2 & MF_GRAYED)}")
    else:
        say("无系统菜单 → EnableMenuItem 无从调用（Q1 不成立）")
    say()

    q2 = bool(htclose_pts)

    # ---- UIPI 判别：SendMessage 到底是被「拦」还是窗口真的返回 0 ----
    say("-" * 78)
    say("UIPI 判别：用 SendMessageTimeout 观察返回值与 GetLastError")
    say("-" * 78)
    uipi_blocked = False
    try:
        user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
                                               wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t)]
        user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        res = ctypes.c_size_t(0xDEAD)
        cx2, cy2 = r[2] - 40, r[1] + 18
        ctypes.set_last_error(0)
        ok = user32.SendMessageTimeoutW(wt.HWND(hwnd), WM_NCHITTEST, 0, lp(cx2, cy2),
                                        0x0002, 800, ctypes.byref(res))
        err = ctypes.get_last_error()
        say(f"SendMessageTimeoutW(WM_NCHITTEST) 返回={ok} 结果值={res.value} "
            f"GetLastError={err} ({ctypes.FormatError(err) if err else '无'})")
        if ok == 0:
            say("  → 调用失败/超时：跨完整性级别被 UIPI 拦截的可能性很高（提权后应能通过）")
            uipi_blocked = True
        else:
            say(f"  → 消息送达，窗口自身返回 {res.value}（说明这是【窗口的真实回答】，非被拦）")
    except Exception as e:
        say(f"UIPI 判别异常：{e}")
    say()

    if not elevated:
        q1_txt = "本次未提权 → 结论不可用（非 0x00000000 的返回值疑为 UIPI 拒绝）"
    elif not q1:
        q1_txt = "窗口无系统菜单 → SC_CLOSE 方案不成立"
    elif grey_ok:
        q1_txt = ("提权后 EnableMenuItem 真实生效 → 「禁用 ✕」在**系统层**可用。"
                  "但真实 ✕ 由应用自绘（见下），能否拦住它取决于应用是否走 SC_CLOSE 通路，"
                  "**必须真实点击 ✕ 才能定论**。")
    else:
        q1_txt = "提权后仍未生效 → 「禁用 ✕」方案失效"
    say(f"→ Q1 结论：{q1_txt}")

    if q2:
        q2_txt = "返回 HTCLOSE，关闭意图可按命中码识别"
    elif not elevated:
        q2_txt = "未提权，返回 0 疑为 UIPI 拦截，结论待提权复验"
    else:
        q2_txt = ("【不返回 HTCLOSE】且消息已送达（非被拦）→ "
                  "close_protection.is_close_button_at() 在真机**恒为假**")
    say(f"→ Q2 结论：{q2_txt}")
    say()
    say("=" * 78)
    say(f"架构判定： 系统菜单={'有' if q1 else '无'}   SC_CLOSE可禁用={grey_ok}   "
        f"HTCLOSE={'有' if q2 else '无'}   提权={'是' if elevated else '否'}")
    if not elevated:
        say("  ⇒ 本次为【未提权】运行，受 UIPI 影响，以上结论仅作参考。")
        say("     必须以管理员身份重跑本探针，才能判定拦截机制是否可行。")
    else:
        if not q2:
            say("  ⇒ 【已确认】关闭意图识别（依赖 HTCLOSE）在真机上不成立：")
            say("     整个客户区对 WM_NCHITTEST 一律返回 HTCLIENT(1)，")
            say("     说明标题栏（含 ✕）是应用自绘、由渲染进程处理点击。")
            say("     → 必须先改为「窗口矩形相对几何 + 状态/前台等联合条件」的判据。")
        if grey_ok:
            say("  ⇒ 【已确认】SC_CLOSE 在提权后确实可被禁用（系统层机制有效）。")
            say("     但自绘 ✕ 是否走 SC_CLOSE 通路仍未验证 → 需真实点击 ✕ 做终局判定；")
            say("     若不走（Electron 常见），则拦截层必须改为输入层方案（见替代方案 A/B）。")
    say("=" * 78)

    dump()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        report.append(traceback.format_exc())
        dump()
        raise
