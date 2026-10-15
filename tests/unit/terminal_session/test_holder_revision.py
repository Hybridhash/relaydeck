"""Holder revisions fence ownership changes, not renewal or unrelated traffic."""

from typing import Any

import pytest

from relaydeck.gateway.leases import CONTROL_LEASE_TTL_MS, Attachment, LeaseError, LeaseStore


def attach(store: LeaseStore, device: str = "a", **kwargs: Any) -> Attachment:
    return store.attach(
        host_id="h",
        terminal_id="t",
        kind="agent",
        account="acct",
        device_id=device,
        takeover=True,
        owner_label=device,
        now_ms=0,
        **kwargs,
    )[0]


def revision(store: LeaseStore, now: int = 0) -> str:
    return store.control_holder("h", "t", "acct", "a", now)["control_revision"]


def test_holder_revision_preserves_same_holder_on_renewal_and_unrelated_traffic() -> None:
    store = LeaseStore()
    initial = revision(store)
    att = attach(store, expected_control_revision=initial)
    held = store.acquire_control(att.attachment_id, "acct", "a", 0)
    observed = revision(store)
    store.acquire_control(att.attachment_id, "acct", "a", 1)
    store.issue_view(att.attachment_id, "acct", "b", 2)
    other, _ = store.attach(
        host_id="h",
        terminal_id="other",
        kind="shell",
        account="acct",
        device_id="b",
        takeover=False,
        owner_label="b",
        now_ms=3,
    )
    store.acquire_control(other.attachment_id, "acct", "b", 4)
    assert revision(store) == observed
    assert attach(store, expected_control_revision=observed).generation == 2
    assert held.superseded


@pytest.mark.parametrize("mutation", ["released", "expired", "revoked", "removed"])
def test_holder_revision_detects_new_lease_even_after_holder_disappears(mutation: str) -> None:
    store = LeaseStore()
    att = attach(store)
    first = store.acquire_control(att.attachment_id, "acct", "a", 0)
    store.release(first.lease_id, "acct", "a")
    observed = revision(store)
    second = store.acquire_control(att.attachment_id, "acct", "b", 0)
    now = 0
    if mutation == "released":
        store.release(second.lease_id, "acct", "b")
    elif mutation == "revoked":
        store.revoke_device("acct", "b")
    elif mutation == "removed":
        store.hard_remove(second.lease_id)
    else:
        now = CONTROL_LEASE_TTL_MS + 1
    assert store.control_holder("h", "t", "acct", "a", now)["held"] is False
    with pytest.raises(LeaseError, match="control holder changed"):
        store.attach(
            host_id="h",
            terminal_id="t",
            kind="agent",
            account="acct",
            device_id="a",
            takeover=True,
            owner_label="a",
            now_ms=now,
            expected_control_revision=observed,
        )
    assert store.get_attachment(att.attachment_id) is att


def test_holder_revision_reserves_inflight_attach_and_survives_no_server_restart() -> None:
    store = LeaseStore()
    att = attach(store, "b")
    pending = store.control_holder("h", "t", "acct", "a", 0)
    assert pending["held"] and not pending["mine"]
    assert pending["holder"] == {"device_id": "b"}
    with pytest.raises(LeaseError, match="control holder changed"):
        attach(store, expected_control_revision=pending["control_revision"])
    assert store.get_attachment(att.attachment_id) is att
    old_empty = revision(LeaseStore())
    with pytest.raises(LeaseError, match="control holder changed"):
        attach(LeaseStore(), expected_control_revision=old_empty)


def test_holder_revision_account_boundary_stays_not_found() -> None:
    store = LeaseStore()
    att = attach(store, "b")
    foreign = store.control_holder("h", "t", "foreign", "a", 0)
    assert not foreign["held"] and "holder" not in foreign
    with pytest.raises(LeaseError) as exc:
        store.attach(
            host_id="h",
            terminal_id="t",
            kind="agent",
            account="foreign",
            device_id="a",
            takeover=True,
            owner_label="a",
            now_ms=0,
            expected_control_revision=foreign["control_revision"],
        )
    assert exc.value.code == "not_found" and exc.value.details == {}
    assert store.get_attachment(att.attachment_id) is att


def test_holder_generation_condition_cannot_redeem_another_devices_reservation() -> None:
    store = LeaseStore()
    att = attach(store, "b")
    with pytest.raises(LeaseError) as exc:
        store.acquire_control(att.attachment_id, "acct", "a", 0, expected_generation=att.generation)
    assert exc.value.code == "lease_conflict" and not store._leases
    assert (
        store.acquire_control(
            att.attachment_id,
            "acct",
            "b",
            0,
            expected_generation=att.generation,
        ).device_id
        == "b"
    )
