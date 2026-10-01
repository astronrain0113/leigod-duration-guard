# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：图形版 LeigodGuard.exe（不弹控制台窗口）。

用法（在项目根目录执行）：
    pyinstaller --clean --noconfirm packaging/LeigodGuard.spec
产物：dist/LeigodGuard/LeigodGuard.exe（onedir，便于放配置与日志）

为什么用 onedir 而不是 onefile：
   本项目需要把 config.json 与 logs/ 放在**程序旁边的可写目录**里。
   onefile 每次运行都会解包到 %TEMP%\\_MEIxxxxx，虽然代码里已按 exe 目录
   解析路径（core/paths.py），但 onedir 启动更快、杀软误报更少、排障更容易。

关于 Qt 与 OCR 的隐藏依赖：PySide6 有官方 hook；uiautomation / pywin32 /
rapidocr_onnxruntime 需要手动列入 hiddenimports，否则运行期才会 ModuleNotFoundError。
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# ---- OCR 模型必须显式收进来（真机事故 2026-09-30）----
# `hiddenimports` 里写上 `rapidocr_onnxruntime` 只会把**代码**打进 PYZ，
# 而它的 3 个 .onnx 模型与 4 个 config.yaml 是**数据文件**，一个都不会进来。
# 后果极具误导性：`import rapidocr_onnxruntime` 成功 → `engine_available()`
# 返回 True（旧实现只检查能否 import）→ 但 `RapidOCR()` 构造时找不到模型、
# 抛异常被 `except` 吞掉 → 此后每一轮 OCR 都**静默返回空列表**。
# 表现是「OCR 永远认不出文字」，而 --check 却报「OCR 可用」。
# 实测证据：`LeigodGuardCUI.exe --inspector` 对一张**能正常识别出 8 行文字**
# 的顶栏截图报 `ocr.lines = []`；同一张图在源码环境识别出「开启时长」。
_OCR_DATAS = collect_data_files("rapidocr_onnxruntime")
# 子模块也必须显式收：RapidOCR 是**按 YAML 动态 import** 子模块的
# （`ch_ppocr_v3_det.text_detect` / `ch_ppocr_v3_rec.text_recognize` /
#  `ch_ppocr_v2_cls.text_cls`），PyInstaller 的静态分析看不到这些调用。
# 只写 hiddenimports=["rapidocr_onnxruntime"] 时，运行期报
# `AttributeError: module 'ch_ppocr_v3_det' has no attribute 'TextDetector'`
# —— 真机 2026-09-30 实测，正是这一句让打包版 OCR 彻底空转。
_OCR_SUBMODULES = collect_submodules("rapidocr_onnxruntime")

# uiautomation 的客户端 DLL 是按 <包目录>/bin 相对路径加载的，
# 不显式收进来的话，打包后 import 不会报错，但第一优先级识别会静默失效
# （运行时表现为「UI Automation 不可用，只能退回坐标点击」）。
# 因此这里把 DLL 同时放到两处：模块自身的 bin\ 与打包根目录。
_uia_pkg = os.path.dirname(__import__("uiautomation").__file__)
_uia_bin = os.path.join(_uia_pkg, "bin")
uia_binaries = []
if os.path.isdir(_uia_bin):
    for _f in sorted(os.listdir(_uia_bin)):
        if _f.lower().endswith(".dll"):
            _p = os.path.join(_uia_bin, _f)
            uia_binaries.append((_p, "uiautomation/bin"))
            uia_binaries.append((_p, "."))

