"""启动器流程与命令行入口的验收（规格书 §十九）。

覆盖：
  · ensure_leigod：雷神已在运行 / 需要等待 / 超时 / 可执行文件缺失，四种情形都要如实报告
  · main.py --launcher --no-ui：真实的启动器链路（拉起 → 等窗口 → 绑定 → 起引擎）
  · 单实例：第二个实例必须自己退出，而不是两个保护程序同时在跑
  · 环境自检 --check：没有雷神时也要能正常给出结论而不崩
  · run_elevated.py 的参数切分：错一次就是一次莫名的 UAC 提权启动

运行：python tests/test_launcher_flow.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import harness as H                                     # noqa: E402
from launcher import launcher                           # noqa: E402

PY = sys.executable


def _one(text: str, limit: int = 160) -> str:
    """把多行输出压成一行，避免报告被整屏日志淹没。"""
    s = " / ".join(x.strip() for x in str(text or "").splitlines() if x.strip())
    return s[:limit] + ("…" if len(s) > limit else "")


def _run(args, timeout=90):
    p = subprocess.run([PY, os.path.join(ROOT, "main.py")] + args, cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def case_ensure_with_mock(rep: H.Report) -> None:
    rep.section("启动器：雷神已在运行 → 直接绑定，不重复拉起")
    proc, hwnd = H.start_mock("RUNNING")
    try:
        cfg = H.test_config()
        res = launcher.ensure_leigod(cfg, timeout=15)
        rep.check("找到了主窗口", res["window"] is not None, str(res.get("message")))
        rep.check("没有重复启动雷神", res["started"] is False, str(res["steps"]))
        rep.check("记录里说明了「已在运行」",
                  any("已在运行" in s for s in res["steps"]), str(res["steps"]))
        if res["window"]:
            rep.check("绑定的是靶机窗口",
                      int(res["window"].hwnd) == int(hwnd),
                      f"{hex(res['window'].hwnd)} vs {hex(int(hwnd))}")
        rep.check("最后一步给出了窗口身份",
                  any("已绑定雷神窗口" in s for s in res["steps"]), str(res["steps"]))
        rep.info("启动器步骤：" + " | ".join(res["steps"]))
    finally:
        proc.terminate()


def case_ensure_missing_exe(rep: H.Report) -> None:
    rep.section("启动器：雷神未运行且可执行文件缺失 → 如实报告并超时退出")
    H.kill_stale_mocks()
    cfg = H.test_config()
    cfg.set("leigod.process_patterns", ["zzz_none_zzz"], save=False)
    cfg.set("leigod.launcher_path", r"D:\no_such_dir\leigod_launcher.exe", save=False)
    cfg.set("leigod.exe_path", r"D:\no_such_dir\leigod.exe", save=False)
    t0 = time.time()
    res = launcher.ensure_leigod(cfg, timeout=3)
    dt = time.time() - t0
    rep.check("没有找到窗口", res["window"] is None)
    rep.check("标记为超时", res["timed_out"] is True, str(res))
    rep.check("没有假装启动成功", res["started"] is False, str(res["steps"]))
    rep.check("指出可执行文件不存在",
              any("不存在" in s for s in res["steps"]), str(res["steps"]))
    rep.check("超时时长被遵守（未卡死）", dt < 12, f"{dt:.1f}s")
    rep.check("给出了可执行的建议", "重新启动本程序" in res["message"] or "手动" in res["message"],
              res["message"])
    rep.info("启动器步骤：" + " | ".join(res["steps"]))


def case_cli_launcher(rep: H.Report) -> None:
    rep.section("命令行：main.py --launcher --no-ui（完整启动器链路）")
    proc, hwnd = H.start_mock("RUNNING")
    try:
        cfg_path = os.path.join(H.OUT, "launcher_cli.json")
        cfg = H.test_config()
        cfg.path = cfg_path
        cfg.set("general.follow_leigod", False, save=False)
        cfg.set("ui.refresh_ms", 300, save=False)
        cfg.save()
        rc, out = _run(["--config", cfg_path, "--launcher", "--no-ui",
                        "--headless-ms", "5000", "--console"], timeout=90)
        rep.check("进程正常结束（退出码 0）", rc == 0, f"rc={rc} | {_one(out, 240)}")
        rep.check("日志里出现启动器步骤", "启动器：" in out, _one(out))
        rep.check("绑定了雷神窗口", "已绑定雷神窗口" in out, _one(out))
        rep.check("引擎正常启动", "保护引擎启动" in out, _one(out))
        rep.check("进入无界面模式", "无界面模式" in out, _one(out))
        rep.check("按 --headless-ms 自动退出", "退出" in out, _one(out))
    finally:
        proc.terminate()


def case_single_instance(rep: H.Report) -> None:
    rep.section("命令行：单实例（第二个实例必须自己退出）")
    proc, hwnd = H.start_mock("RUNNING")
    first = None
    try:
        cfg_path = os.path.join(H.OUT, "launcher_si.json")
        cfg = H.test_config()
        cfg.path = cfg_path
        cfg.set("general.follow_leigod", False, save=False)
        cfg.save()
        args = [PY, os.path.join(ROOT, "main.py"), "--config", cfg_path,
                "--no-ui", "--headless-ms", "12000", "--console"]
        first = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
        # 等第一个实例把互斥体拿稳
        got = False
        for _ in range(40):
            time.sleep(0.5)
            if first.poll() is not None:
                break
            rc2, out2 = _run(["--config", cfg_path, "--no-ui", "--console"], timeout=30)
            if "已有" in out2:
                got = True
                break
        rep.check("第二个实例被拒绝并明确告知", got, "没检测到拒绝提示")
        rep.check("第二个实例退出码为 0（不算错误）", rc2 == 0, f"rc2={rc2} | {_one(out2)}")
        rep.check("第一个实例仍在运行（没被顶掉）", first.poll() is None,
                  f"poll={first.poll()}")
    finally:
        if first:
            first.terminate()
        proc.terminate()


def case_check_no_leigod(rep: H.Report) -> None:
    rep.section("命令行：--check 在没有雷神时也要给结论而不是崩")
    H.kill_stale_mocks()
    cfg_path = os.path.join(H.OUT, "check_none.json")
    cfg = H.test_config()
    cfg.path = cfg_path
    cfg.set("leigod.process_patterns", ["zzz_none_zzz"], save=False)
    cfg.save()
    rc, out = _run(["--config", cfg_path, "--check"], timeout=60)
    rep.check("退出码为 2（表示没找到雷神）", rc == 2, f"rc={rc}")
    rep.check("打印了权限自检", "权限自检" in out, _one(out))
    rep.check("打印了配置状态", "配置" in out and "已校准暂停坐标" in out, _one(out))
    rep.check("如实报告没找到窗口", "没有选中任何窗口" in out or "没有匹配到任何进程" in out,
              _one(out))


def case_spawn_path(rep: H.Report) -> None:
    """启动路径的回归测试 —— 真机上真实踩到过的坑。

    为什么必须专门测这一条：
      case_ensure_with_mock 走的是「雷神已在运行」的早退分支；
      case_ensure_missing_exe 在 os.path.exists 检查处就返回了。
      两者都**没有**真正执行到 subprocess.Popen —— 启动路径是测试盲区。
      于是「DETACHED_PROCESS 与 CREATE_NEW_CONSOLE 互斥，同时传会让
      CreateProcess 返回 WinError 87(参数错误)」这个致命组合一直没被发现，
      直到第一次在真机上启动雷神才暴露（雷神根本拉不起来）。
    """
    import inspect
    rep.section("启动路径：CreateProcess 标志必须合法（真机回归）")
    f = launcher.SPAWN_FLAGS
    rep.check("SPAWN_FLAGS 不同时包含互斥的 DETACHED_PROCESS 与 CREATE_NEW_CONSOLE",
              not (f & launcher.DETACHED_PROCESS and f & launcher.CREATE_NEW_CONSOLE),
              f"flags=0x{f:08X}")

    proc = None
    err = ""
    try:
        proc = subprocess.Popen([PY, "-c", "import time; time.sleep(5)"],
                                creationflags=f, close_fds=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        err = str(e)
    try:
        rep.check("用 SPAWN_FLAGS 真实拉起进程不会报 WinError 87",
                  bool(err) is False and proc is not None and proc.poll() is None,
                  err or f"pid={getattr(proc, 'pid', None)}")
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    src = inspect.getsource(launcher.start_leigod)
    rep.check("start_leigod 确实使用 SPAWN_FLAGS（不会再退回非法组合）",
              "creationflags=SPAWN_FLAGS" in src,
              "已在启动处引用统一常量")


def case_elevated_runner_args(rep: H.Report) -> None:
    """tests/run_elevated.py 的参数切分契约。

    为什么值得单独立一条：这个脚本唯一的副作用是**弹 UAC 并提权启动**，
    参数解析写错的代价不是"报错"，而是**静默起一个不该起的提权进程**——
    用户只会看到一个莫名的 UAC 弹窗，完全不知道是谁弹的、要不要点。
    （实测：`--help` 曾被当作待运行脚本，直接起了一个空的 pythonw。）
    """
    import importlib.util
    rep.section("提权运行器：参数切分（错一次就是一次莫名的 UAC）")

    spec = importlib.util.spec_from_file_location(
        "_re_probe", os.path.join(HERE, "run_elevated.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls = []

    def _fake_launch(argv, wait_file=None, timeout=120.0, label="", console=False):
        calls.append({"argv": list(argv), "wait": wait_file,
                      "timeout": timeout, "console": console})
        return True, "DRY-RUN"

    mod.launch = _fake_launch

    def _last():
        return calls[-1] if calls else {}

    def _case(args, expect_rc, desc, expect_argv=None, expect_wait=None,
              expect_console=None, expect_timeout=None, expect_launch=None):
        # 默认：能跑通（rc=0）就该真的去启动；报错退出（rc≠0）就一次都不许启动。
        if expect_launch is None:
            expect_launch = (expect_rc == 0)
        n = len(calls)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(args)
        launched = len(calls) > n
        ok = (rc == expect_rc)
        detail = f"rc={rc}（期望 {expect_rc}）"
        if expect_launch:
            ok = ok and launched
            d = _last() if launched else {}
            if expect_argv is not None:
                ok = ok and d.get("argv") == expect_argv
                detail += f"；argv={d.get('argv')}"
            if expect_wait is not None:
                got = os.path.basename(str(d.get("wait") or ""))
                ok = ok and got == os.path.basename(expect_wait)
                detail += f"；wait={got}"
            if expect_console is not None:
                ok = ok and d.get("console") is expect_console
                detail += f"；console={d.get('console')}"
            if expect_timeout is not None:
                ok = ok and d.get("timeout") == expect_timeout
                detail += f"；timeout={d.get('timeout')}"
        else:
            ok = ok and not launched       # 不该启动的一次都不能启动
            if launched:
                detail += "；⚠️ 却启动了提权进程"
        rep.check(desc, ok, detail)

    _case(["--help"], 0, "--help 只打用法，绝不触发提权启动", expect_launch=False)
    _case(["-h"], 0, "-h 同上", expect_launch=False)
    _case([], 2, "没给脚本 → 报错退出，不弹 UAC")
    _case(["notapy"], 2, "目标不是 .py → 拒绝，不弹 UAC")
    _case(["--bogus", "--", "tests/get_real_state.py"], 2,
          "无法识别的开关 → 拒绝，不弹 UAC（旧版会把它当脚本名去提权）")
    _case(["--timeout"], 2, "--timeout 缺值 → 报错，不弹 UAC")
    _case(["--wait"], 2, "--wait 缺值 → 报错，不弹 UAC")
    _case(["--console", "--timeout", "300", "--wait", "tests/out/elev_x.json",
           "--", "tests/get_real_state.py", "--out", "real_state"],
          0, "完整写法：开关正确剥离，子脚本参数原样透传",
          expect_argv=["tests/get_real_state.py", "--out", "real_state"],
          expect_wait="elev_x.json", expect_console=True, expect_timeout=300.0)
    _case(["--wait", "tests/out/elev_x.json", "--",
           "tests/get_real_state.py", "-h"], 0,
          "子脚本自己的 -h 不被本脚本抢走", expect_argv=["tests/get_real_state.py", "-h"])
    _case(["--wait", "tests/out/elev_x.json", "--",
           "tests/get_real_state.py", "--wait", "5"], 0,
          "子脚本自己的 --wait 不被本脚本抢走",
          expect_argv=["tests/get_real_state.py", "--wait", "5"])
    _case(["tests/get_real_state.py", "--out", "real_state"], 0,
          "不带 -- 的老写法仍然可用",
          expect_argv=["tests/get_real_state.py", "--out", "real_state"])


def case_verify_script_help(rep: H.Report) -> None:
    """verify_close_loop_real.py 的 --help 必须干净退出，不许被误判成崩溃。

    踩过的坑：崩溃兜底 `except BaseException` 把 argparse 的 `--help`（其实是
    SystemExit(0)）当成了崩溃，往 `tests/out/verify_close_loop.error.txt` 里写了
    一坨 traceback、还返回非 0。这个脚本本就是真机排查「点了 ✕ 没反应」的工具，
    用户很可能先跑 `--help` 看用法 —— 那条路必须干净。
    """
    rep.section("真机闭环脚本：--help 是正常退出，不是崩溃")
    script = os.path.join(ROOT, "tests", "verify_close_loop_real.py")
    err_file = os.path.join(ROOT, "tests", "out", "verify_close_loop.error.txt")
    try:
        if os.path.exists(err_file):
            os.remove(err_file)
    except OSError:
        pass
    try:
        p = subprocess.run([PY, script, "--help"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        out = (p.stdout or "") + (p.stderr or "")
        rep.check("--help 退出码 = 0（argparse 的正常 SystemExit，不是崩溃）",
                  p.returncode == 0, f"rc={p.returncode}")
        rep.check("--help 打印了用法（usage:）",
                  "usage:" in out, _one(out, 120))
        rep.check("--help 不产生崩溃证据文件 error.txt",
                  not os.path.exists(err_file),
                  f"error.txt 存在={os.path.exists(err_file)}")
    finally:
        try:
            if os.path.exists(err_file):
                os.remove(err_file)
        except OSError:
            pass


def main() -> int:
    from core.logging_setup import setup_logging
    setup_logging("INFO", console=False)
    rep = H.Report("启动器流程与命令行入口 —— 自动化验收")
    print(rep.title, flush=True)
    H.kill_stale_mocks()
    case_spawn_path(rep)
    case_ensure_with_mock(rep)
    case_ensure_missing_exe(rep)
    case_cli_launcher(rep)
    case_single_instance(rep)
    case_check_no_leigod(rep)
    case_elevated_runner_args(rep)
    case_verify_script_help(rep)
    path = rep.save("launcher_report.txt")
    fails = sum(1 for l in rep.lines if "[FAIL]" in l)
    print(f"\n结果：{'全部通过' if fails == 0 else f'{fails} 项失败'}；报告 {path}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
