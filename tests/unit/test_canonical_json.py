"""Golden serialization at persisted, hashed and signed boundaries; synthetic data only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from relaydeck.companion import persistence
from relaydeck.connector.journal import result_digest_for
from relaydeck.gateway import cursors, inventory, webpush
from relaydeck.gateway.push import ProviderOutcome
from relaydeck.journal import CommandJournal, canonical_body, digest_for
from relaydeck.usage import contracts, store
from relaydeck.usage.limits_plane.refresh_state import RefreshState, session_scope
from relaydeck.usage.readers import _files
from tests.support.usage.store import NOW, SOURCE, event, mapping, write

VALUE = {"z": [1.5, -0.0, 1e-06, 1e20], "a": "é雪🙂", "nested": {"y": True, "x": None}}
RAW = (
    r'{"a":"\u00e9\u96ea\ud83d\ude42","nested":{"x":null,"y":true},'
    r'"z":[1.5,-0.0,1e-06,1e+20]}'
)
VALUE_DIGEST = "111ab6fac38927ad913f9375d23b9ad4afa23bb7be19a19c88940cf9d24f0ca7"
BODY = (
    r'{"host_id":"h","kind":"shell.create","payload":{"number":1.5,'
    r'"unicode":"\u00e9\u96ea\ud83d\ude42"},"target":' + RAW + "}"
)
BODY_DIGEST = "70aff9134ed8ff4dd75780ab33e30c7800f680d8332124e9d04a4da610cd1f43"
SIGNED_CURSOR = (
    "eyJhIjoiXHUwMGU5XHU5NmVhXHVkODNkXHVkZTQyIiwibmVzdGVkIjp7IngiOm51bGwsInkiOnRydWV9"
    "LCJ6IjpbMS41LC0wLjAsMWUtMDYsMWUrMjBdfQ.3y0dV1fY6jtvi3NwZ_gPEoZutjbAmCkTPhJqSY7aSYo"
)
RECEIPT_DIGEST = "b85796c11033400f13ceb41c17856584c3889ad0e911e146d1306067f837129f"


def test_journal_body_and_digests_golden() -> None:
    payload = {"unicode": "é雪🙂", "number": 1.5}
    assert canonical_body("h", "shell.create", VALUE, payload).encode() == BODY.encode()
    assert digest_for("h", "shell.create", VALUE, payload) == BODY_DIGEST
    assert result_digest_for(VALUE) == inventory._hash(VALUE) == VALUE_DIGEST


def test_persistence_and_usage_json_golden() -> None:
    assert persistence._json(VALUE) == RAW.encode()
    assert store._json(VALUE).encode() == RAW.encode()


def test_journal_fields_and_previously_stored_record(tmp_path: Path) -> None:
    path = tmp_path / "commands.db"
    journal = CommandJournal(path, clock=lambda: 1234)
    journal.submit(
        command_id="command-1",
        principal="account",
        host_id="h",
        kind="shell.create",
        target=VALUE,
        payload=VALUE,
    )
    assert journal.note_progress("command-1", VALUE)
    assert journal.mark_dispatched("command-1")
    assert journal.mark_result("command-1", "succeeded", result_digest=VALUE_DIGEST, result=VALUE)
    row = journal._db.execute(
        "SELECT target_json,payload_json,result_json,progress_json FROM commands"
    ).fetchone()
    assert tuple(row) == (RAW,) * 4
    # Seed the old encoding explicitly, then read it in a fresh journal instance.
    journal._db.execute(
        "UPDATE commands SET target_json=?,payload_json=?,result_json=?,progress_json=?",
        (RAW,) * 4,
    )
    journal._db.commit()
    journal.close()
    journal = CommandJournal(path, clock=lambda: 1234)
    try:
        record = journal.record("command-1")
        assert record is not None and record["target"] == VALUE
        assert journal._db.execute("SELECT result_json FROM commands").fetchone()[0] == RAW
    finally:
        journal.close()


def test_stream_cursor_golden_and_old_cursor_decode() -> None:
    assert cursors.encode_cursor(b"synthetic-secret", VALUE) == SIGNED_CURSOR
    assert cursors.decode_cursor(b"synthetic-secret", SIGNED_CURSOR) == VALUE


def test_usage_cursor_size_and_reader_context_golden() -> None:
    file = contracts.FileCursor(1, 2, 3, 4, 5, "0" * 64, "é雪🙂")
    cursor = contracts.Cursor({"f": file}, {"z": "é雪🙂", "a": "1"})
    assert contracts.encoded_state_bytes(cursor.state) == 40
    assert contracts.encoded_cursor_bytes(cursor) == 170
    context = _files.file_context(reader=VALUE, discard=None, gap=False, pending=False)
    assert context == '{"r":' + RAW + "}"
    gap = _files.file_context(reader=VALUE, discard=None, gap=True, pending=False)
    ledger = _files.Ledger({"f": contracts.FileCursor(1, 2, 3, 4, 5, "0" * 64, gap)}, {})
    assert ledger.summarise_gaps()
    assert ledger.files["f"].context == context
    assert ledger.encoded_bytes() == contracts.encoded_cursor_bytes(
        contracts.Cursor(ledger.files, ledger.state)
    )


def test_usage_receipt_golden_and_stored_receipt_retry(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with store.SqliteUsageStore(path, "UTC", clock=lambda: NOW) as usage:
        batch = write(usage, (event(),))
        assert usage._db.execute("SELECT digest FROM receipts").fetchone() == (RECEIPT_DIGEST,)
    with store.SqliteUsageStore(path, "UTC", clock=lambda: NOW) as usage:
        before = usage._db.total_changes
        assert (
            usage.apply("kind-a", batch, (mapping(),), source_key=SOURCE) == contracts.ApplyResult()
        )
        assert usage._db.total_changes == before
        assert usage._db.execute("SELECT digest FROM receipts").fetchone() == (RECEIPT_DIGEST,)


def test_refresh_state_golden_and_old_record_load(tmp_path: Path) -> None:
    path = tmp_path / "refresh.json"
    state = RefreshState(path, clock_ms=lambda: 1234)
    scope = session_scope("kind-a", "root-é", "space-雪")
    state.set_cooldown("kind-a", 2000)
    state.save_session(scope, "session-🙂", "hint")
    state.record_attempt("kind-a", "attempt-1", 1234, "ok")
    raw = (
        rb'{"attempts":{"kind-a":{"finished_ms":1234,"id":"attempt-1","result":"ok"}},'
        rb'"cooldowns":{"kind-a":2000},"evicted_unresolved":0,"journal":[],"sessions":'
        rb'{"[\"kind-a\",\"root-\\u00e9\",\"space-\\u96ea\"]":{"id":'
        rb'"session-\ud83d\ude42","provenance":"hint","saved_ms":1234}},"version":1}'
    )
    assert path.read_bytes() == raw
    path.write_bytes(raw)
    restored = RefreshState(path, clock_ms=lambda: 1234)
    assert restored.saved_session(scope) == state.saved_session(scope)
    assert restored.last_attempt("kind-a") == state.last_attempt("kind-a")
    assert restored.cooldown_until("kind-a") == 2000


def test_webpush_plaintext_golden_and_jwt_order_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintexts: list[bytes] = []

    def capture(raw: bytes, public: bytes, auth: bytes) -> bytes:
        plaintexts.append(raw)
        return b"synthetic-ciphertext"

    monkeypatch.setattr(webpush, "encrypt", capture)
    key = ec.generate_private_key(ec.SECP256R1())
    token = json.dumps(
        {
            "endpoint": "https://web.push.apple.com/synthetic",
            "ttl": 60,
            "urgency": "high",
            "topic": "synthetic",
            "p256dh": webpush.public_key_b64u(key),
            "auth": webpush.b64u(b"\x04" * 16),
        }
    )
    payload = {"z": "é雪🙂", "a": "Notice"}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(201))) as client:
        provider = webpush.WebPushProvider(
            key,
            client=client,
            resolver=lambda h: "17.250.0.1",
            clock=lambda: 1234,
        )
        assert provider.send(token, payload) is ProviderOutcome.SENT
    assert plaintexts == [rb'{"a":"Notice","z":"\u00e9\u96ea\ud83d\ude42"}']
    header, claims, _ = webpush.vapid_jwt(
        key, "https://example.test", "https://example.test/contact", 1234
    ).split(".")
    assert webpush.b64u_decode(header) == b'{"typ":"JWT","alg":"ES256"}'
    assert webpush.b64u_decode(claims) == (
        b'{"aud":"https://example.test","exp":44434,"sub":"https://example.test/contact"}'
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_existing_nonfinite_policies(value: float) -> None:
    expected = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert persistence._json(value) == expected.encode()
    assert canonical_body("h", "k", None, value).endswith('"target":null}')
    assert (
        result_digest_for({"n": value})
        == hashlib.sha256(
            json.dumps({"n": value}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with pytest.raises(ValueError):
        store._json(value)
    with pytest.raises(ValueError):
        contracts.encoded_state_bytes({"n": value})  # type: ignore[dict-item]


@pytest.mark.parametrize("ensure_ascii", [True, False])
@pytest.mark.parametrize("allow_nan", [True, False])
@pytest.mark.parametrize("value", [VALUE, ["é雪🙂", -0.0, 1e-06], None, True])
def test_helper_matches_exact_encoding_options(
    value: Any,
    ensure_ascii: bool,
    allow_nan: bool,
) -> None:
    from relaydeck.contracts.canonical import canonical_json

    assert canonical_json(value, ensure_ascii=ensure_ascii, allow_nan=allow_nan) == json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=ensure_ascii,
        allow_nan=allow_nan,
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_helper_nonfinite_policy(value: float) -> None:
    from relaydeck.contracts.canonical import canonical_json

    assert canonical_json(value) == json.dumps(value)
    with pytest.raises(ValueError):
        canonical_json(value, allow_nan=False)
