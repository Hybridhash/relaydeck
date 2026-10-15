"""Pause-time draft persistence. Flet-free.

`pause_persist` writes one `export_draft` snapshot to secure storage
through the `CredentialStore` port, whose implementation is
fail-closed: unavailable storage raises instead of downgrading. On a
secure-storage outage the snapshot is held in memory and pause reports
a capability failure -- drafts NEVER fall back to plaintext preferences
(this module takes no plaintext handle at all, so a downgrade is
unwritable here).

`resume_restore` reads the snapshot back for `import_draft`: a missing
key is a clean miss (nothing was persisted), an outage surfaces the
same capability failure and the caller keeps its in-memory draft, and
a corrupt payload is reported without touching the live draft.

Keys use the adapter-safe `draft-<id>` form: the adapter
rejects `:` in keys, so the `draft:` prefix of the stores model is
spelled with a dash on the real path. Same placement rule (secure
storage only), same audit outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from relaydeck.client.adapters.ports import CredentialStore

#: Prefix (adapter-safe form) for pause-persisted draft keys.
DRAFT_KEY_PREFIX = "draft-"

#: Capability failure surfaced when secure storage cannot take the draft.
OUTAGE_MESSAGE = (
    "Secure storage unavailable: draft held in memory only (capability failure; never plaintext)."
)


def draft_key(agent_id: str) -> str:
    """Secure-storage key for one agent surface's pause-persisted draft."""
    return f"{DRAFT_KEY_PREFIX}{agent_id}"


@dataclass(frozen=True)
class PausePersistReport:
    """What `pause_persist` did with one draft snapshot."""

    key: str
    persisted: bool
    held_snapshot: dict[str, Any] | None
    capability_error: str | None


async def pause_persist(
    store: CredentialStore, key: str, snapshot: dict[str, Any]
) -> PausePersistReport:
    """Persist one `export_draft` snapshot to secure storage only.

    Success reports `persisted` with no hold and no error. Any secure
    outage (unavailable store, failed write) reports `persisted=False`
    with an in-memory `held_snapshot` copy plus a `capability_error` --
    and writes nothing anywhere else. There is no plaintext fallback.
    """
    try:
        if not await store.available():
            return PausePersistReport(key, False, dict(snapshot), OUTAGE_MESSAGE)
        await store.set(key, json.dumps(snapshot))
    except Exception as exc:
        return PausePersistReport(key, False, dict(snapshot), f"{OUTAGE_MESSAGE} ({exc})")
    return PausePersistReport(key, True, None, None)


@dataclass(frozen=True)
class ResumeRestoreReport:
    """What `resume_restore` found under one draft key."""

    key: str
    snapshot: dict[str, Any] | None
    found: bool
    capability_error: str | None


async def resume_restore(store: CredentialStore, key: str) -> ResumeRestoreReport:
    """Read one pause-persisted snapshot back for `import_draft`.

    A missing key reports `found=False` with no error (nothing was
    persisted). An outage reports the capability failure with no
    snapshot (the caller keeps its in-memory draft). A corrupt payload
    reports an error with no snapshot (the live draft is untouched).
    """
    try:
        if not await store.available():
            return ResumeRestoreReport(key, None, False, OUTAGE_MESSAGE)
        raw = await store.get(key)
    except Exception as exc:
        return ResumeRestoreReport(key, None, False, f"{OUTAGE_MESSAGE} ({exc})")
    if raw is None:
        return ResumeRestoreReport(key, None, False, None)
    try:
        doc = json.loads(raw)
    except ValueError:
        return ResumeRestoreReport(key, None, False, "stored draft snapshot is corrupt")
    if not isinstance(doc, dict):
        return ResumeRestoreReport(key, None, False, "stored draft snapshot is corrupt")
    return ResumeRestoreReport(key, doc, True, None)
