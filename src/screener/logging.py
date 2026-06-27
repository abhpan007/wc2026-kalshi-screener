"""structlog configuration.

Human-readable console output locally; JSON in Lambda (set ``log_json=True``)
so CloudWatch can index fields.
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(*, json: bool = False, level: int = logging.INFO) -> None:
    """Idempotent structlog setup. Call once at process start."""
    logging.basicConfig(format="%(message)s", level=level)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
