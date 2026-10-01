"""窗口层诊断（规格书 §二十四 的基础部分）。

只回答一个问题：雷神的窗口到底长什么样。
把 HWND / ClassName / 进程 / 版本 / 矩形 / DPI / 显示器 / 窗口层级
一次性打印出来，供排查「找不到窗口」「点歪了」「DPI 不对」这类问题。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os

from detection import coordinate_fallback as cf
from leigod import window as win_mod

user32 = ctypes.windll.user32

GWL_STYLE, GWL_EXSTYLE = -16, -20


def _hierarchy(hwnd) -> list:
    """父链 + 子窗口清单。"""
    chain = []
    cur = int(hwnd)
    while cur:
        chain.append({"hwnd": cur, "class": _class_of(cur), "title": _title_of(cur)})
        cur = int(user32.GetParent(wt.HWND(cur)) or 0)
    children = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        children.append({"hwnd": int(h), "class": _class_of(h), "title": _title_of(h),
                         "visible": bool(user32.IsWindowVisible(h))})
        return True

    user32.EnumChildWindows(wt.HWND(int(hwnd)), cb, 0)
    return {"parents": chain, "children": children[:60], "child_count": len(children)}


def _class_of(hwnd) -> str:
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(int(hwnd)), b, 256)
    return b.value


def _title_of(hwnd) -> str:
    b = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(wt.HWND(int(hwnd)), b, 512)
    return b.value


def process_summary(config) -> list:
    out = []
    for p in win_mod.find_processes(config.get("leigod.process_patterns")):
        out.append({"pid": p.pid, "name": p.name, "exe": p.exe,
                    "version": p.version, "is_launcher": p.is_launcher})
    return out


def inspect(config) -> dict:
    cf.set_dpi_aware()
    procs = process_summary(config)
    win = win_mod.find_main_window(config)
    data = {
        "processes": procs,
        "patterns": config.get("leigod.process_patterns"),
        "class_candidates": config.get("leigod.window_class_candidates"),
        "window": None,
        "hierarchy": None,
        "styles": None,
        "candidates": [],
    }
    # 所有候选窗口都列出来，方便判断「选主窗口」的规则是否选对了
    pids = {p["pid"] for p in procs}
    if pids:
        for hwnd, title, cls, pid in win_mod._enum_windows_of_pids(pids, include_hidden=True):
            rect = cf.get_window_rect(hwnd)
            data["candidates"].append({
                "hwnd": hwnd, "hwnd_hex": hex(hwnd), "title": title, "class": cls,
                "pid": pid, "rect": rect,
                "size": (rect[2] - rect[0], rect[3] - rect[1]),
                "visible": bool(user32.IsWindowVisible(wt.HWND(hwnd))),
                "offscreen": rect[0] < -20000 or rect[1] < -20000,
            })
    if win:
        data["window"] = win.as_dict()
        data["hierarchy"] = _hierarchy(win.hwnd)
        style = user32.GetWindowLongPtrW(wt.HWND(win.hwnd), GWL_STYLE)
        exstyle = user32.GetWindowLongPtrW(wt.HWND(win.hwnd), GWL_EXSTYLE)
        data["styles"] = {"style": hex(style & 0xFFFFFFFF),
                          "exstyle": hex(exstyle & 0xFFFFFFFF),
                          "has_sysmenu": bool(style & 0x00080000),
                          "borderless": not bool(style & 0x00C00000)}
    return data


def render(data: dict) -> str:
    lines = []
    lines.append("== 雷神进程 ==")
    if not data["processes"]:
        lines.append("  （没有匹配到任何进程，请检查 leigod.process_patterns）")
    for p in data["processes"]:
        tag = "启动器" if p["is_launcher"] else "主进程"
        lines.append(f"  PID={p['pid']:<8} {p['name']:<22} {tag}  版本={p['version']}  {p['exe']}")
    lines.append(f"  匹配模式: {data['patterns']}   类名候选: {data['class_candidates']}")

    lines.append("\n== 候选窗口（含隐藏）==")
    for c in data["candidates"]:
        flags = []
        if not c["visible"]:
            flags.append("隐藏")
        if c["offscreen"]:
            flags.append("屏幕外(托盘态)")
        lines.append(f"  HWND={c['hwnd_hex']:<12} class={c['class']:<22} pid={c['pid']:<8} "
                     f"size={c['size'][0]}x{c['size'][1]:<5} title={c['title']!r} "
                     f"{' '.join(flags)}")

    w = data["window"]
    lines.append("\n== 选中的主窗口 ==")
    if not w:
        lines.append("  （没有选中任何窗口）")
        return "\n".join(lines)
    lines.append(f"  HWND        : {w['hwnd_hex']}  ({w['hwnd']})")
    lines.append(f"  Title       : {w['title']!r}")
    lines.append(f"  ClassName   : {w['class_name']}")
    lines.append(f"  PID / 进程  : {w['pid']} / {w['process_name']}")
    lines.append(f"  进程路径    : {w['exe_path']}")
    lines.append(f"  客户端版本  : {w['version']}")
    lines.append(f"  WindowRect  : {w['rect']}")
    lines.append(f"  可见边框    : {w['frame']}")
    lines.append(f"  客户区      : {w['client']}")
    lines.append(f"  宽 x 高     : {w['size'][0]} x {w['size'][1]}")
    lines.append(f"  DPI         : {w['dpi']}  (缩放 {w['dpi'] / 96:.0%})")
    mon = w["monitor"] or {}
    lines.append(f"  显示器      : {mon.get('monitor')} 工作区={mon.get('work')} "
                 f"主屏={mon.get('primary')}")
    lines.append(f"  托盘最小化  : {w['minimized_to_tray']}")
    st = data["styles"] or {}
    lines.append(f"  窗口样式    : style={st.get('style')} exstyle={st.get('exstyle')} "
                 f"有系统菜单={'是' if st.get('has_sysmenu') else '否'}")

    h = data["hierarchy"] or {}
    lines.append(f"\n== 窗口层级 ==  子窗口 {h.get('child_count', 0)} 个")
    for c in (h.get("children") or [])[:20]:
        lines.append(f"  child HWND={hex(c['hwnd']):<12} class={c['class']:<24} "
                     f"visible={c['visible']} title={c['title']!r}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    from core.config import load_config
    from core.logging_setup import setup_logging

    ap = argparse.ArgumentParser(description="雷神窗口诊断（Window Inspector）")
    ap.add_argument("--json", action="store_true", help="额外输出 JSON")
    ap.add_argument("--save", default=None, help="把 JSON 写入指定文件")
    a = ap.parse_args(argv)
    setup_logging("INFO", console=False)
    cfg = load_config()
    data = inspect(cfg)
    print(render(data), flush=True)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)
    if a.save:
        os.makedirs(os.path.dirname(os.path.abspath(a.save)), exist_ok=True)
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n已保存: {a.save}", flush=True)
    return 0 if data["window"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
