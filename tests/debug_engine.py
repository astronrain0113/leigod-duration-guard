"""调试：让引擎单步跑一遍，把异常暴露出来。"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness as H                                   # noqa: E402
from core.logging_setup import setup_logging          # noqa: E402

log = setup_logging("DEBUG", console=True)
proc, hwnd = H.start_mock("RUNNING")
print("mock hwnd", hex(hwnd), flush=True)

from leigod import window as win_mod                  # noqa: E402
cfg = H.test_config()
print("patterns", cfg.get("leigod.process_patterns"),
      "classes", cfg.get("leigod.window_class_candidates"), flush=True)
procs = win_mod.find_processes(cfg.get("leigod.process_patterns"))
print("matched processes:", [(p.pid, p.name) for p in procs], flush=True)
w = win_mod.find_main_window(cfg)
print("main window:", w.as_dict() if w else None, flush=True)

from core.protection_engine import ProtectionEngine   # noqa: E402
engine = ProtectionEngine(cfg, logger=log)
try:
    engine._ensure_window(__import__("time").time())
    print("win after ensure:", engine.win, flush=True)
    engine._tick()
    print("status:", engine._last_status, flush=True)
except Exception:
    traceback.print_exc()

# 单独测识别
if w:
    eng = engine.detector
    r = eng.detect(w, allow_ocr=True)
    print("reading:", r.state.value, "|", r.summary(), flush=True)
    print("uia pause:", [c.name for c in eng.last_uia.get("pause", [])], flush=True)
    print("uia start:", [c.name for c in eng.last_uia.get("start", [])], flush=True)
    print("uia total:", len(eng.last_uia.get("all", [])), flush=True)
    print("capture:", eng.last_capture.method, eng.last_capture.detail, flush=True)
    print("ocr lines:", eng.last_ocr_lines, flush=True)

proc.terminate()
