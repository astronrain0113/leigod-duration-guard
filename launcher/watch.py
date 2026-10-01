"""待命守望（Standby Watcher）——「开雷神时自动唤起工具」的实现。

为什么要有这个模块（真机证据，2026-09-29）：
  用户开启「开机待命」后反馈「后台看不到工具，打开加速器后也没看到」。
  逐条查证的结论是三个真实缺陷，而不是用户操作问题：

    1. 计划任务 `/sc onlogon` 只在**下次登录**时触发，不会回溯当前已登录的会话。
       实测证据：任务 StartBoundary=23:30，23:34 复查时 logs/guard.log 仍停在
       23:29:42 —— 任务从未触发过，程序根本没被拉起来。
    2. 旧「待命」= 直接启动完整守卫并 `--minimized` 驻留托盘。守卫会在雷神退出时
       正常退出（这是规格要求的跟随行为），而**没有任何机制把它拉回来**，
       于是用户下一次开雷神时工具不在。
    3. `--minimized` 只有托盘图标，Windows 默认把它收进托盘溢出区（^），
       用户看不见 —— 表现上和"没启动"完全一样。

  所以把「待命」改成真正的按需唤起：一个**只做守望**的极轻进程，
  不加载 OCR、不建面板、不做任何状态判定，只周期性回答两个问题：
    · 雷神在不在？
    · 守卫在不在？
  两者「在 / 不在」时立刻把守卫以**可见面板**拉起。它自己保留一个托盘图标，
  把"后台到底有没有在跑"这件事变成看得见的东西。

武装与去武装（重要，避免和用户抢控制权）：
  唤起一次之后就**去武装**，直到雷神彻底消失过一次才重新武装。
  否则用户在雷神运行期间手动退出守卫，守望会立刻把它拉回来，
  用户就再也关不掉了 —— 那是在替用户做决定，违反 §三十三 的精神。

纪律边界：本模块**不做任何时长状态判定**（那是 duration_detector 的职责），
也不读写雷神窗口。它只回答「在 / 不在」，其余一概不管。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

#: 守卫进程持有的互斥体名（与 main.py 中的单实例同名）
MUTEX_GUARD = "LeigodDurationGuard"
#: 守望进程自己持有的互斥体名（与守卫互不干扰）
MUTEX_WATCHER = "LeigodGuardWatcher"

#: 唤起动作的返回值
WAKE = "wake"            # 已拉起守卫
ALREADY = "already"      # 守卫本来就在跑
IDLE = "idle"            # 雷神没在跑，继续等
DISABLED = "disabled"    # 配置里关掉了自动唤起
STANDBY = "standby"      # 本轮已唤起过（用户自己关掉了），不再打扰
FAILED = "failed"        # 尝试拉起但失败了（必须如实上报，不许当成功）


#: 只看到雷神**进程**、却迟迟看不到可辨认的主窗口时，最多再等多久就照样唤起（秒）。
#: 为什么要有这个宽限：雷神启动途中 Electro 的辅助顶层窗口先可见、真主窗口后建，
#: 此时唤起守卫会让它绑错窗口（真机 13:06 事故）。但也不能永远不唤起 ——
#: 万一某台机器上雷神的窗口类名跟配置不符，那就等于永远不保护。
PROCESS_ONLY_GRACE_S = 20.0


def should_treat_present(window_found: bool, procs_found: bool,
                         proc_since, now: float,
                         grace: float = PROCESS_ONLY_GRACE_S) -> tuple:
    """守望是否该认为「雷神在」。返回 (结论, 新的 proc_since)。

    这是一个**纯函数**，因为它决定的是一件很容易搞错、又很贵的事：
    太早唤起守卫 → 守卫在雷神启动途中绑窗口 → 绑到 Electron 的辅助顶层窗口 →
    状态永远读不出来（真机 2026-09-30 13:06 就是这么坏的）。
    太晚唤起 → 用户开了雷神却没有任何保护。

    规则（顺序不能变）：
      认得主窗口          → 在（最可靠，立刻唤起）
      没有进程            → 不在，计时清零
      只看到进程、刚开始   → 先等（给主窗口一点时间建出来）
      只看到进程、超宽限   → 照样认为在（不能因为"认不出窗口"就永不保护）
    """
    if window_found:
        return True, None
    if not procs_found:
        return False, None
    if proc_since is None:
        return False, now
    return (now - proc_since) >= grace, proc_since


def decide(present: bool, guard_up: bool, armed: bool, auto_wake: bool = True) -> tuple:
    """纯函数：守望决策。返回 (动作, 下一个 armed 状态)。

    规则（顺序不能变）：
      雷神不在          → 重新武装，安静等待下一次开雷神；
      守卫已在跑        → 什么都不做（保持当前武装状态）；
      本轮已唤起过      → 不再打扰（用户是自己关掉的，不替他决定）；
      其余             → 唤起，并立刻去武装。
    """
    if not present:
        return IDLE, True
    if guard_up:
        return ALREADY, armed
    if not auto_wake:
        return DISABLED, armed
    if not armed:
        return STANDBY, armed
    return WAKE, False


class Watcher:
    """待命守望。可被 Qt 定时器和单元测试同样驱动（tick 不含任何阻塞）。"""

    #: 默认轮询间隔（秒）。守望只做进程枚举，2s 一次开销可忽略。
    POLL_S = 2.0

    def __init__(self, config, logger=None, poll: float = None,
                 mutex_guard: str = MUTEX_GUARD):
        self.config = config
        self.log = logger
        self.mutex_guard = mutex_guard
        self.poll = float(poll if poll is not None else
                          config.get("standby.poll_ms", 2000) / 1000.0) or self.POLL_S
        self.armed = True           # 是否会在下一次发现雷神时唤起守卫
        self.last_action = IDLE
        self.wake_count = 0
        #: 第一次「只看到进程、看不到主窗口」的时刻（用于宽限期判定）
        self._proc_since = None

    # ------------------------------------------------------------ 探测
    def leigod_present(self) -> bool:
        """雷神在不在。

        **以认得出的主窗口为准**，不只看进程 —— 真机 13:06 的事故就是
        「进程一出现就唤起」，结果守卫在主窗口建好之前绑到了 Electron 的
        辅助顶层窗口上，状态从此永远读不出来。只看到进程时先给一个宽限期
        （见 `should_treat_present`），超时仍认不出窗口也会照样唤起，
        免得"认不出窗口"变成"永远不保护"。
        """
        try:
            from leigod import window as win_mod
            window_found = win_mod.find_main_window(self.config, strict=True) is not None
            procs_found = (not window_found) and bool(
                win_mod.find_processes(self.config.get("leigod.process_patterns")))
        except Exception as e:
            if self.log:
                self.log.warning("守望：探测雷神失败：%s", e)
            return False
        present, self._proc_since = should_treat_present(
            window_found, procs_found, self._proc_since, time.time())
        if not present and procs_found and self._proc_since is not None:
            # 只打一次（_proc_since 刚被设上的那一轮），免得每 2 秒刷屏
            if self.log and abs(time.time() - self._proc_since) < 0.001:
                self.log.info("守望：看到雷神进程但主窗口还没就绪，先等 "
                              "%.0fs 再唤起（避免绑错窗口）", PROCESS_ONLY_GRACE_S)
        return present

    def guard_running(self) -> bool:
        """守卫在不在跑。用互斥体探测，**探测完立刻释放**。

        这里有个必须小心的地方：命名互斥体的「是否已存在」取决于内核对象
        是否还有句柄。探测时拿到的句柄若不关掉，守卫就再也启动不了
        （详见 launcher.SingleInstance.acquire 里的注释）。
        """
        from launcher.launcher import SingleInstance
        probe = SingleInstance(self.mutex_guard)
        got = False
        try:
            got = probe.acquire()
        finally:
            if got:
                # 只有「我们真的拿到了」才需要释放；拿不到时 acquire 内部已关闭句柄
                probe.release()
        return not got

    def need_elevation(self) -> bool:
        """雷神疑似提权而本进程没提权时，拉起的守卫必须同样提权，
        否则 UIPI 会让关闭保护静默失效（§十九 已明确要求提前告知）。"""
        try:
            from launcher.launcher import is_elevated, process_may_be_elevated
            from leigod import window as win_mod
            if is_elevated():
                return False
            procs = win_mod.find_processes(self.config.get("leigod.process_patterns"))
            return any(process_may_be_elevated(p.pid) for p in procs)
        except Exception:
            return False

    # ------------------------------------------------------------ 拉起
    def _guard_command(self) -> tuple:
        """构造启动守卫的命令。返回 (命令列表, 工作目录)。

        工作目录必须是**程序所在目录**，不是解释器所在目录 —— 源码形态下
        若把 cwd 设成 Python 的 Scripts 目录，守卫会去那里找 config/logs，
        行为就和直接双击 LeigodGuard.exe 不一致了。
        """
        if getattr(sys, "frozen", False):
            exe = os.path.abspath(sys.executable)
            return [exe], os.path.dirname(exe)
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return [sys.executable, os.path.join(here, "main.py")], here

    def launch_guard(self) -> bool:
        """拉起守卫（**带可见面板**，否则用户还是"看不到"）。失败必须如实返回 False。"""
        cmd, cwd = self._guard_command()
        try:
            if self.need_elevation():
                return self._launch_elevated(cmd[0], cwd)
            subprocess.Popen(cmd, cwd=cwd, close_fds=True)
            if self.log:
                self.log.info("守望：已拉起守卫 %s", cmd)
            return True
        except OSError as e:
            if self.log:
                self.log.error("守望：拉起守卫失败 %s：%s", cmd, e)
            return False

    def _launch_elevated(self, exe: str, cwd) -> bool:
        """用 ShellExecute('runas') 提权拉起。会弹一次 UAC —— 这是必须的：
        不提权的守卫在雷神提权时关闭保护会静默失效，比弹一次框严重得多。"""
        try:
            shell32 = ctypes.windll.shell32
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                              ctypes.c_wchar_p, ctypes.c_wchar_p,
                                              ctypes.c_wchar_p, ctypes.c_int]
            rc = shell32.ShellExecuteW(None, "runas", exe, None, cwd, 1)
            # 返回值 > 32 表示成功；<= 32 是错误码（常见 1223 = 用户取消 UAC）
            ok = int(rc or 0) > 32
            if self.log:
                if ok:
                    self.log.info("守望：已提权拉起守卫 %s", exe)
                else:
                    self.log.error("守望：提权拉起守卫失败，ShellExecute 返回 %s"
                                   "（1223 = 你取消了 UAC 授权）", rc)
            return ok
        except Exception as e:
            if self.log:
                self.log.error("守望：提权拉起守卫异常：%s", e)
            return False

    # ------------------------------------------------------------ 驱动
    def tick(self) -> str:
        """一次守望周期。返回动作字符串。**本函数不得阻塞**。"""
        auto_wake = bool(self.config.get("standby.auto_wake", True))
        present = self.leigod_present()
        guard_up = self.guard_running() if present else False
        action, next_armed = decide(present, guard_up, self.armed, auto_wake)
        if action == WAKE:
            if self.launch_guard():
                self.wake_count += 1
                self.armed = next_armed
            else:
                # 拉不起来就是拉不起来，不能假装成功（§三十三）。
                # 保持 armed=True：下一轮再试，而不是永久放弃。
                action = FAILED
        else:
            self.armed = next_armed
        self.last_action = action
        return action


def watcher_single_instance():
    """守望自己的单实例（避免重复开多个守望）。"""
    from launcher.launcher import SingleInstance
    return SingleInstance(MUTEX_WATCHER)
