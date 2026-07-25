"""
📊 日志模块 — 统一日志配置

用法:
  from logger import get_logger
  log = get_logger("ra.graph")
  log.info("tokens: +%d", total)
  log.debug("payload: %s", payload)
"""

import logging
import sys


def setup_logging(debug: bool = False):
    """配置根日志器。debug=True 时输出 DEBUG 级别。"""
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    datefmt = "%H:%M:%S"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root = logging.getLogger("ra")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ra.{name}")
