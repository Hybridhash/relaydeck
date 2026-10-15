"""Format "jsonl-message-usage-v1": per-call usage records in per-project JSON Lines files.

Layout under the root: `<project>/<session>.jsonl`, and subagent logs in
`<project>/<session>/subagents/<agent>.jsonl`. Records with the role `assistant` carry
`message.usage` with the record's own totals. Only those totals are read:

- `message.usage.output_tokens_details.thinking_tokens` is reasoning, a subset of output.
- `message.usage.iterations[]` is ignored: the top-level usage is the record total.
- Top-level `modelUsage` summaries are ignored entirely, so they cannot double count.

Streamed duplicates share `(message.id, requestId)`, which is the event key, so a replay replaces.
The session is the record's `sessionId` when present, else the file name. The session's working
folder (top-level `cwd`) is digested with the budget's keyed digest callable and pinned per file;
the raw folder never leaves the reader.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from relaydeck.usage.contracts import Basis, Batch, Budget, Coverage, Cursor
from relaydeck.usage.readers._files import (
    Layout,
    LineHandler,
    as_count,
    context_of,
    counters_of,
    key_text,
    make_event,
    ms_of,
    read_file_unit,
    run_units,
    session_text,
    to_batch,
)

FORMAT_ID = "jsonl-message-usage-v1"
SCHEMA = "message-4"  # v4: session working-folder digest pinned per file


def _split_creation(usage: dict[str, object]) -> tuple[int, int, int] | None:
    """(short, long, unknown) cache-creation tokens, or None when a count is invalid."""
    total = as_count(usage.get("cache_creation_input_tokens", 0))
    detail = usage.get("cache_creation")
    if total is None:
        return None
    if not isinstance(detail, dict):
        return 0, 0, total
    short = as_count(detail.get("ephemeral_5m_input_tokens", 0))
    long = as_count(detail.get("ephemeral_1h_input_tokens", 0))
    if short is None or long is None:
        return None
    return short, long, max(0, total - short - long)


class _Handler(LineHandler):
    def __init__(self, cursor: Cursor, digest: Callable[[str], str | None] | None = None) -> None:
        self.cursor = cursor
        self.digest_of = digest
        self.reader_out: dict[str, dict[str, object]] = {}
        self.skipped = 0

    def begin(self, key: str, rel: str, restarted: bool) -> None:
        loaded: dict[str, object] = {}
        if not restarted:
            prior = context_of(self.cursor.files.get(key)).get("r")
            loaded = prior if isinstance(prior, dict) else {}
        pinned = loaded.get("d")
        self.pinned_digest = pinned if isinstance(pinned, str) and pinned else None

    def end(self, key: str, rel: str) -> bool:
        # No digest (no key, or the file carries no folder) leaves the file context
        # untouched, so digest-less reads keep byte-identical cursors.
        if self.pinned_digest is not None:
            self.reader_out[key] = {"d": self.pinned_digest}
        return False

    def reader_context(self, key: str) -> dict[str, object] | None:
        return self.reader_out.get(key)

    def line(self, key: str, rel: str, offset: int, data: bytes) -> bool:
        try:
            record = json.loads(data)
        except ValueError:
            self.skipped += 1
            return True
        if not isinstance(record, dict):
            return True
        if self.pinned_digest is None and self.digest_of is not None:
            folder = record.get("cwd")
            if isinstance(folder, str) and folder:
                self.pinned_digest = self.digest_of(folder)
        message = record.get("message")
        if not isinstance(message, dict):
            return True
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return True  # summaries and other record kinds carry no per-call usage
        parts = rel.split("/")
        subagent = len(parts) >= 4 and parts[-2] == "subagents"
        session = session_text(record.get("sessionId")) or session_text(parts[-1][: -len(".jsonl")])
        parent = session_text(parts[-3]) if subagent else None
        occurred = ms_of(record.get("timestamp"))
        input_tokens = as_count(usage.get("input_tokens", 0))
        output_tokens = as_count(usage.get("output_tokens", 0))
        cache_read = as_count(usage.get("cache_read_input_tokens", 0))
        creation = _split_creation(usage)
        details = usage.get("output_tokens_details")
        thinking = as_count(details.get("thinking_tokens", 0)) if isinstance(details, dict) else 0
        if (
            occurred is None
            or input_tokens is None
            or output_tokens is None
            or cache_read is None
            or creation is None
            or thinking is None
            or session is None
        ):
            self.skipped += 1
            return True
        short, long, unknown = creation
        counters = counters_of(
            input_uncached=input_tokens,
            cache_read=cache_read,
            cache_write_short=short,
            cache_write_long=long,
            cache_write_unknown=unknown,
            output=output_tokens,
            reasoning=thinking,
        )
        if counters is None:
            self.skipped += 1
            return True
        message_id = message.get("id")
        request_id = record.get("requestId")
        if isinstance(message_id, str) and isinstance(request_id, str):
            event_key = key_text("message", message_id, request_id)
        else:
            event_key = key_text("position", rel, offset)  # no identity: keyed by position
        event = make_event(
            session=session,
            event_key=event_key,
            occurred_ms=occurred,
            model=message.get("model"),
            counters=counters,
            basis=Basis.PER_CALL,
            parent=parent,
            digest=self.pinned_digest,
        )
        return self.collector.offer(event)


def _descend(parts: tuple[str, ...]) -> bool:
    """Projects, their session directories, and their subagents directories; nothing else."""
    return len(parts) <= 2 or (len(parts) == 3 and parts[2] == "subagents")


def _accept(parts: tuple[str, ...]) -> bool:
    if not parts[-1].endswith(".jsonl"):
        return False
    return len(parts) == 2 or (len(parts) == 4 and parts[2] == "subagents")


LAYOUT = Layout(descend=_descend, accept=_accept)


class JsonlMessageUsageReader:
    format_id = FORMAT_ID
    tokens_reported = True
    lock_filename: str | None = None

    def lock_session_keys(self, rel: str) -> tuple[str | None, str | None]:
        return None, None

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch:
        handler = _Handler(cursor, budget.digest)
        result = run_units(
            root,
            cursor,
            budget,
            LAYOUT,
            lambda root_fd, item, prev, pending, state, ledger: read_file_unit(
                root_fd, item, prev, cursor, pending, state, handler
            ),
            schema=SCHEMA,
        )
        return to_batch(result, cursor, Coverage.COMPLETE)


READER = JsonlMessageUsageReader()
