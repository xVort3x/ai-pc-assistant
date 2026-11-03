"""Logging setup.

One rotating file, never a console handler by default (the palette has no
console).  Repeated ``Assistant`` construction reuses the same logger instead
of stacking handlers.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "pcassistant"
_CONFIGURED = False


def setup_logging(log_dir: Path, *, level: str | None = None) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)

    if _CONFIGURED:
        return logger

    requested = (level or os.environ.get("PCASSISTANT_LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, requested, logging.INFO))
    logger.propagate = False

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_dir / "assistant.log",
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
    except OSError:
        handler = logging.NullHandler()

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    _CONFIGURED = True
    return logger
