"""One logging configuration for the companion, the gateway and the connector.

Server side only: the phone and browser client never import this module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from relaydeck.gateway.request_id import RequestIdFilter

_QUERY_REDACTED = "?[redacted]"
_QUERY_URL = re.compile(r"(?P<path>/[^\s?\"'<>]*)\?[^\s\"'<>]*")
FORMAT = "%(asctime)s %(levelname)s %(name)s rid=%(request_id)s %(message)s"


def _redact_query(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    # Uvicorn's request-target argument is one whole string. Drop its entire
    # query, including hostile quotes/whitespace, without altering the path.
    if value.startswith(("/", "http://", "https://", "ws://", "wss://")) and "?" in value:
        return value.partition("?")[0] + _QUERY_REDACTED
    return _QUERY_URL.sub(lambda match: match["path"] + _QUERY_REDACTED, value)


class _UvicornQueryFilter(logging.Filter):
    """Redact before any own/ancestor handler sees a Uvicorn request record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_query(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_query(arg) for arg in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {key: _redact_query(value) for key, value in record.args.items()}
        if hasattr(record, "message"):
            record.message = record.getMessage()
        return True


def logging_config(level: str = "INFO") -> dict[str, Any]:
    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_queries"] = {"()": _UvicornQueryFilter}
    config["filters"]["request_id"] = {"()": RequestIdFilter}
    config["formatters"]["relaydeck"] = {"format": FORMAT}
    config["handlers"]["default"]["formatter"] = "relaydeck"
    config["handlers"]["default"]["filters"] = ["request_id"]
    for name in ("uvicorn.error", "uvicorn.access"):
        config["loggers"][name].setdefault("filters", []).append("redact_queries")
    for name, own_level in (
        ("relaydeck.client.features.terminal.pictures", "INFO"),
        ("relaydeck.lease_lifecycle", "INFO"),
        ("relaydeck.gateway.loop_watch", "WARNING"),
    ):
        config["loggers"][name] = {
            "level": own_level,
            "handlers": ["default"],
            "propagate": False,
        }
    # The wider gateway logger keeps propagating, so a root handler (tests) still sees it.
    config["loggers"]["relaydeck.gateway"] = {
        "level": level,
        "handlers": ["default"],
        "propagate": True,
    }
    return config
