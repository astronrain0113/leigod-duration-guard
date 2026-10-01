"""游戏进程监控（第二保险丝，规格书 §十七/§十八/§十九）。

只做一件事：判断「被监控的游戏是不是全都退出了，并且退出了足够久」。
游戏退出后立刻暂停是错的——玩家可能马上换一个游戏；
所以必须等 exit_wait_seconds（默认 30 秒）后再次确认没有游戏在跑，才允许触发暂停。
"""
from __future__ import annotations

import time

import psutil

#: 常见游戏进程名（小写，子串匹配）。
#: 刻意不含 Steam / WeGame 等启动器：启动器常驻 ≠ 游戏在运行。
#: 误判方向很关键——把「有游戏」误判成「没游戏」会导致多余的暂停；
#: 把「没游戏」误判成「有游戏」会导致该暂停时不暂停。后者更危险，
#: 因此这里只放明确是游戏本体的进程名。
BUILTIN_GAME_PROCESSES = [
    "cs2", "csgo", "dota2", "league of legends.exe", "leagueclient",
    "valorant", "overwatch", "apex", "pubg", "fortnite", "tslgame",
    "genshinimpact", "yuanshen", "starrail", "wuthering",
    "eldenring", "cyberpunk2077", "gta5", "rdr2", "warframe",
    "destiny2", "ffxiv", "wow.exe", "diablo", "minecraft",
    "javaw.exe", "rainbowsix", "escapefromtarkov", "hunt.exe",
]

_EXCLUDED_PROCS = {"explorer.exe", "searchhost.exe", "shellexperiencehost.exe",
                   "textinputhost.exe"}
_EXCLUDED_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd",
                     "Shell_SecondaryTrayWnd", "XamlExplorerHostIslandWindow",
                     "Windows.UI.Core.CoreWindow", "ApplicationFrameWindow"}


class GameMonitor:
    def __init__(self, config, logger=None):
        self.config = config
        self.log = logger
        self._exited_since = None
        self._seen_game = False
        self._last_running = []

    @property
    def extra(self) -> list:
        return [p.lower() for p in (self.config.get("game_monitor.processes") or [])]

    def _match_processes(self) -> list:
        patterns = BUILTIN_GAME_PROCESSES + self.extra
        matched = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info["name"] or "")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            low = name.lower()
            if any(p in low for p in patterns):
                matched.append(name)
        return sorted(set(matched))

    def _foreground_fullscreen(self) -> tuple:
        try:
            import win32api
            import win32gui
            import win32process
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False, "", ""
            if win32gui.GetClassName(hwnd) in _EXCLUDED_CLASSES:
                return False, "", ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                pname = psutil.Process(pid).name().lower()
            except psutil.Error:
                pname = ""
            if pname in _EXCLUDED_PROCS:
                return False, "", ""
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
            full = (r - l) >= sw - 8 and (b - t) >= sh - 8
            return full, win32gui.GetWindowText(hwnd), pname
        except Exception:
            return False, "", ""

    def poll(self) -> dict:
        running = self._match_processes()
        full, ftitle, fproc = (False, "", "")
        if self.config.get("game_monitor.fullscreen_heuristic", False):
            full, ftitle, fproc = self._foreground_fullscreen()
        any_running = bool(running) or full

        now = time.time()
        if any_running:
            if not self._seen_game:
                self._seen_game = True      # 只有真的见过游戏，之后才谈得上「游戏退出」
                if self.log:
                    self.log.info("首次检测到游戏运行：%s", ", ".join(running) or fproc)
            elif self._exited_since is not None and self.log:
                self.log.info("检测到游戏重新启动，取消自动暂停计时")
            self._exited_since = None
        else:
            if self._seen_game and self._exited_since is None:
                self._exited_since = now
                if self.log:
                    self.log.info("所有被监控游戏均已退出，开始 %s 秒等待",
                                  self.config.get("game_monitor.exit_wait_seconds", 30))

        elapsed = (now - self._exited_since) if self._exited_since else 0.0
        wait = max(5, int(self.config.get("game_monitor.exit_wait_seconds", 30)))
        self._last_running = running
        return {
            "running": any_running,
            "processes": running,
            "foreground_fullscreen": full,
            "foreground_title": ftitle,
            "foreground_process": fproc,
            "seen_game": self._seen_game,
            "exited_seconds": elapsed,
            "exit_wait_seconds": wait,
            "ready_to_pause": bool(self._seen_game and self._exited_since and elapsed >= wait),
        }

    def reset(self) -> None:
        self._exited_since = None
