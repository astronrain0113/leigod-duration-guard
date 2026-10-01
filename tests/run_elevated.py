"""以管理员身份运行指定的 Python 脚本（会弹一次 UAC）。

为什么需要它：雷神以 requireAdministrator 运行，凡是要
  · 把雷神窗口置前（SetForegroundWindow / AttachThreadInput）
  · 向雷神发消息（SendMessage / PostMessage）
  · 改动雷神窗口（EnableMenuItem / SetWindowLong）
  · 向雷神注入合成输入（SendInput）
的操作，**不提权一律被 UIPI 静默拒绝**，而且往往不报错、只给出错误结果。
（实测：同一探针未提权时 WM_NCHITTEST 全返回 0，提权后返回 1。）

用法：
    python tests/run_elevated.py --wait <输出文件> -- <脚本> [参数...]
    python tests/run_elevated.py --wait out/state.json -- tests/get_real_state.py --out out
    python tests/run_elevated.py --timeout 300 --console --wait out/x.json -- tests/需要真人操作的脚本.py

说明：
  · 子进程默认用 pythonw.exe 启动（无控制台黑框），因此子脚本**必须把结果写文件**，
    不能依赖 stdout。--wait 指定要等的文件，按 mtime 变化判断完成。
  · **需要真人配合的脚本请加 `--console`**：改用 python.exe 并正常显示窗口，
    子脚本的 print/提示会实时出现在控制台里。否则用户全程看不到任何提示
    （本项目的真机闭环验证就属于这种情况：它要提示用户「现在请点一次 ✕」）。
  · `--timeout` 是**父进程等待子进程**的上限（秒，默认 120）。需要真人点击的脚本
    往往比 120s 长，此时父进程会先报超时、而子进程仍在跑 —— 那种"看起来失败、
    其实还在等"的现象很难判断，所以这类场景务必显式把 --timeout 设得比子脚本的
    `--wait` 更宽裕。
  · 子进程若崩溃，不用 --console 时不会留下控制台信息；请让子脚本自己写错误日志。
"""
import ctypes
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PY_DIR = os.environ.get("WORKBUDDY_PYTHON_DIR",
                        os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 ".workbuddy", "binaries", "python", "envs", "default"))
PYW = os.path.join(PY_DIR, "Scripts", "pythonw.exe")      # 无控制台
PY = os.path.join(PY_DIR, "Scripts", "python.exe")        # 带控制台


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def launch(argv, wait_file=None, timeout=120.0, label="", console=False):
    """提权运行脚本；等待 wait_file 更新。返回 (ok, message)。

    `console=True` 时改用 python.exe 并以正常窗口显示：需要真人配合的脚本
    必须这样启动，否则它打印的提示会被完全丢弃，用户不知道什么时候该做什么。
    """
    exe = PY if console else PYW
    if not os.path.exists(exe):
        return False, f"找不到解释器：{exe}"

    t0 = mtime(wait_file) if wait_file else 0.0
    args = " ".join(f'"{a}"' if " " in a else a for a in argv)
    kind = "带控制台" if console else "无控制台"
    print(f"[{label}] 请求管理员权限（UAC 请点「是」）…（{kind}）", flush=True)

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_wchar_p,
                                      ctypes.c_wchar_p, ctypes.c_int]
    # nShowCmd：无控制台用 SW_HIDE(0)；带控制台必须给 SW_SHOWNORMAL(1)，
    # 否则控制台窗口不会显示，--console 就白加了。
    rc = shell32.ShellExecuteW(None, "runas", exe, args, ROOT, 1 if console else 0)
    code = int(rc) if rc else 0
    if code <= 32:
        return False, f"启动被拒绝或失败（ShellExecuteW 返回 {code}；5=UAC 被取消）"

    if not wait_file:
        return True, "已启动（未指定 --wait，无法确认完成）"

    deadline = time.time() + timeout
    while time.time() < deadline:
        if mtime(wait_file) > t0 + 0.001:
            return True, f"完成，输出已更新：{wait_file}"
        time.sleep(1.0)
    return False, f"等待超时（{timeout:.0f}s）：{wait_file} 未更新"


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])

    # 先按 `--` 把「本脚本自己的开关」和「子脚本及其参数」切开。
    # 必须先切：否则 `-- tests/x.py -h` 里的 `-h` 会被当成我们自己的开关，
    # 而 `-- tests/x.py --wait 5` 又会把子脚本的 --wait 抢走。
    if "--" in argv:
        cut = argv.index("--")
        own, child = argv[:cut], argv[cut + 1:]
    else:
        own, child = argv, []

    wait_file = None
    timeout = 120.0
    console = False
    child = list(child)

    if "-h" in own or "--help" in own:
        print(__doc__)
        return 0
    if "--console" in own:
        console = True
        own.remove("--console")
    if "--wait" in own:
        i = own.index("--wait")
        if i + 1 >= len(own):
            print("--wait 需要一个输出文件路径，例如 --wait tests/out/x.json")
            return 2
        wait_file = own[i + 1]
        del own[i:i + 2]
    if "--timeout" in own:
        i = own.index("--timeout")
        try:
            timeout = max(5.0, float(own[i + 1]))
        except (IndexError, ValueError):
            print("--timeout 需要一个秒数，例如 --timeout 300")
            return 2
        del own[i:i + 2]

    # 不带 `--` 的老写法：剩下的第一个 token 就是子脚本，其余是它的参数。
    if not child and own:
        child, own = own, []

    if own:
        print(f"无法识别的开关：{' '.join(own)}\n")
        print(__doc__)
        return 2
    if not child:
        print("没有指定要运行的脚本。\n")
        print(__doc__)
        return 2
    # 目标必须是 .py 文件：拦住把开关名、目录名之类误当作脚本的情况
    # （这类误传会静默弹出一次 UAC，用户完全不知道发生了什么）。
    if not child[0].lower().endswith(".py"):
        print(f"第一个参数应当是要运行的 .py 脚本，收到：{child[0]}\n")
        print(__doc__)
        return 2
    if wait_file and not os.path.isabs(wait_file):
        wait_file = os.path.join(ROOT, wait_file)

    ok, msg = launch(child, wait_file=wait_file, timeout=timeout, label="elevated",
                     console=console)
    print(msg, flush=True)
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
