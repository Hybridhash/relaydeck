"""Format "jsonl-cumulative-token-count-v1": per-call usage in per-session rollout files.

Layout under the root: `sessions/YYYY/MM/DD/rollout-*.jsonl`. Counted usage comes ONLY from
top-level `token_usage_record` records, one per model call: `payload.usage` is the per-call usage,
keyed by `(session, payload.response_id)`. Nested `compacted.latest_token_usage_record` entries
repeat earlier records and are ignored. Cumulative `token_count` totals are NOT used for usage;
they supply rate-limit snapshots only (latest per quota and window). A file that reports
cumulative totals but no per-call records is a known gap: it is counted as legacy, and the batch
stays PARTIAL.

Input is inclusive: uncached = input - cached_input - cache_write_input. A record whose result is
negative, or whose reasoning exceeds its output, is skipped and counted (a gap). The model is the
latest turn context's model, with the session-meta model as the fallback. The session's working
folder (`session_meta` `cwd`) is digested with the budget's keyed digest callable and pinned per
file; the raw folder never leaves the reader.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from pathlib import Path

from relaydeck.usage.contracts import (
    Basis,
    Batch,
    Budget,
    Coverage,
    Cursor,
    LimitSnapshot,
    LimitWindow,
)
from relaydeck.usage.readers._files import (
    Layout,
    LineHandler,
    as_count,
    context_of,
    counters_of,
    key_text,
    make_event,
    model_text,
    ms_of,
    read_file_unit,
    run_units,
    session_text,
    to_batch,
)

FORMAT_ID = "jsonl-cumulative-token-count-v1"
FILE_UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
SCHEMA = "rollout-4"  # v4: session working-folder digest pinned per file


def _window(name: str, data: object, observed: int) -> LimitWindow | None:
    if not isinstance(data, dict):
        return None
    used = data.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, int | float):
        return None
    if not math.isfinite(used) or not 0 <= used <= 100:
        return None
    minutes = as_count(data.get("window_minutes"))
    resets = as_count(data.get("resets_at"))
    return LimitWindow(
        window_key=name,
        used_percent=float(used),
        window_minutes=minutes,
        resets_at_ms=resets * 1000 if resets is not None else None,
    )


class _Handler(LineHandler):
    def __init__(self, cursor: Cursor, digest: Callable[[str], str | None] | None = None) -> None:
        self.cursor = cursor
        self.digest_of = digest
        self.reader_out: dict[str, dict[str, object]] = {}
        self.skipped = 0
        self.limits: dict[str, tuple[int, tuple[LimitWindow, ...]]] = {}

    def begin(self, key: str, rel: str, restarted: bool) -> None:
        loaded: dict[str, object] = {}
        prior_offset = 0
        if not restarted:
            stored = self.cursor.files.get(key)
            prior_offset = stored.offset if stored is not None else 0
            prior = context_of(stored).get("r")
            loaded = prior if isinstance(prior, dict) else {}
        match = FILE_UUID.search(rel)
        self.fallback_session = session_text(match.group(1)) if match else session_text(key)
        self.session = session_text(loaded.get("sid"))
        self.model = model_text(loaded.get("model")) or ""
        pinned = loaded.get("d")
        self.pinned_digest = pinned if isinstance(pinned, str) and pinned else None
        # What earlier chunks of this file showed is kept in the file's reader context, so a
        # file read in several budget-limited chunks is judged on everything it contains.
        self.saw_usage = loaded.get("u") == 1
        self.saw_legacy = loaded.get("l") == 1
        # A cursor that already consumed bytes but never recorded u/l was written before those
        # flags existed: the prefix's per-call usage is unknown. That never asserts absence.
        self.prefix_unknown = loaded.get("pu") == 1 or (
            prior_offset > 0 and "u" not in loaded and "l" not in loaded
        )
        self.unknown_gap = False

    def end(self, key: str, rel: str) -> bool:
        out: dict[str, object] = {
            "sid": self.session,
            "model": self.model,
            "u": 1 if self.saw_usage else 0,
            "l": 1 if self.saw_legacy else 0,
            "pu": 1 if self.prefix_unknown else 0,
        }
        if self.pinned_digest is not None:
            out["d"] = self.pinned_digest
        self.reader_out[key] = out
        # Verdicts about the whole file are given only at its verified end (reached_end, read by
        # read_file_unit only when no unterminated record is left).
        cumulative_only = self.reached_end and self.saw_legacy and not self.saw_usage
        if self.prefix_unknown:
            self.unknown_gap = (
                cumulative_only  # the origin is unknown: an incomplete gap, no legacy
            )
            return False
        return cumulative_only

    def reader_context(self, key: str) -> dict[str, object] | None:
        return self.reader_out.get(key)

    def _limits(self, payload: dict[str, object], observed: int) -> None:
        raw = payload.get("rate_limits")
        if not isinstance(raw, dict):
            return
        quota_raw = raw.get("limit_id")
        quota = quota_raw if isinstance(quota_raw, str) and 0 < len(quota_raw) <= 128 else "default"
        windows = tuple(
            item
            for item in (
                _window("primary", raw.get("primary"), observed),
                _window("secondary", raw.get("secondary"), observed),
            )
            if item is not None
        )
        if not windows:
            return
        known = self.limits.get(quota)
        if known is None or observed >= known[0]:
            self.limits[quota] = (observed, windows)

    def _usage(self, payload: dict[str, object], observed: int | None, rel: str) -> bool:
        """Count one per-call record. Returns False only when the batch budget refuses it."""
        self.saw_usage = True
        usage = payload.get("usage")
        response = payload.get("response_id")
        if not isinstance(response, str) or not response or len(response) > 256:
            self.skipped += 1
            return True
        if not isinstance(usage, dict) or observed is None:
            self.skipped += 1
            return True
        inputs = as_count(usage.get("input_tokens", 0))
        cached = as_count(usage.get("cached_input_tokens", 0))
        writes = as_count(usage.get("cache_write_input_tokens", 0))
        outputs = as_count(usage.get("output_tokens", 0))
        reasoning = as_count(usage.get("reasoning_output_tokens", 0))
        if None in (inputs, cached, writes, outputs, reasoning):
            self.skipped += 1
            return True
        assert inputs is not None and cached is not None and writes is not None  # noqa: S101
        assert outputs is not None and reasoning is not None  # noqa: S101
        uncached = inputs - cached - writes
        if uncached < 0 or reasoning > outputs:
            self.skipped += 1
            return True
        counters = counters_of(
            input_uncached=uncached,
            cache_read=cached,
            cache_write_unknown=writes,
            output=outputs,
            reasoning=reasoning,
        )
        if counters is None:
            self.skipped += 1
            return True
        session = self.session or self.fallback_session or key_text("session", rel)
        return self.collector.offer(
            make_event(
                session=session,
                event_key=key_text("response", response),
                occurred_ms=observed,
                model=self.model or None,
                counters=counters,
                basis=Basis.PER_CALL,
                digest=self.pinned_digest,
            )
        )

    def line(self, key: str, rel: str, offset: int, data: bytes) -> bool:
        try:
            record = json.loads(data)
        except ValueError:
            self.skipped += 1
            return True
        if not isinstance(record, dict):
            return True
        kind = record.get("type")
        raw = record.get("payload")
        payload: dict[str, object] = raw if isinstance(raw, dict) else {}
        if kind == "session_meta":
            found = session_text(payload.get("id"))
            if found is not None:
                self.session = found
            model = model_text(payload.get("model"))
            if model is not None and not self.model:
                self.model = model
            if self.pinned_digest is None and self.digest_of is not None:
                folder = payload.get("cwd")
                if isinstance(folder, str) and folder:
                    self.pinned_digest = self.digest_of(folder)
            return True
        if kind == "turn_context":
            self.model = model_text(payload.get("model")) or self.model
            return True
        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                self.saw_legacy = True  # cumulative totals only: usage is not derived from them
            observed = ms_of(record.get("timestamp"))
            if observed is not None:
                self._limits(payload, observed)
            return True
        if kind == "token_usage_record":
            return self._usage(payload, ms_of(record.get("timestamp")), rel)
        return True  # compacted replays and other kinds are not counted


class JsonlCumulativeRolloutReader:
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
        limits = tuple(
            LimitSnapshot(kind="", quota_key=quota, observed_ms=observed, windows=windows)
            for quota, (observed, windows) in sorted(handler.limits.items())
        )
        return to_batch(result, cursor, Coverage.COMPLETE, limits)


def _descend(parts: tuple[str, ...]) -> bool:
    return bool(parts) and parts[0] == "sessions" and len(parts) <= 4


def _accept(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 5
        and parts[0] == "sessions"
        and parts[4].startswith("rollout-")
        and parts[4].endswith(".jsonl")
    )


LAYOUT = Layout(descend=_descend, accept=_accept)


READER = JsonlCumulativeRolloutReader()
