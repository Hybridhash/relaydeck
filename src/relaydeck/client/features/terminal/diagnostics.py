"""Content-free terminal decision log (shared by hooks and sessions, Flet-free).

Lines name enum-like sites, booleans and numbers only: never ids, draft text,
output bytes or agent names. Logger `relaydeck.client.typing_split` keeps the
historical name the owner's server log already greps for.
"""

from __future__ import annotations

import logging
import sys

_DIAG = logging.getLogger("relaydeck.client.typing_split")


def diagnostics_logger() -> logging.Logger:
    if not _DIAG.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _DIAG.addHandler(handler)
        _DIAG.setLevel(logging.INFO)
        _DIAG.propagate = False
    return _DIAG
