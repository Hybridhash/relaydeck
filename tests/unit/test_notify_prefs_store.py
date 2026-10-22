"""The per-device choice filters enqueue, is checked again at
send, is deleted with the device, and is never shared across devices. In memory; no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from relaydeck.gateway import push
from relaydeck.gateway.push import PushDenied, PushStore, pump_outbox
from tests.support.push import StubProvider

ACCOUNT = "acct-1"
HOSTS = frozenset({"home-mac"})
NOW = 1_000_000


def _store() -> PushStore:
    store = PushStore()
    store.register_device(ACCOUNT, "install-1", "fcm-tok-1", "android", NOW)
    store.register_device(ACCOUNT, "install-2", "fcm-tok-2", "android", NOW)
    return store


def _notify(store: PushStore, category: str, event_id: str) -> list[dict[str, Any]]:
    return store.notify(
        account=ACCOUNT,
        account_hosts=HOSTS,
        host_id="home-mac",
        session_id="term-1",
        event_id=event_id,
        category=category,
        now=NOW,
    )


def test_the_default_for_a_device_without_a_choice_is_every_category() -> None:
    store = _store()
    assert store.preferences(ACCOUNT, "install-1") == frozenset({"attention", "completion"})
    assert store.category_allowed(ACCOUNT, "install-1", "attention")
    assert store.category_allowed(ACCOUNT, "install-1", "completion")


@pytest.mark.parametrize(
    ("chosen", "allowed_attention", "allowed_completion"),
    [
        ({"attention", "completion"}, True, True),  # all
        ({"attention"}, True, False),  # blocked only
        ({"completion"}, False, True),  # done only
        (set(), False, False),  # none: stays subscribed, sends nothing
    ],
)
def test_each_choice_filters_the_right_categories(
    chosen: set[str], allowed_attention: bool, allowed_completion: bool
) -> None:
    store = _store()
    store.set_preferences(ACCOUNT, "install-1", chosen, NOW)
    assert store.category_allowed(ACCOUNT, "install-1", "attention") is allowed_attention
    assert store.category_allowed(ACCOUNT, "install-1", "completion") is allowed_completion


def test_enqueue_skips_only_the_device_that_turned_the_category_off() -> None:
    store = _store()
    store.set_preferences(ACCOUNT, "install-1", {"completion"}, NOW)
    rows = _notify(store, "attention", "ev-attention")
    assert [row["device_id"] for row in rows] == ["install-2"]
    rows = _notify(store, "completion", "ev-completion")
    assert sorted(row["device_id"] for row in rows) == ["install-1", "install-2"]


def test_a_change_between_enqueue_and_send_is_honoured() -> None:
    store = _store()
    rows = _notify(store, "attention", "ev-1")
    assert {row["device_id"] for row in rows} == {"install-1", "install-2"}
    store.set_preferences(ACCOUNT, "install-1", set(), NOW)  # the device turns everything off
    provider = StubProvider()
    report = pump_outbox(
        store,
        provider,
        now=NOW,
        authorized=lambda row: store.category_allowed(
            str(row["account"]), str(row["device_id"]), str(row["category"])
        ),
    )
    sent_tokens = [token for token, _payload in provider.sends]
    assert len(sent_tokens) == 1  # only install-2 received it
    assert report["dropped"] >= 1


def test_the_choice_is_per_device_and_never_shared() -> None:
    store = _store()
    store.set_preferences(ACCOUNT, "install-1", set(), NOW)
    assert store.preferences(ACCOUNT, "install-2") == frozenset({"attention", "completion"})
    assert store.preferences("acct-2", "install-1") == frozenset({"attention", "completion"})


def test_an_unknown_category_is_refused_and_nothing_is_stored() -> None:
    store = _store()
    with pytest.raises(PushDenied):
        store.set_preferences(ACCOUNT, "install-1", {"attention", "secret-thing"}, NOW)
    assert store.preferences(ACCOUNT, "install-1") == frozenset({"attention", "completion"})


def test_the_choice_persists_until_it_is_changed_again() -> None:
    store = _store()
    store.set_preferences(ACCOUNT, "install-1", {"attention"}, NOW)
    store.set_preferences(ACCOUNT, "install-1", {"completion"}, NOW + 1)
    assert store.preferences(ACCOUNT, "install-1") == frozenset({"completion"})


def test_revoking_the_device_deletes_its_choice() -> None:
    store = _store()
    store.set_preferences(ACCOUNT, "install-1", set(), NOW)
    assert store.revoke_device(ACCOUNT, "install-1", NOW) is True
    # With the row gone the device is back on the default, and nothing kept the old choice.
    assert store.preferences(ACCOUNT, "install-1") == frozenset({"attention", "completion"})


def test_revoking_by_reference_deletes_the_choice_too() -> None:
    store = _store()
    store.set_preferences(ACCOUNT, "install-2", {"attention"}, NOW)
    ref = store._db.execute(  # the opaque reference the logout path uses; tests may read it
        "SELECT device_ref FROM push_devices WHERE account=? AND device_id=?",
        (ACCOUNT, "install-2"),
    ).fetchone()["device_ref"]
    assert store.revoke_device_by_ref(ACCOUNT, ref, NOW) is True
    assert store.preferences(ACCOUNT, "install-2") == frozenset({"attention", "completion"})


def test_the_stored_default_has_the_two_categories_the_companion_uses() -> None:
    assert push.HINT_CATEGORIES == frozenset({"attention", "completion"})
    assert push.PREF_UI_TO_CATEGORY == {"blocked": "attention", "done": "completion"}
