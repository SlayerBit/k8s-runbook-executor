"""
logging_config.py — Structured logging setup.

Supports two formats:
  json  → machine-readable, ideal for log aggregators (Loki, Stackdriver, etc.)
  text  → human-readable, useful for local development
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict


class _JsonFormatter(logging.Formatter):
    """Emit one-line JSON per log record."""

    # All attributes that exist on every LogRecord — we never re-emit these
    # as extra fields because they are already captured in the structured keys
    # above (timestamp, level, logger, message) or are internal Python noise.
    RESERVED_KEYS: frozenset = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    ) | frozenset(
        [
            "message", "asctime", "exc_text",  # added by Formatter.format()
            "stack_info",                        # Python 3.5+
            "taskName",                          # Python 3.12+
        ]
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach only caller-supplied extra fields
        for key, val in record.__dict__.items():
            if key not in self.RESERVED_KEYS and not key.startswith("_"):
                payload[key] = val

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    DATEFMT = "%Y-%m-%dT%H:%M:%SZ"
    FMT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self.FMT, datefmt=self.DATEFMT)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """
    Configure the root logger.

    Call this once at application startup before any other imports.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    formatter: logging.Formatter
    if fmt.lower() == "json":
        formatter = _JsonFormatter()
    else:
        formatter = _TextFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Remove any pre-existing handlers (e.g. from previous calls)
    root.handlers.clear()
    root.addHandler(handler)

    # Silence noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger — use this everywhere instead of logging.getLogger."""
    return logging.getLogger(name)
