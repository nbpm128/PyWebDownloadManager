from __future__ import annotations

import logging
import os.path
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

FILE_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

CONSOLE_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s"


def setup_logger(
    *,
    level: str = "DEBUG",
    log_dir: Path = LOG_DIR,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> None:
    """Configure the root logger with console + file handlers."""

    numeric_level = logging.getLevelName(level.upper())

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any pre-existing handlers to avoid duplicates on re-init
    root.handlers.clear()

    formatter = logging.Formatter(fmt=FILE_FORMAT, datefmt=DATE_FORMAT)
    console_formatter = logging.Formatter(fmt=CONSOLE_FORMAT, datefmt=DATE_FORMAT)

    # --- Console handler ---
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    # --- app.log: all messages ---
    app_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    app_handler.setLevel(numeric_level)
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    # --- errors.log: ERROR and above only ---
    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "errors.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)

    # Quieten noisy third-party loggers if needed
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

    logging.getLogger(__name__).info(
        "Logger initialised | level=%s | log_dir=%s | max_bytes=%s | backup_count=%s",
        level, log_dir, max_bytes, backup_count,
    )