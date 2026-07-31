"""structlog configuration.

Console rendering when stderr is a terminal, JSON otherwise (systemd, cron).
The redaction processor is installed early in the chain — before any renderer
and before any exception formatting — so that nothing downstream can emit an
unscrubbed value.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

import structlog
from structlog.typing import EventDict, WrappedLogger

from pricewatch.redaction import redact

LogLevel = Literal["debug", "info", "warning", "error"]


def redaction_processor(
    logger: WrappedLogger,  # noqa: ARG001 - structlog processor signature
    method_name: str,  # noqa: ARG001 - structlog processor signature
    event_dict: EventDict,
) -> EventDict:
    """Scrub every value in the event dict.

    Placed ahead of the renderers so it also covers values that were bound to
    the logger's context far from the call site.
    """
    scrubbed: dict[str, Any] = redact(dict(event_dict))
    return scrubbed


def configure_logging(level: LogLevel = "info", *, force_json: bool | None = None) -> None:
    """Install the processor chain. Idempotent; safe to call more than once."""
    use_json = not sys.stderr.isatty() if force_json is None else force_json

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Everything above may have *added* strings to the event dict
            # (notably the formatted traceback), so redaction runs after them
            # and before anything that emits.
            redaction_processor,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
