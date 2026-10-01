"""真机测量：UIA 扫描耗时 A/B（2026-09-30 性能治理的量化依据）。

要回答的问题很具体：用户报「点 ✕ 之后要 6~8 秒才自动暂停」，并要求做到 1~2 秒。
延迟几乎全部来自「每轮识别都要枚举整棵控件树」。这个脚本在**同一台机器、同一个窗口**
上分别测量三种做法，给出可直接比较的毫秒数：

  A. 旧做法：`list_controls()` —— 为**每个**控件都建 ControlInfo（读 8 个属性
     + 做一次 GetPattern 查询）。这就是旧 `find_duration_controls` 的成本。
  B. 新做法（冷）：`find_duration_controls(fast=False)` —— 只读 Name，
     命中的才建对象；InvokePattern 只探前几个。会填充缓存。
  C. 新做法（热）：`find_duration_controls(fast=True)` —— 走缓存，只重读
     1~2 个控件的 Name。这是稳态轮询/暂停后复核的实际成本。

用法（必须提权：雷神恒以管理员运行，未提权时 UIA 枚举恒为 0 个控件）：

    python tests\\run_elevated.py --timeout 240 --wait tests\\out\\uia_scan.json ^
        -- tests\\measure_uia_scan.py --start-leigod

不加 `--start-leigod` 就要求雷神已经在跑。
产物：`tests/out/uia_scan.json`（无论成败都写，失败也写原因）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

OUT = os.path.join(HERE, "out", "uia_scan.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-leigod", action="store_true",
                    help="雷神没在跑时先把它启动起来（需要提权）")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    res = {"ok": False, "reason": "", "uia_controls": 0, "samples": []}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    try:
        from core.config import load_config
        from leigod import window as win_mod
        from detection import ui_automation as uia_mod

        cfg = load_config()
        if args.start_leigod:
            from launcher import launcher
            if not launcher.leigod_running(cfg):
                res["started"] = launcher.start_leigod(cfg, logger=None)[1]
            w, timed_out = launcher.wait_for_main_window(cfg, timeout=90)
            res["start_wait_timed_out"] = bool(timed_out)
        else:
            w = win_mod.find_main_window(cfg, strict=True)

        if w is None:
            res["reason"] = "未找到雷神主窗口（请先打开雷神）"
            _flush(res)
            return 1

        frame = w.frame or w.rect
        crop = cfg.get("detection.topbar_crop")
        res["hwnd"] = int(w.hwnd)
        res["class_name"] = w.class_name
        res["size"] = list(w.size)

        for i in range(max(1, args.rounds)):
            s = {"round": i + 1}

            t0 = time.perf_counter()
            allc = uia_mod.list_controls(int(w.hwnd))
            s["A_old_build_all_objects_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            s["A_control_count"] = len(allc)

            uia_mod.clear_cache()
            t0 = time.perf_counter()
            r = uia_mod.find_duration_controls(int(w.hwnd), frame=frame, crop=crop,
                                               fast=False)
            s["B_new_cold_full_scan_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            s["B_all_n"] = int(r.get("all_n") or 0)
            s["B_pause"] = len(r.get("pause") or [])
            s["B_start"] = len(r.get("start") or [])
            s["B_scan_ms_reported"] = round(float(r.get("scan_ms") or 0), 1)
            res["uia_controls"] = int(r.get("all_n") or 0)

            # 模拟真实暂停后的"立刻复核"（缓存刚建好，元素可能刚改名）
            t0 = time.perf_counter()
            r2 = uia_mod.find_duration_controls(int(w.hwnd), frame=frame, crop=crop,
                                                fast=True)
            s["C_new_warm_cached_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            s["C_from_cache"] = bool(r2.get("from_cache"))
            s["C_pause"] = len(r2.get("pause") or [])
            s["C_start"] = len(r2.get("start") or [])
            res["samples"].append(s)

        res["ok"] = True
        res["reason"] = "测量完成"
    except Exception as e:                                    # noqa: BLE001
        res["reason"] = f"{type(e).__name__}: {e}"
    _flush(res)
    return 0 if res["ok"] else 1


def _flush(res: dict) -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
