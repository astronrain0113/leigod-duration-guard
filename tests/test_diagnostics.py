"""诊断工具的自动化验收（规格书 §二十四）。

窗口诊断（Window Inspector）必须给出：PID / HWND / Title / ClassName /
进程路径 / WindowRect / 宽高 / DPI / 显示器 / 窗口层级。
UI 诊断（UI Inspector）必须给出：控件树、目标按钮定位、截图、OCR 明细、
以及最终 DurationState —— 认不出来时必须明确报 UNKNOWN 而不是猜。

运行：python tests/test_diagnostics.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import harness as H                                   # noqa: E402
from diagnostics import ui_inspector as ui            # noqa: E402
from diagnostics import window_inspector as wi        # noqa: E402
from detection import coordinate_fallback as cf       # noqa: E402


def case_window_inspector(rep: H.Report) -> None:
    rep.section("用例 1：Window Inspector 输出完整窗口信息")
    proc, hwnd = H.start_mock("RUNNING")
    try:
        cfg = H.test_config()
        data = wi.inspect(cfg)
        w = data.get("window")
        rep.check("找到了主窗口", bool(w), "未找到")
        if not w:
            return
        rep.check("HWND 正确", int(w["hwnd"]) == int(hwnd), f"{w['hwnd']} vs {hwnd}")
        rep.check("类名正确", w["class_name"] == H.MOCK_CLS, w["class_name"])
        rep.check("标题正确", "雷神" in w["title"], w["title"])
        rect = cf.get_window_rect(hwnd)
        rep.check("窗口矩形与真实一致", tuple(w["rect"]) == tuple(rect),
                  f"{w['rect']} vs {rect}")
        rep.check("宽高与真实一致",
                  tuple(w["size"]) == (rect[2] - rect[0], rect[3] - rect[1]), str(w["size"]))
        rep.check("DPI 已读出", int(w["dpi"]) > 0, str(w["dpi"]))
        rep.check("进程路径已读出", bool(w["exe_path"]), w["exe_path"])
        rep.check("记录了显示器信息", w["monitor"].get("monitor") is not None,
                  str(w["monitor"].get("monitor")))
        st = data.get("styles") or {}
        rep.check("读出了窗口样式（含系统菜单标志）", st.get("style") is not None, str(st))
        rep.check("列出了子窗口", (data.get("hierarchy") or {}).get("child_count", 0) >= 1,
                  str((data.get("hierarchy") or {}).get("child_count")))

        text = wi.render(data)
        for label in ("HWND", "ClassName", "WindowRect", "DPI", "托盘最小化", "窗口层级"):
            rep.check(f"文本报告含「{label}」", label in text)
        rep.info(f"报告首行: {text.splitlines()[0]}")
    finally:
        proc.terminate()


def case_ui_inspector_running(rep: H.Report) -> None:
    rep.section("用例 2：UI Inspector（RUNNING）识别出暂停按钮与状态")
    proc, hwnd = H.start_mock("RUNNING")
    try:
        cfg = H.test_config()
        out = os.path.join(H.OUT, "inspector_running")
        rep_data = ui.run(out, config=cfg, verbose=False)

        res = rep_data.get("result") or {}
        rep.check("最终结论为 RUNNING", res.get("state") == "RUNNING", str(res.get("state")))
        rep.check("给出了依据（证据链非空）", bool(res.get("detail")), str(res.get("detail")))

        ctrls = (rep_data.get("ui_automation") or {}).get("controls") or []
        rep.check("枚举到了控件树", len(ctrls) >= 1, f"{len(ctrls)} 个控件")
        found = rep_data.get("duration_controls") or {}
        rep.check("定位到「暂停时长」按钮", len(found.get("pause") or []) >= 1,
                  json.dumps(found.get("pause"), ensure_ascii=False))
        rep.check("确认按钮支持 Invoke（第一优先级可用）",
                  any(c.get("supports_invoke") for c in (found.get("pause") or [])),
                  json.dumps(found.get("pause"), ensure_ascii=False))

        full = os.path.join(out, "leigod_full.png")
        region = os.path.join(out, "duration_region.png")
        rep.check("已保存 leigod_full.png", os.path.exists(full), full)
        rep.check("已保存 duration_region.png", os.path.exists(region), region)
        rep.check("已保存 inspector.json", os.path.exists(os.path.join(out, "inspector.json")))
        ocr = rep_data.get("ocr") or {}
        rep.check("有 OCR 结果结构（recognized_text/confidence/bounding_box）",
                  "lines" in ocr, str(list(ocr.keys())))
        if ocr.get("lines"):
            l = ocr["lines"][0]
            rep.check("OCR 明细字段完整",
                      all(k in l for k in ("text", "confidence", "bbox")), str(l))
            rep.info(f"OCR 首行: {l['text']!r} conf={l['confidence']} bbox={l['bbox']}")
            rep.check("已保存 ocr.json", os.path.exists(os.path.join(out, "ocr.json")))
    finally:
        proc.terminate()


def case_ui_inspector_unknown(rep: H.Report) -> None:
    rep.section("用例 3：UI Inspector 认不出来时必须报 UNKNOWN（不许猜）")
    proc, hwnd = H.start_mock("RUNNING", label="雷神加速器")
    try:
        cfg = H.test_config()
        cfg.set("detection.ocr_enabled", False, save=False)
        out = os.path.join(H.OUT, "inspector_unknown")
        rep_data = ui.run(out, config=cfg, verbose=False)
        res = rep_data.get("result") or {}
        rep.check("最终结论为 UNKNOWN", res.get("state") == "UNKNOWN", str(res.get("state")))
        rep.check("说明了为什么认不出来", bool(res.get("detail")), str(res.get("detail")))
        found = rep_data.get("duration_controls") or {}
        rep.check("明确报告两个按钮都没找到",
                  not (found.get("pause") or found.get("start")),
                  json.dumps(found, ensure_ascii=False))
    finally:
        proc.terminate()


def case_no_window(rep: H.Report) -> None:
    rep.section("用例 4：没有雷神窗口时，诊断工具必须如实报告而不是报假数据")
    H.kill_stale_mocks()
    cfg = H.test_config()
    # 用一个绝不可能存在的进程模式，确保「找不到」这个场景是干净的
    cfg.set("leigod.process_patterns", ["zzz_no_such_process_zzz"], save=False)
    data = wi.inspect(cfg)
    rep.check("明确没有选中窗口", data.get("window") is None, str(data.get("window")))
    text = wi.render(data)
    rep.check("文本里说明了没找到", "没有选中任何窗口" in text)
    out = os.path.join(H.OUT, "inspector_none")
    rep_data = ui.run(out, config=cfg, verbose=False)
    rep.check("UI Inspector 结论为 UNKNOWN",
              (rep_data.get("result") or {}).get("state") == "UNKNOWN",
              str((rep_data.get("result") or {}).get("state")))


def main() -> int:
    from core.logging_setup import setup_logging
    setup_logging("INFO", console=False)
    rep = H.Report("诊断工具（Window / UI Inspector）—— 自动化验收")
    print(rep.title, flush=True)
    H.kill_stale_mocks()
    case_window_inspector(rep)
    case_ui_inspector_running(rep)
    case_ui_inspector_unknown(rep)
    case_no_window(rep)
    path = rep.save("diagnostics_report.txt")
    fails = sum(1 for l in rep.lines if "[FAIL]" in l)
    print(f"\n结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}；报告 {path}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
