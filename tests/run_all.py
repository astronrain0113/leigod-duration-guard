"""一键运行全部验收并汇总（规格书 §三十三：不许假装成功）。

五套用例，从纯逻辑到真实窗口操作逐级递进：
  test_units.py         纯逻辑：状态机 / 识别 / 配置 / 监控 / 文案（无需雷神）
  test_diagnostics.py   诊断工具：窗口与 UI 诊断的输出是否真实可信
  test_calibration.py   坐标校准：必须通过真实测试才允许写配置
  test_ui_smoke.py      UI 伴生层：状态渲染 / 窗口跟随 / 按钮 / 通知
  test_e2e_mock.py      关闭保护端到端：规格书测试 A/B/C/D + 拦截可逆性

用法：
  python tests/run_all.py           全部运行
  python tests/run_all.py units ui  只跑指定几套（名字前缀匹配）

每套的输出都完整落盘到 tests/out/<名称>.log，便于事后核对。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

SUITES = [
    ("units", "test_units.py", "纯逻辑单元测试（状态机 / 识别 / 配置 / 监控 / 文案）"),
    ("real", "test_real_capture.py", "真机截图回归（裁剪 / OCR / 判定 链路）"),
    ("diagnostics", "test_diagnostics.py", "诊断工具（Window / UI Inspector）"),
    ("calibration", "test_calibration.py", "坐标校准（必须通过真实点击测试）"),
    ("ui", "test_ui_smoke.py", "UI 伴生层（面板 / 跟随 / 通知）"),
    ("launcher", "test_launcher_flow.py", "启动器流程与命令行入口（含单实例）"),
    ("e2e", "test_e2e_mock.py", "关闭保护端到端（规格书 A/B/C/D）"),
    ("guard", "test_close_guard.py", "关闭保护 v2 闭环（结构仿真靶机：自绘 ✕ + 应用内确认框）"),
]


def main(argv=None) -> int:
    picks = [a.lower() for a in (argv or sys.argv[1:])]
    suites = [s for s in SUITES if not picks or any(p in s[0] for p in picks)]
    if not suites:
        print(f"没有匹配的用例：{picks}", flush=True)
        return 2

    results = []
    for key, script, title in suites:
        print(f"\n{'#' * 70}\n# {title}\n# {script}\n{'#' * 70}", flush=True)
        log = os.path.join(OUT, f"run_{key}.log")
        t0 = time.time()
        with open(log, "w", encoding="utf-8") as f:
            p = subprocess.run([sys.executable, os.path.join(HERE, script)],
                               cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
        dt = time.time() - t0
        text = ""
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            pass
        fails = sum(1 for l in text.splitlines() if "[FAIL]" in l)
        passes = sum(1 for l in text.splitlines() if "[PASS]" in l)
        for line in text.splitlines():
            if "[FAIL]" in line:
                print(line, flush=True)
        tail = [l for l in text.splitlines() if l.startswith("结果：")]
        print(f"→ 退出码 {p.returncode}｜PASS {passes}｜FAIL {fails}｜耗时 {dt:.1f}s"
              f"{'｜' + tail[-1] if tail else ''}", flush=True)
        results.append((title, script, p.returncode, passes, fails, dt, log))

    print(f"\n{'=' * 70}\n汇总\n{'=' * 70}", flush=True)
    lines = ["雷神总时长保护 —— 全部验收汇总", ""]
    for title, script, rc, ps, fs, dt, log in results:
        mark = "通过" if rc == 0 and fs == 0 else "失败"
        line = (f"  [{mark}] {title:<40} PASS {ps:<4} FAIL {fs:<3} {dt:5.1f}s  {script}")
        print(line, flush=True)
        lines.append(line + f"   日志={os.path.basename(log)}")
    bad = [r for r in results if r[2] != 0 or r[4] != 0]
    lines.append("")
    lines.append(f"结论：{'全部通过' if not bad else f'{len(bad)} 套未通过'}")
    summary = os.path.join(OUT, "run_all_report.txt")
    with open(summary, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n结论：{'全部通过' if not bad else f'{len(bad)} 套未通过'}；汇总 {summary}",
          flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
