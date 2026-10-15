"""Opt-in synthetic lifecycle evidence. No terminal content or user labels."""

import logging
import os


def trace_lifecycle(event: str, **fields: int | bool) -> None:
    if os.environ.get("RELAYDECK_TERMINAL_DIAGNOSTICS") == "1":
        logging.getLogger("relaydeck.terminal.lifecycle").warning(
            "terminal_lifecycle %s %s", event, fields
        )
