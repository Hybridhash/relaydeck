"""Format readers, keyed by format id. Each reads owner-allowlisted roots, same guarantees."""

from __future__ import annotations

from collections.abc import Mapping

from relaydeck.usage.contracts import Reader
from relaydeck.usage.readers.json_session_credits import READER as _CREDITS
from relaydeck.usage.readers.json_session_turns import READER as _TURNS
from relaydeck.usage.readers.jsonl_cumulative_rollout import READER as _ROLLOUT
from relaydeck.usage.readers.jsonl_event_usage import READER as _EVENTS
from relaydeck.usage.readers.jsonl_message_usage import READER as _MESSAGES
from relaydeck.usage.readers.sqlite_session_usage import READER as _SQLITE

_ALL: tuple[Reader, ...] = (_MESSAGES, _ROLLOUT, _EVENTS, _TURNS, _SQLITE, _CREDITS)
READERS: Mapping[str, Reader] = {reader.format_id: reader for reader in _ALL}

__all__ = ["READERS"]
