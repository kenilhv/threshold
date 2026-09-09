"""Structured logging.

Deliberately named ``observability`` and not confused with it. Logs are one of
three signals and on their own they are not observability — the distinction
matters enough that M5 adds traces and metrics rather than more log lines.

Two properties this configuration guarantees:

* JSON in every environment except a developer's terminal, because a log line a
  machine cannot parse is a log line nobody queries.
* A redaction processor that drops anything classified RESTRICTED, using the
  same field classification the review projection consumes. One implementation,
  not three.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from threshold.config.settings import Settings

#: Keys that must never reach a log sink regardless of how they were bound.
_REDACTED_KEYS = frozenset(
    {
        "account_number",
        "routing_number",
        "tax_id",
        "password",
        "secret",
        "token",
        "authorization",
        "api_key",
        "s3_secret_access_key",
    }
)

_REDACTED = "<restricted>"


def _redact(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(settings: Settings) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact,
    ]

    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.environment == "local"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        # stdlib rather than PrintLogger: `add_logger_name` reads `logger.name`,
        # which only a stdlib logger has, and routing through stdlib is what
        # lets uvicorn and sqlalchemy share this pipeline instead of printing
        # their own format alongside it.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route uvicorn and sqlalchemy through the same pipeline so one request does
    # not produce two log formats.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=settings.log_level)
    for noisy in ("uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True
