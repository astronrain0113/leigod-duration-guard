"""以管理员身份启动雷神并等主窗口就绪（真机验证的辅助脚本）。

为什么需要单独一个脚本：`leigod_launcher.exe` 的清单是 `requireAdministrator`
（实测），未提权的进程 `CreateProcess` 它只会拿到 `[WinError 740] 请求的操作需要提升`。
所以"想验证真机行为"这件事本身就需要先提权 —— 用 `tests/run_elevated.py` 包一层即可：

    python tests\\run_elevated.py --timeout 180 --wait tests\\out\\leigod_started.json ^
        -- tests\\start_leigod.py

产物：`tests/out/leigod_started.json`（含 hwnd / class / size / 耗时），
失败时也照样写文件（内容里有 ok=false 与原因），绝不静默。
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

OUT = os.path.join(HERE, "out", "leigod_started.json")


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    res = {"ok": False, "reason": "", "hwnd": 0, "class_name": "", "size": None,
           "already_running": False, "seconds": 0.0}
    t0 = time.time()
    try:
        from core.config import load_config
        from launcher import launcher

        cfg = load_config()
        if launcher.leigod_running(cfg):
            res["already_running"] = True
            res["reason"] = "雷神已在运行"
        else:
            started, why = launcher.start_leigod(cfg, logger=None)
            res["reason"] = why
            if not started and "失败" in why:
                res["seconds"] = round(time.time() - t0, 2)
                _flush(res)
                return 2
        w, timed_out = launcher.wait_for_main_window(cfg, timeout=90)
        res["timed_out"] = bool(timed_out)
        if w is not None:
            res.update(ok=True, hwnd=int(w.hwnd), class_name=w.class_name,
                       size=list(w.size), title=w.title)
            if not res["reason"]:
                res["reason"] = "主窗口已就绪"
        else:
            res["reason"] = "等待雷神主窗口超时"
    except Exception as e:                                    # noqa: BLE001
        res["reason"] = f"{type(e).__name__}: {e}"
    res["seconds"] = round(time.time() - t0, 2)
    _flush(res)
    return 0 if res["ok"] else 1


def _flush(res: dict) -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
