"""Centralised logging configuration.

Logs are written to stderr (so they never corrupt stdout / progress bars) and,
when an output directory is supplied, also to ``output/run.log``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False
LOGGER_NAME = "github_contrib"


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> logging.Logger:
    """Configure and return the package root logger.

    Safe to call multiple times; handlers are only attached once.
    """
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(numeric_level)

    if _CONFIGURED:
        # Allow the level to be adjusted on subsequent calls without dupe handlers.
        for handler in logger.handlers:
            handler.setLevel(numeric_level)
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(numeric_level)
    logger.addHandler(stream_handler)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(fmt)
            file_handler.setLevel(logging.DEBUG)
            logger.addHandler(file_handler)
        except OSError:
            logger.warning("Could not open log file %s; continuing without it.", log_file)

    # Quieten noisy third-party libraries.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger of the package logger."""
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)
