"""路径解析：区分「源码运行」与「打包成 exe 运行」。

为什么必须有这个文件（打包后才会暴露的坑）：
  源码运行时 `__file__` 指向项目目录，一切正常；
  但用 PyInstaller 打成单文件 exe 后，`__file__` 指向的是**临时解包目录**
  （%TEMP%\\_MEIxxxxx），程序一退出就被删掉。
  如果配置和日志按 `dirname(__file__)` 落盘，用户会发现：
    「我改了 config.json 重启就没了」「日志目录是空的」。
  所以打包形态下一律以 **exe 所在目录** 为基准。
"""
from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_dir() -> str:
    """配置 / 日志的基准目录（可写目录）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dir() -> str:
    """只读资源目录（打包后是解包目录，源码运行时与 base_dir 相同）。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", base_dir())
    return base_dir()


def ensure_writable_hint(path: str) -> str:
    """返回一个给用户看的说明：配置/日志当前落在哪里，以及是否可写。"""
    ok = os.access(path, os.W_OK) if os.path.isdir(path) else os.access(
        os.path.dirname(path) or ".", os.W_OK)
    tip = "可写" if ok else "不可写（请把程序放到有写权限的目录，或以管理员身份运行）"
    return f"{path} —— {tip}"
