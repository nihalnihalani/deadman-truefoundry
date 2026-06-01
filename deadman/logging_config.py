"""DEADMAN structured JSON logging + correlation-id support.

Usage
-----
    from deadman.logging_config import configure_logging, set_correlation_id

    configure_logging()         # call once at startup (idempotent)
    set_correlation_id("inc-42")  # call at the start of an incident

After ``configure_logging()`` every log record is emitted as a single JSON
line with: timestamp (ISO-8601), level, logger, message, and any ``extra``
fields passed by the caller.  When a correlation id is set via
``set_correlation_id()``, it is automatically injected into every record for
the current thread/task.

No external dependencies — uses only stdlib ``logging``, ``json``, and
``contextvars``.
"""
from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional


# ── Correlation-id context variable ───────────────────────────────────────────

_correlation_id: ContextVar[Optional[str]] = ContextVar("deadman_correlation_id", default=None)


def set_correlation_id(incident_id: Optional[str]) -> None:
    """Set (or clear) the correlation id for the current context.

    Call this at the start of each incident to tag all subsequent log records
    with the incident id.  Call with ``None`` to clear.
    """
    _correlation_id.set(incident_id)


def get_correlation_id() -> Optional[str]:
    """Return the current correlation id, or None if not set."""
    return _correlation_id.get()


# ── Logging filter that injects the correlation id ────────────────────────────

class _CorrelationIdFilter(logging.Filter):
    """Injects the current correlation_id into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()  # type: ignore[attr-defined]
        return True


# ── JSON formatter ─────────────────────────────────────────────────────────────

class _JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON.

    Fields emitted:
      timestamp   ISO-8601 UTC
      level       DEBUG / INFO / WARNING / ERROR / CRITICAL
      logger      record.name
      message     the formatted message (with % substitution applied)
      correlation_id (when set)
      + any keys passed via ``extra={}``
    """

    # Keys added by logging internally that we do NOT want to re-emit as extras.
    _SKIP = frozenset({
        "args", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs",
        "msg", "name", "pathname", "process", "processName", "relativeCreated",
        "stack_info", "thread", "threadName", "correlation_id",
        # added by our filter:
    })

    def format(self, record: logging.LogRecord) -> str:
        # Apply % substitution so record.message is the final string.
        record.message = record.getMessage()

        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }

        # Correlation id (may be None — omit the key rather than emit null).
        cid = getattr(record, "correlation_id", None)
        if cid is not None:
            payload["correlation_id"] = cid

        # Exception info.
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        # Any extra fields passed by the caller via extra={...}.
        for key, value in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                try:
                    json.dumps(value)   # only include JSON-serialisable values
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = str(value)

        return json.dumps(payload, ensure_ascii=False)


# ── configure_logging — idempotent ────────────────────────────────────────────

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Install JSON formatter + correlation-id filter on the root logger.

    Idempotent: calling multiple times (e.g. from webhook.py and from tests) is
    safe — subsequent calls are no-ops.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level)

    # If no handlers yet, add a StreamHandler (stderr).
    if not root.handlers:
        handler = logging.StreamHandler()
        root.addHandler(handler)

    formatter = _JSONFormatter()
    cid_filter = _CorrelationIdFilter()

    for handler in root.handlers:
        handler.setFormatter(formatter)
        handler.addFilter(cid_filter)

    _CONFIGURED = True
