"""结构化日志配置。

用法:
    from parsing_core.log import get_logger
    log = get_logger(__name__)
    log.info("ocr_page", page=3, status="success")
    log.error("pipeline_failed", exc_info=True)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

LOG_LEVEL = os.environ.get("PDF2MD_LOG_LEVEL", "INFO")
LOG_FILE = os.environ.get("PDF2MD_LOG_FILE", "")

LOGGER_CACHE: dict[str, logging.Logger] = {}


class _StructuredFormatter(logging.Formatter):
    """JSON 行格式，便于日志聚合工具解析。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["exc"] = str(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    """获取具名 logger，自动缓存。"""
    if name in LOGGER_CACHE:
        return LOGGER_CACHE[name]
    log = logging.getLogger(name)
    log.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    log.propagate = False

    # 避免重复添加 handler
    if not log.handlers:
        _setup_handlers(log)
    LOGGER_CACHE[name] = log
    return log


def _setup_handlers(log: logging.Logger) -> None:
    fmt = _StructuredFormatter()
    # stderr handler
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    # file handler（可选）
    if LOG_FILE:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def _install_root_handler() -> None:
    """给项目中未显式初始化的任意 logger 提供一个默认 handler。"""
    root = logging.getLogger("parsing_core")
    if not root.handlers:
        _setup_handlers(root)
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))


# 首次 import 即生效
_install_root_handler()