hidden = [
    "uiautomation", "comtypes", "comtypes.stream",
    "win32gui", "win32ui", "win32api", "win32process", "win32con",
    "psutil", "PIL.Image", "PIL.ImageGrab", "PIL.ImageDraw",
    "rapidocr_onnxruntime", "onnxruntime",
    "core.protection_engine", "core.state_machine", "core.events",
    "core.config", "core.logging_setup", "core.paths",
    "detection.coordinate_fallback", "detection.ui_automation",
    "detection.ocr", "detection.image_detection",
    "leigod.window", "leigod.duration_detector", "leigod.duration_controller",
    "leigod.close_protection", "game.process_monitor",
    "launcher.launcher", "ui.app", "ui.guard_window", "ui.tray",
    "ui.notifications", "ui.theme",
    "diagnostics.calibration", "diagnostics.ui_inspector",
    "diagnostics.window_inspector",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=uia_binaries,
    # 不打包 config/：首次运行会自动生成，放外面用户才看得见。
    # 但 OCR 的模型/配置必须打进去（见上面 _OCR_DATAS 的说明）。
    datas=list(_OCR_DATAS),
    hiddenimports=hidden + _OCR_SUBMODULES,
    hookspath=[],
    runtime_hooks=[],
    # 这些是测试与开发期才用到的重依赖，打进去只会让体积暴涨
    excludes=["tkinter", "matplotlib", "pandas", "scipy", "pytest",
              "PySide6.QtWebEngineCore", "PySide6.QtQuick", "PySide6.Qt3DCore"],
    noarchive=False,
)
# ---- 体积瘦身：砍掉「打包进来了但永远不会加载」的大件 ----
# 610MB 的产物里这几项占了近 60MB：
#   opengl32sw.dll          Qt 的软件 OpenGL（只用 Widgets，不用 QML/3D）
#   opencv_videoio_ffmpeg   opencv 的编解码 DLL（只用 cv2 做缩放/二值化）
#   _avif                   Pillow 的 AVIF 插件（截图是 PNG/BMP，用不到）
#   Qt6Quick/Qt6Qml 系列    QML 相关（本程序纯 Widgets）
# 注意： 对 **Qt 的 DLL** 不起作用 —— 它们由 PyInstaller 的 Qt hook
# 直接收进 binaries，只能在 Analysis 之后按名字过滤。
_SLIM_EXCLUDE = (
    "opengl32sw",
    "opencv_videoio_ffmpeg",
    "_avif",
    "Qt6Quick", "Qt6Qml", "Qt6Quick3D", "Qt6ShaderTools", "Qt6QuickWidgets",
    # ---- 2026-09-30 第二轮精简（逐项确认过"程序不会加载"）----
    # .qm            Qt 的 96 个翻译文件（6.4MB）。本程序界面全中文硬编码，不做国际化。
    # Qt6Pdf         PDF 渲染（4.4MB）—— 没有任何 PDF 代码路径。
    # Qt6OpenGL      QWidgets 走 raster 后端，不用 OpenGL 渲染（1.9MB）。
    # Qt6Network     不联网（骨架许可与总时长保护都只用本地 API）。Qt6Gui 不需要它。
    # Qt6VirtualKeyboard  虚拟键盘（0.4MB）—— 只有触摸屏输入需要。
    # 图像格式插件    只处理内存里的 numpy 数组，不加载 jpg/webp/tiff 文件（1.8MB）。
    # qdirect2d      Qt 的 Direct2D 平台后端，实际用的是 qwindows（1.0MB）。
    # TLS 后端       不联网就不需要 OpenSSL/SChannel 后端（0.6MB）。
    ".qm", "Qt6Pdf", "Qt6OpenGL", "Qt6Network", "Qt6VirtualKeyboard",
    "qdirect2d", "qwebp", "qtiff", "qjpeg",
    "qopensslbackend", "qschannelbackend", "qnetworklistmanager",
)


def _slim(toc):
    return [t for t in toc
            if not any(k.lower() in str(t[0]).lower() for k in _SLIM_EXCLUDE)]


a.binaries = _slim(a.binaries)
a.datas = _slim(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LeigodGuard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX 压缩会显著增加杀软误报，得不偿失
    console=False,                  # 图形版：不弹黑框
    # 清单里写入 requireAdministrator：启动时弹一次 UAC 以取得管理员权限。
    # 为什么必须这样：雷神的 leigod.exe / leigod_launcher.exe 清单本身就是
    # requireAdministrator（实测），所以雷神**永远**以高完整性级别运行。
    # 若本程序不提权，Windows 的 UIPI 会静默拒绝禁用雷神的系统菜单 SC_CLOSE，
    # 表现为「✕ 看起来灰了，其实还能点」——关闭保护等于没开。
    uac_admin=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "packaging", "leigod_guard.ico")
    if os.path.exists(os.path.join(ROOT, "packaging", "leigod_guard.ico")) else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LeigodGuard",
)
