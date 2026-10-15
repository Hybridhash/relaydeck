"""Shared opaque stream cursor limits and authenticated replay header."""

MAX_CURSOR_LEN = 512
# Authenticated SSE response: the server-verified replay boundary for the
# requested opaque cursor, never an upstream Herdr field.
AFTER_SEQUENCE_HEADER = "X-RelayDeck-After-Sequence"
