"""日志：滚动文件 + 控制台，并强制屏蔽账户敏感信息（规格书 §二十七/§二十八）。

本项目本身不接触 account_token / Cookie / 密码，
但只要日志里出现了这些词，就必须在写盘前被替换掉，避免误传。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re

from core.paths import base_dir

BASE_DIR = base_dir()
LOG_DIR = os.path.join(BASE_DIR, "logs")

#: 命中即整段打码的敏感键名
_SENSITIVE_KEYS = ("password", "passwd", "pwd", "cookie", "token", "authorization",
                   "account_token", "session", "secret", "signature")
# 顺序很重要：Bearer / Cookie 这两条**必须排在通用 key:value 之前**。
# 否则通用规则会把 "Authorization: Bearer xxx" 里的 "Bearer" 当成值先吃掉，
# 剩下 "xxx" 裸露在日志里，Bearer 规则就再也匹配不上了。
_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)\b(?:cookie|set-cookie)\b\s*[:=]\s*[^\r\n]+"),
    re.compile(r"(?i)\b(" + "|".join(_SENSITIVE_KEYS) + r")\b\s*[:=]\s*([^\s,;\"']+)"),
]

_logger = None


def sanitize(text: str) -> str:
    out = str(text)
    for p in _PATTERNS:
        out = p.sub(lambda m: (m.group(1) + "=<redacted>") if m.lastindex else "<redacted>", out)
    return out


class _SanitizingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(sanitize(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: (sanitize(v) if isinstance(v, str) else v)
                           for k, v in record.args.items()}
        return True


def setup_logging(level: str = "INFO", console: bool = True) -> logging.Logger:
    """初始化全局 logger（幂等）。"""
    global _logger
    if _logger is not None:
        return _logger
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("guard")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    flt = _SanitizingFilter()

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "guard.log"), maxBytes=2 * 1024 * 1024,
        backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(flt)
    logger.addHandler(fh)

    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(flt)
        logger.addHandler(sh)

    _logger = logger
    return logger


def get_logger() -> logging.Logger:
    return _logger if _logger is not None else setup_logging()
