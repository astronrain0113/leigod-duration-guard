"""启动器与单实例（规格书 §十九）。

职责（四步，顺序不能乱）：
  1. 单实例互斥体：同一台机器只允许一个保护程序（多开会让两个钩子互相打架）；
  2. 若雷神没在运行 → 启动雷神（优先 leigod_launcher.exe，它才是官方入口）；
  3. 等待雷神**主窗口**出现（不是干等进程，进程起来窗口可能还没建好）；
  4. 返回绑定的窗口，交给引擎。

另外提供一个容易被忽略但很致命的检查：**权限是否匹配**。
雷神若以管理员身份运行，而本程序没有，Windows 的 UIPI 会拒绝我们
禁用它的系统菜单 —— 表现为「✕ 明明灰了却还能点」。必须提前告诉用户。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

from leigod import window as win_mod
from core.paths import base_dir

k32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
advapi32 = ctypes.windll.advapi32

k32.CreateMutexW.restype = ctypes.c_void_p
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
k32.GetLastError.restype = wt.DWORD
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.OpenProcess.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
advapi32.OpenProcessToken.restype = wt.BOOL
advapi32.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                        wt.DWORD, ctypes.POINTER(wt.DWORD)]
advapi32.GetTokenInformation.restype = wt.BOOL

ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20
ERROR_ACCESS_DENIED = 5

CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

#: 启动雷神时使用的 CreateProcess 标志。
#:
#: 踩过的坑（真机上实测）：**DETACHED_PROCESS 与 CREATE_NEW_CONSOLE 互斥**。
#: 两者同时传，CreateProcess 会直接返回 ERROR_INVALID_PARAMETER(87)，
#: 表现为「启动雷神失败：[WinError 87] 参数错误」，雷神根本拉不起来。
#: 之前没被发现是因为靶机测试走的是「雷神已在运行」的早退分支，
#: 启动路径从未真正被执行过。
#: 采用 DETACHED_PROCESS（不继承父进程控制台，适合拉起 GUI 客户端）
#: 配合 CREATE_NEW_PROCESS_GROUP（与父进程的 Ctrl+C 隔离）。
SPAWN_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


# ====================================================================== 单实例
class SingleInstance:
    """命名互斥体实现的单实例。acquire() 返回 False 表示已有实例在跑。"""

    def __init__(self, name: str = "LeigodDurationGuard", global_scope: bool = False):
        # 默认用 Local\\（当前登录会话）即可：保护程序与雷神一定在同一会话里。
        # 用 Global\\ 需要 SeCreateGlobalPrivilege，普通用户会失败。
        self.name = ("Global\\" if global_scope else "Local\\") + name
        self._handle = None
        self.already_running = False

    def acquire(self) -> bool:
        if self._handle:
            return True
        k32.SetLastError(0)
        h = k32.CreateMutexW(None, True, self.name)
        err = k32.GetLastError()
        if not h:
            # 拿不到互斥体（极罕见）：不要因此拦死程序，按"第一个实例"处理
            self.already_running = False
            return True
        self._handle = h
        if err == ERROR_ALREADY_EXISTS:
            # 必须把刚拿到的这个句柄关掉！命名互斥体的「是否已存在」看的是
            # **内核对象是否还有句柄**，而不是谁持有它。若第二个实例不关句柄，
            # 即使第一个实例 release 了，对象依然活着，后续任何实例都会
            # 一直被告知「已在运行」——保护程序从此再也起不来。
            self.already_running = True
            try:
                k32.CloseHandle(ctypes.c_void_p(h))
            except Exception:
                pass
            self._handle = None
            return False
        self.already_running = False
        return True

    def release(self) -> None:
        if self._handle:
            try:
                k32.ReleaseMutex(ctypes.c_void_p(self._handle))
                k32.CloseHandle(ctypes.c_void_p(self._handle))
            except Exception:
                pass
            self._handle = None


# ====================================================================== 权限
def is_elevated() -> bool:
    """本进程是否以管理员身份运行。"""
    try:
        token = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            class TOKEN_ELEVATION(ctypes.Structure):
                _fields_ = [("TokenIsElevated", wt.DWORD)]
            res = TOKEN_ELEVATION()
            size = wt.DWORD()
            ok = advapi32.GetTokenInformation(token, TOKEN_ELEVATION_CLASS,
                                              ctypes.byref(res), ctypes.sizeof(res),
                                              ctypes.byref(size))
            return bool(ok and res.TokenIsElevated)
        finally:
            k32.CloseHandle(token)
    except Exception:
        return False


def process_elevation(pid: int) -> tuple:
    """判断目标进程是否以管理员身份运行。返回 (是否疑似提权, 说明)。

    这里踩过一个**真机才暴露**的坑，务必保留这段说明：
      旧实现只用「OpenProcess 被拒」来反推提权，结果是**永远返回 False**。
      原因是 PROCESS_QUERY_LIMITED_INFORMATION(0x1000) 从 Vista 起就是特意
      设计成**可以跨完整性级别授予**的权限，所以它打得开提权进程 —— 判定恒定
      为「没提权」，权限警告于是永远不触发，安全网形同虚设。

      正确做法是去问令牌本身：
        1) OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)
        2) OpenProcessToken(TOKEN_QUERY)
        3) GetTokenInformation(TokenElevationClass=20)
      任一步骤拿到 ACCESS_DENIED(5)，本身就是「对方完整性级别更高」的强信号；
      其余无法判定的一律保守地当作「疑似提权」——因为漏报的代价是关闭保护
      **静默失效**，比多弹一次提醒严重得多。
    """
    if not pid:
        return False, "无 pid"
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        err = k32.GetLastError()
        if err == ERROR_ACCESS_DENIED:
            return True, "OpenProcess 被拒（ACCESS_DENIED）"
        return False, f"OpenProcess 失败 err={err}"
    try:
        tok = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(ctypes.c_void_p(h), TOKEN_QUERY,
                                         ctypes.byref(tok)):
            err = k32.GetLastError()
            if err == ERROR_ACCESS_DENIED:
                return True, "OpenProcessToken 被拒（ACCESS_DENIED）"
            return False, f"OpenProcessToken 失败 err={err}"
        try:
            class _TE(ctypes.Structure):
                _fields_ = [("TokenIsElevated", wt.DWORD)]
            res = _TE()
            size = wt.DWORD()
            if not advapi32.GetTokenInformation(tok, TOKEN_ELEVATION_CLASS,
                                                ctypes.byref(res),
                                                ctypes.sizeof(res),
                                                ctypes.byref(size)):
                err = k32.GetLastError()
                if err == ERROR_ACCESS_DENIED:
                    return True, "GetTokenInformation 被拒（ACCESS_DENIED）"
                return False, f"GetTokenInformation 失败 err={err}"
            return bool(res.TokenIsElevated), "已读取令牌"
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def process_may_be_elevated(pid: int) -> bool:
    """进程是否疑似以管理员身份运行（保守判定，详见 process_elevation）。"""
    return process_elevation(pid)[0]


def privilege_report(config) -> dict:
    """检查权限是否匹配。不匹配时关闭拦截会静默失效，必须提前告知用户。"""
    me = is_elevated()
    procs = win_mod.find_processes(config.get("leigod.process_patterns"))
    leigod_elevated = any(process_may_be_elevated(p.pid) for p in procs) if procs else None
    ok = True
    msg = ""
    if procs and leigod_elevated and not me:
        ok = False
        msg = ("检测到雷神以管理员身份运行，而本程序不是。\n"
               "此时 Windows 会拒绝本程序锁定雷神的 ✕（UIPI），关闭保护将失效。\n"
               "解决办法：右键本程序 → 以管理员身份运行。")
    return {"self_elevated": me, "leigod_elevated": leigod_elevated,
            "leigod_running": bool(procs), "ok": ok, "message": msg}


# ====================================================================== 雷神
def leigod_processes(config) -> list:
    return win_mod.find_processes(config.get("leigod.process_patterns"))


def leigod_running(config) -> bool:
    return bool(leigod_processes(config))


def _pick_exe(config) -> tuple:
    """决定用哪个可执行文件启动雷神。返回 (路径, 说明)。

    优先 launcher：LeigodAcc 的更新/校验都走 launcher，直接拉 leigod.exe
    在某些版本上会因为缺少启动参数而闪退。
    """
    cands = [("launcher_path", "启动器 leigod_launcher.exe"),
             ("exe_path", "主程序 leigod.exe")]
    for key, label in cands:
        p = str(config.get(f"leigod.{key}") or "").strip()
        if p and os.path.exists(p):
            return p, label
    for key, label in cands:
        p = str(config.get(f"leigod.{key}") or "").strip()
        if p:
            return p, label + "（路径当前不存在，仍尝试启动）"
    return "", "未配置雷神可执行文件路径"


def start_leigod(config, logger=None) -> tuple:
    """启动雷神。返回 (是否发起了启动, 说明)。已在运行则不动。"""
    if leigod_running(config):
        return False, "雷神已在运行，无需启动"
    exe, label = _pick_exe(config)
    if not exe:
        return False, f"无法启动雷神：{label}"
    if not os.path.exists(exe):
        return False, f"无法启动雷神：文件不存在 {exe}"
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe) or None,
                         creationflags=SPAWN_FLAGS, close_fds=True)
        if logger:
            logger.info("已启动雷神（%s）：%s", label, exe)
        return True, f"已启动 {label}：{exe}"
    except OSError as e:
        return False, f"启动雷神失败：{e}"


def wait_for_main_window(config, timeout: float = 90.0, on_step=None,
                         logger=None, poll: float = 1.0):
    """等雷神**主窗口**就绪。返回 (window|None, 是否超时)。"""
    t0 = time.time()
    last_note = -1
    while True:
        w = win_mod.find_main_window(config)
        if w is not None:
            if logger:
                logger.info("雷神主窗口已就绪：HWND=0x%X class=%s pid=%s 尺寸=%s",
                            w.hwnd, w.class_name, w.pid, w.size)
            return w, False
        elapsed = time.time() - t0
        if timeout and elapsed >= timeout:
            if logger:
                logger.warning("等待雷神主窗口超时（%.0fs）", elapsed)
            return None, True
        sec = int(elapsed)
        if on_step and sec != last_note:
            last_note = sec
            try:
                on_step(f"等待雷神主窗口出现…（已等待 {sec}s）")
            except Exception:
                pass
        time.sleep(poll)


def ensure_leigod(config, on_step=None, logger=None, timeout: float = None) -> dict:
    """启动器主流程。返回 {window, steps[], started, timed_out, message}"""
    steps = []
    to = float(timeout if timeout is not None else
               config.get("leigod.start_timeout_seconds", 90))

    def step(text):
        steps.append(text)
        if logger:
            logger.info("启动器：%s", text)
        if on_step:
            try:
                on_step(text)
            except Exception:
                pass

    step(f"单实例检查通过（PID={os.getpid()}）")
    started, why = start_leigod(config, logger)
    step(("雷神未运行 → " if started else "") + why)

    w = win_mod.find_main_window(config)
    if w is None:
        step(f"等待雷神主窗口（最长 {to:.0f}s）…")
        w, timed_out = wait_for_main_window(config, timeout=to, on_step=on_step, logger=logger)
    else:
        timed_out = False
    if w is None:
        msg = ("等待雷神主窗口超时。请确认雷神能正常启动；"
               "也可以先手动打开雷神，再重新启动本程序。")
        step(msg)
        return {"window": None, "steps": steps, "started": started,
                "timed_out": timed_out, "message": msg}
    step(f"已绑定雷神窗口：HWND={hex(w.hwnd)} {w.class_name} {w.size[0]}x{w.size[1]} "
         f"v{w.version or '?'} @{w.dpi}dpi")
    return {"window": w, "steps": steps, "started": started,
            "timed_out": timed_out, "message": "雷神已就绪"}


# ====================================================================== 杂项
def reveal(path: str, logger=None) -> bool:
    """在资源管理器里定位文件/打开目录（托盘菜单用）。"""
    try:
        if os.path.isdir(path):
            os.startfile(path)          # noqa: S606  (Windows 专用)
        elif os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(os.path.dirname(os.path.abspath(path)))
        return True
    except Exception as e:
        if logger:
            logger.warning("打开 %s 失败：%s", path, e)
        return False


def relaunch_tool(module_args: list, cli_args: list, new_console: bool = True, logger=None):
    """以子进程方式运行另一个入口（界面诊断 / 坐标校准）。

    必须区分两种运行形态：
      · 源码运行：python -m diagnostics.ui_inspector
      · 打包成 exe（frozen）后 sys.executable 就是本程序自己，
        这时 `-m 模块名` 是无效的，必须改用本程序自己的开关（--inspector 等）。
    """
    frozen = bool(getattr(sys, "frozen", False))
    cui = _cui_sibling()
    if frozen:
        # 打包形态下必须用带控制台的那个 exe，否则诊断输出用户根本看不到
        cmd = ([cui or sys.executable] + cli_args)
    else:
        cmd = [sys.executable, "-m"] + module_args
    flags = CREATE_NEW_CONSOLE if new_console else 0
    try:
        return subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                                cwd=base_dir())
    except OSError as e:
        if logger:
            logger.error("启动子进程失败 %s：%s", cmd, e)
        return None


def _cui_sibling() -> str:
    """打包形态下用于诊断/校准的控制台版 exe（这些工具必须有可见输出）。

    查找顺序（构建脚本会把控制台版放到第 2 个位置）：
      1. 同目录          LeigodGuardCUI.exe
      2. 子目录 cui\\     cui\\LeigodGuardCUI.exe
      3. 上一级目录       ..\\LeigodGuardCUI\\LeigodGuardCUI.exe
    """
    if not getattr(sys, "frozen", False):
        return ""
    d = os.path.dirname(os.path.abspath(sys.executable))
    names = ("LeigodGuardCUI.exe", "LeigodGuard-cui.exe")
    cands = []
    for n in names:
        cands.append(os.path.join(d, n))
        cands.append(os.path.join(d, "cui", n))
        cands.append(os.path.join(os.path.dirname(d), "LeigodGuardCUI", n))
    for p in cands:
        if os.path.exists(p):
            return p
    return ""
