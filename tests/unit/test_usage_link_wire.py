"""Additive wire fields, the inferred_linking toggle, historical labels,
and the no-path guarantee on the wire and in storage."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from relaydeck.companion.config import usage_from
from relaydeck.gateway.usage import UsageService
from relaydeck.usage.contracts import Batch, Coverage, PaneCwdObservation, UsageQuery
from relaydeck.usage.link_digests import cwd_digest, load_or_create_link_key
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, SOURCE, event, mapping

ZONE = "UTC"
FOLDER = "/synthetic/work/alpha"
KINDS = ("kind-a",)


def _value(tmp_path: Path, **over) -> dict:
    value: dict = {
        "sources": [
            {
                "kind": "kind-a",
                "format": "jsonl-message-usage-v1",
                "root": str(tmp_path),
                "display_name": "A",
            }
        ]
    }
    value.update(over)
    return value


def test_inferred_linking_defaults_true(tmp_path: Path) -> None:
    config = usage_from(_value(tmp_path), KINDS)
    assert config is not None and config.inferred_linking is True


def test_inferred_linking_explicit_false_kept(tmp_path: Path) -> None:
    config = usage_from(_value(tmp_path, inferred_linking=False), KINDS)
    assert config is not None and config.inferred_linking is False


def test_inferred_linking_must_be_bool(tmp_path: Path) -> None:
    assert usage_from(_value(tmp_path, inferred_linking="yes"), KINDS) is None


def _service(tmp_path: Path, labels: dict[str, str], inferred: bool) -> UsageService:
    store = SqliteUsageStore(tmp_path / "state/usage.db", ZONE, clock=lambda: NOW)
    config = SimpleNamespace(sources=(), timezone=ZONE, inferred_linking=inferred)
    return UsageService(store, config, "h1", tmp_path, lambda: NOW, workspace_labels=lambda: labels)


def _query() -> UsageQuery:
    return UsageQuery("h1", "2026-10-11", "2026-10-11")


def _pane_obs(digest: str) -> PaneCwdObservation:
    return PaneCwdObservation(SOURCE, "kind-a", "c1", "h1", digest, "w1", 0, NOW, NOW + 200)


def test_gateway_merges_historical_labels_under_live_ones(tmp_path: Path) -> None:
    service = _service(tmp_path, {"w2": "Live Label"}, True)
    assert service.store is not None
    cursor = service.store.cursor(SOURCE)
    aged = replace(mapping("w2", "a", NOW, NOW + 200), label="Stored Label")
    service.store.apply(
        "kind-a",
        Batch((event(t=NOW + 100),), cursor, Coverage.COMPLETE, cursor.revision),
        (aged,),
        source_key=SOURCE,
    )
    body = service._report(_query(), NOW)
    assert body["workspaces"] == [
        {"workspace_id": "w2", "label": "Live Label", "last_used_day": "2026-10-11"}
    ]
    assert "workspace_labels" not in body  # internal only, never on the wire
    quiet = _service(tmp_path, {}, True)
    quiet.store = service.store
    body = quiet._report(_query(), NOW)
    assert body["workspaces"] == [
        {"workspace_id": "w2", "label": "Stored Label", "last_used_day": "2026-10-11"}
    ]


def test_gateway_flag_gates_inferred_rows(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    key = load_or_create_link_key(tmp_path / "state")
    assert key is not None
    digest = cwd_digest(key, FOLDER)
    assert digest is not None
    for inferred in (True, False):
        service = _service(tmp_path / str(inferred), {}, inferred)
        assert service.store is not None
        cursor = service.store.cursor(SOURCE)
        service.store.apply(
            "kind-a",
            Batch(
                (replace(event(t=NOW + 100), cwd_digest=digest),),
                cursor,
                Coverage.COMPLETE,
                cursor.revision,
            ),
            (),
            source_key=SOURCE,
            cwd=(_pane_obs(digest),),
        )
        body = service._report(_query(), NOW)
        scopes = {row["scope"] for row in body["rows"]}
        assert scopes == ({"workspace", "overall"} if inferred else {"not_linked"})
        (coverage,) = body["coverage"]
        assert coverage["inferred_sessions"] == (1 if inferred else 0)
        for row in body["rows"]:
            assert "linked_sessions" in row and "inferred_sessions" in row


def test_missing_kinds_report_zero_link_counts(tmp_path: Path) -> None:
    service = _service(tmp_path, {}, True)
    assert service.store is not None
    source = SimpleNamespace(
        kind="kind-a",
        format="jsonl-message-usage-v1",
        display_name="A",
        limits_settings=None,
        limits_cache=None,
    )
    service.config = SimpleNamespace(sources=(source,), timezone=ZONE, inferred_linking=True)
    service._checks = {
        ("kind-a", "jsonl-message-usage-v1"): SimpleNamespace(check=lambda now: None)
    }
    body = service._report(_query(), NOW)
    (coverage,) = body["coverage"]
    assert coverage["coverage"] == Coverage.UNAVAILABLE
    assert (coverage["unmapped_sessions"], coverage["linked_sessions"]) == (0, 0)
    assert coverage["inferred_sessions"] == 0


def test_no_path_on_the_wire_or_in_storage(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    key = load_or_create_link_key(tmp_path / "state")
    assert key is not None
    digest = cwd_digest(key, FOLDER)
    assert digest is not None
    service = _service(tmp_path, {}, True)
    assert service.store is not None
    cursor = service.store.cursor(SOURCE)
    service.store.apply(
        "kind-a",
        Batch(
            (replace(event(t=NOW + 100), cwd_digest=digest),),
            cursor,
            Coverage.COMPLETE,
            cursor.revision,
        ),
        (replace(mapping("w1", "a", NOW, NOW + 200), label="Space One"),),
        source_key=SOURCE,
        cwd=(_pane_obs(digest),),
    )
    body = service._report(_query(), NOW)
    assert FOLDER not in json.dumps(body)
    assert digest not in json.dumps(body)  # digests never leave the store either
    service.store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = b"".join(
        sidecar.read_bytes()
        for name in ("usage.db", "usage.db-wal", "usage.db-shm")
        if (sidecar := tmp_path / "state" / name).exists()
    )
    assert FOLDER.encode() not in blob
    assert b"Space One" in blob  # labels are display data, and they do store
