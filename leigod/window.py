"""雷神窗口定位：进程 → 窗口 → 几何信息（规格书 §八 / §十三 / §二十）。

本机实测事实（用于配置默认值与排障）：
  - 安装目录 D:\\LeiGod_Acc，主程序 leigod.exe，另有 leigod_launcher.exe
  - 客户端是 Electron（存在 chrome_*.pak / icudtl.dat / LICENSES.chromium.html），
    版本号见安装目录 version_f.txt（实测 11.3.2.9），窗口类名预期 Chrome_WidgetWin_1
  - 顶栏右侧顺序：[剩余时长] [开启时长|暂停时长] [充值] [≡] [−] [✕]
  - 最小化到托盘时窗口被挪到 -25600，IsIconic 仍为 False

进程名匹配而不能只用 launcher：leigod_launcher.exe 会另起 leigod.exe，
两者都含「leigod」，所以必须优先精确匹配主进程名。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from dataclasses import dataclass, field

import psutil

from detection import coordinate_fallback as cf

user32 = ctypes.windll.user32


@dataclass
class ProcessInfo:
    pid: int
    name: str = ""
    exe: str = ""
    version: str = ""
    is_launcher: bool = False


#: 窗口匹配方式。**绑定决策必须看它**，不能只看"找到了一个窗口"。
#:
#: 真机教训（2026-09-30 13:06，用户报「工具又无法判断时长状态了」）：
#: 守望在雷神**启动途中**把守卫唤起（进程一出现就唤起），此时主窗口
#: `Chrome_WidgetWin_1`(1500x938) 还没建好，而 Electron 的辅助顶层窗口
#: `Chrome_WidgetWin_0`(1920x1130, 无标题) 已经可见。旧实现在类名/标题都
#: 没命中时会**静默退化成"挑最大的顶层窗口"**，于是绑到了辅助窗口 ——
#: 表现是 UIA 只枚举到 6 个控件（正常 ~476）、OCR 说"雷神不在前台"，
#: 状态永远 UNKNOWN，而且**绑上之后再也不复核**，只能重启程序。
#: 所以：匹配方式必须如实记录，弱匹配必须可被识别、可被替换。
MATCH_CLASS = "class"        # 命中了配置的窗口类名（最可靠）
MATCH_TITLE = "title"        # 命中了配置的标题关键词
MATCH_NOFILTER = "nofilter"  # 没配任何规则 → 不存在"认错"这回事
MATCH_FALLBACK = "fallback"  # **弱匹配**：没对上任何规则，只凭尺寸挑的

#: 哪些匹配方式算"认准了"
STRICT_MATCHES = (MATCH_CLASS, MATCH_TITLE, MATCH_NOFILTER)


@dataclass
class LeigodWindow:
    hwnd: int = 0
    title: str = ""
    class_name: str = ""
    pid: int = 0
    process_name: str = ""
    exe_path: str = ""
    version: str = ""
    rect: tuple = None
    frame: tuple = None
    client: tuple = None
    dpi: int = 96
    monitor: dict = field(default_factory=dict)
    minimized_to_tray: bool = False
    #: 这个窗口是怎么被认出来的（见上面的 MATCH_* 常量）
    match: str = "unknown"

    @property
    def weak_match(self) -> bool:
        """是否是"没认准"的窗口（只有 fallback 算弱匹配）。"""
        return self.match == MATCH_FALLBACK

    @property
    def size(self) -> tuple:
        if not self.rect:
            return (0, 0)
        return (self.rect[2] - self.rect[0], self.rect[3] - self.rect[1])

    def as_dict(self) -> dict:
        return {
            "hwnd": self.hwnd, "hwnd_hex": hex(self.hwnd), "title": self.title,
            "class_name": self.class_name, "pid": self.pid,
            "process_name": self.process_name, "exe_path": self.exe_path,
            "version": self.version, "rect": self.rect, "frame": self.frame,
            "client": self.client, "size": self.size, "dpi": self.dpi,
            "monitor": self.monitor, "minimized_to_tray": self.minimized_to_tray,
            "match": self.match, "weak_match": self.weak_match,
        }


def _read_version(exe_path: str) -> str:
    """读取雷神安装目录的 version_f.txt（比解析 exe 版本资源更稳）。"""
    try:
        p = os.path.join(os.path.dirname(exe_path), "version_f.txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
    except OSError:
        pass
    return ""


def find_processes(patterns: list) -> list:
    """按进程名匹配。patterns 为空时使用默认 leigod 规则。

    刻意排除**本进程自己**：process_patterns 是用户可改的，一旦写宽了
    （例如写成 "python"），本程序自己的窗口就会被当成雷神，
    进而去拦截自己的 ✕、在自己窗口里找「暂停时长」——必须从源头挡掉。
    """
    pats = [p.lower() for p in (patterns or ["leigod.exe"])]
    me = os.getpid()
    found = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info["name"] or "")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if proc.info["pid"] == me:
            continue
        low = name.lower()
        if not any(p in low for p in pats):
            continue
        try:
            exe = proc.info["exe"] or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            exe = ""
        found.append(ProcessInfo(pid=proc.info["pid"], name=name, exe=exe,
                                 version=_read_version(exe),
                                 is_launcher="launcher" in low))
    # 主进程优先（leigod.exe 优于 leigod_launcher.exe）
    found.sort(key=lambda p: (p.is_launcher, len(p.name)))
    return found


def _enum_windows_of_pids(pids: set, include_hidden: bool = False) -> list:
    """枚举这些进程的顶层窗口 → [(hwnd, title, class_name, pid)]。

    include_hidden=True 时连不可见窗口一起收进来：雷神最小化到托盘后
    窗口可能被隐藏，此时仍需拿到它并 SendMessage 唤醒，否则状态识别与
    暂停都无从下手（只能干瞪眼）。
    """
    result = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        try:
            if not include_hidden and not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetParent(hwnd):
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            result.append((int(hwnd), title.value, cls.value, pid.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return result


def _pick_main(candidates: list, min_size: tuple) -> tuple:
    """从候选里挑主窗口：优先屏幕内且尺寸达标的，面积最大者；否则取屏幕外的（托盘态）。"""
    on_screen, off_screen = [], []
    mw, mh = min_size
    for hwnd, title, cls, pid in candidates:
        rect = cf.get_window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        item = (w * h, hwnd, title, cls, pid, rect)
        if rect[0] < -20000 or rect[1] < -20000:
            off_screen.append(item)          # 最小化到屏幕外，尺寸不可信
        elif w >= mw and h >= mh:
            on_screen.append(item)
    pool = on_screen or off_screen
    if not pool:
        return None
    _, hwnd, title, cls, pid, rect = max(pool, key=lambda i: i[0])
    return hwnd, title, cls, pid, rect


def narrow_candidates(cands: list, classes: list, keywords: list) -> tuple:
    """按配置收窄候选窗口，并**如实返回匹配方式**。

    返回 `(候选列表, MATCH_*)`。旧实现只返回候选，把"没命中任何规则"这件事
    藏了起来 —— 调用方看到"有候选"就以为找到了，于是绑到了 Electron 的辅助
    顶层窗口上（真机 13:06 实例）。把匹配方式显式暴露出来，
    调用方才能决定"宁可再等"还是"先用着但要盯着"。
    """
    if classes:
        hit = [c for c in cands if c[2].lower() in classes]
        if hit:
            return hit, MATCH_CLASS
    if keywords:
        hit = [c for c in cands if any(k in c[1].lower() for k in keywords)]
        if hit:
            return hit, MATCH_TITLE
    if not classes and not keywords:
        return cands, MATCH_NOFILTER    # 没配规则 → 不存在"认错"这回事
    return cands, MATCH_FALLBACK


def find_main_window(config, strict: bool = False) -> LeigodWindow | None:
    """定位雷神主窗口。找不到返回 None（绝不猜）。

    `strict=True`：**只接受匹配上配置规则（窗口类 / 标题）的窗口**，没对上就
    返回 None，把"再等一会儿"的决定权交给调用方。什么时候需要它：
    雷神启动途中主窗口还没建好，但辅助顶层窗口已经可见 —— 这时候宽松匹配
    会挑错窗口，而且（旧实现）绑上就再也不复核。
    """
    procs = find_processes(config.get("leigod.process_patterns"))
    if not procs:
        return None
    pids = {p.pid for p in procs}
    by_pid = {p.pid: p for p in procs}

    classes = [c.lower() for c in (config.get("leigod.window_class_candidates") or [])]
    keywords = [k.lower() for k in (config.get("leigod.window_title_keywords") or [])]
    min_size = tuple(config.get("leigod.min_window_size", [300, 200]))

    picked = None
    picked_kind = MATCH_FALLBACK
    weak = None                 # 弱匹配时留一手，非严格模式才有资格用
    weak_kind = MATCH_FALLBACK
    # 两趟：先只看可见窗口（正常情况），再把隐藏窗口收进来（托盘态，尺寸不可信）。
    for include_hidden, min_sz in ((False, min_size), (True, (1, 1))):
        raw = _enum_windows_of_pids(pids, include_hidden)
        if not raw:
            continue
        cands, kind = narrow_candidates(raw, classes, keywords)
        if kind not in STRICT_MATCHES:
            if weak is None:
                weak, weak_kind = _pick_main(cands, min_sz), kind
            continue
        picked = _pick_main(cands, min_sz)
        if picked:
            picked_kind = kind
            break

    if picked is None:
        if strict or weak is None:
            return None
        picked, picked_kind = weak, weak_kind

    hwnd, title, cls, pid, rect = picked
    proc = by_pid.get(pid)
    win = LeigodWindow(hwnd=hwnd, title=title, class_name=cls, pid=pid,
                       process_name=proc.name if proc else "",
                       exe_path=proc.exe if proc else "",
                       version=(proc.version if proc else "") or config.get("leigod.client_version", ""),
                       match=picked_kind)
    return refresh(win)


def refresh(win: LeigodWindow) -> LeigodWindow:
    """重新读取窗口几何信息（窗口可能被移动/缩放/最小化）。"""
    win.rect = cf.get_window_rect(win.hwnd)
    win.frame = cf.get_frame_bounds(win.hwnd)
    win.client = cf.get_client_rect_screen(win.hwnd)
    win.dpi = cf.get_dpi(win.hwnd)
    win.monitor = cf.get_monitor(win.hwnd)
    win.minimized_to_tray = cf.is_minimized_offscreen(win.hwnd)
    return win


def is_valid(hwnd) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(wt.HWND(int(hwnd))))
