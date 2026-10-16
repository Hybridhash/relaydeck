import asyncio
import json
import threading
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from relaydeck.companion.config import UsageConfig, UsageSourceConfig
from relaydeck.connector.source import translate_event
from relaydeck.gateway.app import create_app
from relaydeck.gateway.usage import (
    CURSOR_TTL_MS,
    MAX_REPORTS,
    UsageService,
    live_workspace_labels,
)
from relaydeck.usage.contracts import (
    Basis,
    Batch,
    Counters,
    Coverage,
    SessionMapping,
    UsageEvent,
    UsageQuery,
)
from relaydeck.usage.pricing import PriceTable
from relaydeck.usage.store import SqliteUsageStore
from tests.security.helpers import auth, make_state
from tests.support.usage.store import NOW

SOURCE = "home-mac/kind-a/jsonl-message-usage-v1"
URL = "/v1/hosts/home-mac/usage"
PARAMS = {"from": "2026-10-11", "to": "2026-10-11"}


@pytest.fixture
def endpoint(tmp_path):
    state, _ = make_state(NOW)
    clock = [NOW]
    state.now_ms = lambda: clock[0]
    state.hosts.enroll("home-mac", account="owner")
    config = UsageConfig(
        "UTC",
        (
            UsageSourceConfig("kind-a", "jsonl-message-usage-v1", tmp_path.resolve(), "Kind A"),
            UsageSourceConfig("kind-b", "json-session-turns-v1", tmp_path.resolve(), "Kind B"),
        ),
    )
    store = SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: clock[0])
    store.prices = PriceTable(snapshot_labels=(("2026-10-01", "Standard-tier estimate"),))
    service = UsageService(store, config, "home-mac", tmp_path.resolve() / "state", state.now_ms)
    service.workspace_labels = lambda: live_workspace_labels(state, "home-mac")
    state.usage = service
    token, _ = state.sessions.create(
        "owner", {"sessions.read"}, {"home-mac"}, NOW, device_id="device-a"
    )
    client = TestClient(create_app(state))
    try:
        yield client, state, store, service, token, clock
    finally:
        client.close()
        asyncio.run(service.close())
        store.close()


def write(store, events, mappings=()):
    cursor = store.cursor(SOURCE)
    store.apply(
        "kind-a",
        Batch(tuple(events), cursor, Coverage.COMPLETE, cursor.revision),
        tuple(mappings),
        source_key=SOURCE,
    )


def populate(store, count=600):
    maps = (
        SessionMapping(SOURCE, "kind-a", "secret-session", "i1", "home-mac", "space-a", NOW, NOW),
    )
    write(
        store,
        (
            UsageEvent("secret-session", str(i), NOW, f"model-{i:04}", Counters(10), Basis.PER_CALL)
            for i in range(count)
        ),
        maps,
    )


def get(client, token, params=None):
    return client.get(URL, params=params or PARAMS, headers=auth(token))


def test_wire_metadata_and_privacy_through_http_bridge(endpoint, tmp_path):
    client, state, store, service, token, _ = endpoint
    populate(store, 1)
    response = get(client, token)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "schema_version",
        "host_id",
        "generated_ms",
        "timezone",
        "price_table_date",
        "next_cursor",
        "rows",
        "limits",
        "coverage",
        "snapshot_labels",
        "kinds",
        "workspaces",
        "capacity",  # additive field
    }
    # v1 fields are unchanged; `sources` is checked on its own.
    unconfigured = {  # PANE-LIMITS additive field; no on-demand block is wired here
        "available": False,
        "reason": "not_configured",
        "in_progress": False,
        "cooldown_until_ms": None,
        "last_error": None,
        "last_attempt": None,
    }
    assert [{k: v for k, v in kind.items() if k != "sources"} for kind in body["kinds"]] == [
        {
            "kind": "kind-a",
            "display_name": "Kind A",
            "tokens_reported": True,
            "credits_reported": False,  # additive v2 field; token kinds report no credits
            "limits_setup": None,
            "limits_refresh": unconfigured,
            "last_activity_ms": body["kinds"][0]["last_activity_ms"],  # latest event, additive
        },
        {
            "kind": "kind-b",
            "display_name": "Kind B",
            "tokens_reported": False,
            "credits_reported": False,
            "limits_setup": None,
            "limits_refresh": unconfigured,
            "last_activity_ms": None,
        },
    ]
    assert isinstance(body["kinds"][0]["last_activity_ms"], int)
    assert body["kinds"][1]["last_activity_ms"] is None  # no events: null, never a guess
    assert body["kinds"][0]["sources"] == [
        {"format": "jsonl-message-usage-v1", "limits_setup": None}
    ]
    assert body["snapshot_labels"] == [{"date": "2026-10-01", "label": "Standard-tier estimate"}]
    assert "secret-session" not in response.text and str(tmp_path) not in response.text
    assert all(row["cost_micro_usd"] is None for row in body["rows"])

    # The same authenticated HTTP request path used by make_relaydeck_main.
    async def bridge():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://gw"
        ) as http:
            r = await http.get(URL, params=PARAMS, headers=auth(token))
            assert r.status_code == 200 and r.json() == body

    asyncio.run(bridge())


@pytest.mark.parametrize(
    "case",
    ["none", "scope", "wrong_host", "foreign_owner", "device", "account", "host", "missing_record"],
)
def test_authorization_current_ownership_and_revocation(endpoint, case):
    client, state, _, _, token, _ = endpoint
    if case == "none":
        token = ""
    elif case == "scope":
        token, _ = state.sessions.create("owner", set(), {"home-mac"}, NOW)
    elif case == "wrong_host":
        token, _ = state.sessions.create(
            "owner", {"sessions.read"}, {"lab-mac"}, NOW, host_binding="lab-binding"
        )
    elif case == "foreign_owner":
        token, _ = state.sessions.create("foreign", {"sessions.read"}, {"home-mac"}, NOW)
    elif case == "device":
        state.sessions.revoke_device("owner", "device-a")
    elif case == "account":
        state.sessions.revoke_account("owner")
    elif case == "host":
        state.hosts.revoke("home-mac")
    elif case == "missing_record":
        state.hosts._by_token.clear()
        state.hosts._token_by_host.clear()
    response = get(client, token)
    assert response.status_code in (401, 403, 404)
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"error"}


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"from": "2026-01-01", "to": "2026-04-01"},
        {"from": "2026-10-12", "to": "2026-10-11"},
        {**PARAMS, "unexpected": "secret"},
        {**PARAMS, "from": "20261011"},
        {**PARAMS, "cursor": "not-a-cursor"},
        {**PARAMS, "space_id": ""},
        [("from", "2026-10-11"), ("from", "2026-10-11"), ("to", "2026-10-11")],
    ],
)
def test_invalid_duplicate_unknown_parameters_and_range(endpoint, params):
    client, _, _, _, token, _ = endpoint
    response = client.get(URL, params=params, headers=auth(token))
    assert response.status_code == 400 and response.json() == {"error": "invalid_query"}
    assert response.headers["cache-control"] == "no-store"


def test_90_day_calendar_span_in_dst_timezone(endpoint, tmp_path):
    client, _, store, service, token, clock = endpoint
    service.config = replace(service.config, timezone="America/New_York")
    clock[0] = int(datetime(2026, 11, 2, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    # New token valid at the test's November clock, not an expired October credential.
    state = endpoint[1]
    token, _ = state.sessions.create("owner", {"sessions.read"}, {"home-mac"}, clock[0])
    stamp = int(
        datetime(2026, 11, 1, 23, 30, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
    )
    ny_store = SqliteUsageStore(
        tmp_path / "ny-state/usage.db", "America/New_York", clock=lambda: clock[0]
    )
    service.store = ny_store
    write(
        ny_store, (UsageEvent("unlinked", "e-dst", stamp, "model-a", Counters(2), Basis.PER_CALL),)
    )
    response = get(client, token, {"from": "2026-08-05", "to": "2026-11-02"})
    assert response.status_code == 200 and response.json()["timezone"] == "America/New_York"
    assert response.json()["rows"][0]["day"] == "2026-11-01"
    assert get(client, token, {"from": "2026-08-04", "to": "2026-11-02"}).status_code == 400
    ny_store.close()
    service.store = store


def test_immutable_pages_cursor_binding_and_expiry(endpoint):
    client, state, store, _, token, clock = endpoint
    populate(store)
    state.backend.snapshots["home-mac"]["workspaces"] = [{"id": "space-a", "label": "Space A"}]
    first = get(client, token).json()
    cursor = first["next_cursor"]
    assert cursor and len(first["rows"]) == 512
    expected = store.report_snapshot(
        UsageQuery("home-mac", **{"from_day": PARAMS["from"], "to_day": PARAMS["to"]}), NOW
    )
    write(
        store,
        (UsageEvent("secret-session", "new", NOW, "model-9999", Counters(5), Basis.PER_CALL),),
    )
    state.backend.snapshots["home-mac"]["workspaces"][0]["label"] = "Renamed after first page"
    for changed in [{"space_id": "space-a"}, {"from": "2026-10-10"}]:
        assert get(client, token, {**PARAMS, "cursor": cursor, **changed}).json() == {
            "error": "invalid_cursor"
        }
    other, _ = state.sessions.create(
        "owner", {"sessions.read"}, {"home-mac"}, NOW, device_id="device-b"
    )
    assert get(client, other, {**PARAMS, "cursor": cursor}).status_code == 400
    rows = first["rows"]
    while cursor:
        page = get(client, token, {**PARAMS, "cursor": cursor}).json()
        assert page["generated_ms"] == first["generated_ms"]
        assert page["kinds"] == first["kinds"] and page["coverage"] == first["coverage"]
        assert (
            page["workspaces"]
            == first["workspaces"]
            == [{"workspace_id": "space-a", "label": "Space A", "last_used_day": "2026-10-11"}]
        )
        rows += page["rows"]
        cursor = page["next_cursor"]
    assert len(rows) == len(expected.rows) == 1200
    assert not any(r["model"] == "model-9999" for r in rows)
    clock[0] += CURSOR_TTL_MS
    assert get(client, token, {**PARAMS, "cursor": first["next_cursor"]}).json() == {
        "error": "invalid_cursor"
    }


def test_disabled_closed_store_and_bounded_snapshots(endpoint):
    client, state, store, service, token, _ = endpoint
    state.usage = None
    assert get(client, token).json() == {"error": "usage_disabled"}
    assert get(client, token).status_code == 404
    state.usage = service
    populate(store)
    first = get(client, token).json()
    for _ in range(MAX_REPORTS):
        assert get(client, token).status_code == 200
    # identical polls reuse one snapshot while nothing changed,
    # so the store stays at one report instead of churning to the cap.
    assert len(service._reports) == 1
    assert get(client, token, {**PARAMS, "cursor": first["next_cursor"]}).status_code == 200
    store.close()
    assert get(client, token).json() == {"error": "usage_unavailable"}
    assert get(client, token).status_code == 503


@pytest.mark.parametrize("change", ["device", "account", "host_owner"])
def test_offloop_bounded_admission_and_revoke_during_query(endpoint, monkeypatch, change):
    _, state, _, service, token, _ = endpoint
    started, release = threading.Event(), threading.Event()
    original = service._report

    def delayed(query, now):
        started.set()
        assert release.wait(3)
        return original(query, now)

    monkeypatch.setattr(service, "_report", delayed)

    async def race():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            task = asyncio.create_task(http.get(URL, params=PARAMS, headers=auth(token)))
            while not started.is_set():
                await asyncio.sleep(0.001)
            # Request handling stays responsive; an identical second request shares the running
            # computation instead of failing.
            second = asyncio.create_task(http.get(URL, params=PARAMS, headers=auth(token)))
            await asyncio.sleep(0.05)
            assert not second.done()
            if change == "device":
                state.sessions.revoke_device("owner", "device-a")
            elif change == "account":
                state.sessions.revoke_account("owner")
            else:
                state.hosts.enroll("home-mac", account="replacement-owner")
            release.set()
            expected = 404 if change == "host_owner" else 401
            assert (await task).status_code == expected
            assert (await second).status_code == expected

    asyncio.run(race())


@pytest.mark.parametrize(
    "params",
    [
        {"days": "0"},
        {"days": "91"},
        {"days": "-1"},
        {"days": "01"},
        {"days": "true"},
        {"days": "1", "from": "2026-10-11"},
        {"days": "7", **PARAMS},
        [("days", "1"), ("days", "1")],
    ],
)
def test_days_requires_unique_integer_alternative(endpoint, params):
    client, _, _, _, token, _ = endpoint
    assert client.get(URL, params=params, headers=auth(token)).json() == {"error": "invalid_query"}


def test_days_uses_host_calendar_not_the_device_date(endpoint, tmp_path):
    client, state, store, service, token, clock = endpoint
    service.config = replace(service.config, timezone="America/New_York")
    # UTC October 11 is still October 10 in this host timezone.
    clock[0] = NOW + 2 * 3600_000
    token, _ = state.sessions.create("owner", {"sessions.read"}, {"home-mac"}, clock[0])
    with SqliteUsageStore(
        tmp_path / "local-state/usage.db", "America/New_York", clock=lambda: clock[0]
    ) as local:
        service.store = local
        write(
            local,
            (UsageEvent("unlinked", "e-local", clock[0], "model-a", Counters(2), Basis.PER_CALL),),
        )
        response = client.get(URL, params={"days": 1}, headers=auth(token))
        assert response.status_code == 200
        assert {row["day"] for row in response.json()["rows"]} == {"2026-10-10"}
        assert client.get(URL, params={"days": 90}, headers=auth(token)).status_code == 200
    service.store = store


def test_days_pagination_keeps_original_snapshot_across_midnight(endpoint):
    client, state, store, _, _, clock = endpoint
    clock[0] = NOW + 86_399_000
    token, _ = state.sessions.create("owner", {"sessions.read"}, {"home-mac"}, clock[0])
    populate(store)
    first = client.get(URL, params={"days": 1}, headers=auth(token)).json()
    assert first["next_cursor"]
    clock[0] += 2000
    params = {"days": 1, "cursor": first["next_cursor"]}
    next_page = client.get(URL, params=params, headers=auth(token))
    assert next_page.status_code == 200
    assert next_page.json()["generated_ms"] == first["generated_ms"]
    assert {row["day"] for row in next_page.json()["rows"]} == {"2026-10-11"}
    assert client.get(URL, params={**params, "days": 7}, headers=auth(token)).status_code == 400


def test_space_filter_workspace_only_live_labels_and_fallback(endpoint):
    client, state, store, _, token, _ = endpoint
    populate(store, 1)
    write(store, (UsageEvent("unlinked", "other", NOW, "model-b", Counters(3), Basis.PER_CALL),))
    snapshot = state.backend.snapshots["home-mac"]
    snapshot["workspaces"] = [{"id": "space-a", "label": "Old label"}]
    partial = translate_event(
        "workspace.renamed",
        {"workspace_id": "space-a", "label": "Space A"},
        epoch=snapshot["host"]["epoch"],
        event_id="rename-1",
        observed_at="2026-10-11T00:00:00Z",
    )
    state.backend.publish("home-mac", {**partial, "host_id": "home-mac"})
    all_spaces = get(client, token).json()
    assert {r["scope"] for r in all_spaces["rows"]} == {"overall", "workspace", "not_linked"}
    assert all_spaces["workspaces"] == [
        {"workspace_id": "space-a", "label": "Space A", "last_used_day": "2026-10-11"}
    ]
    selected = get(client, token, {**PARAMS, "space_id": "space-a"}).json()
    assert {r["scope"] for r in selected["rows"]} == {"workspace"}
    assert sum(r["counters"]["input_uncached"] for r in selected["rows"]) == 10
    state.backend.event_logs.clear()
    snapshot["workspaces"] = []
    fallback = get(client, token, {**PARAMS, "space_id": "space-a"}).json()
    assert fallback["workspaces"] == [
        {"workspace_id": "space-a", "label": "space-a", "last_used_day": "2026-10-11"}
    ]
    assert get(client, token, {**PARAMS, "space_id": "missing-space"}).json()["rows"] == []


def test_web_companion_route_forwarding_preserves_session_guard(endpoint):
    from relaydeck.client.serve import build_web_app

    _, state, store, _, token, _ = endpoint
    populate(store, 1)

    async def main(page):
        pass

    web, _ = build_web_app(
        gateway_state=state,
        gateway_app=create_app(state),
        flet_main=main,
        public_origin="https://test-mac.synthetic-tailnet.ts.net",
        cookie_secret=b"usage5-synthetic-private-cookie-key",
    )
    with TestClient(web) as client:
        assert client.get(URL, params={"days": 1}, headers=auth(token)).status_code == 200
        assert client.get(URL, params={"days": 1}).status_code == 401


def test_setup_evidence_is_frozen_and_independent_of_missing_quota(endpoint, tmp_path):
    from relaydeck.usage.hook_setup import HookSetupCheck

    client, _, _, service, token, clock = endpoint
    timer = [0.0]
    service._checks["kind-a", "jsonl-message-usage-v1"] = HookSetupCheck(
        tmp_path.resolve() / "settings",
        tmp_path.resolve() / "cache",
        tmp_path.resolve() / "missing-hook.py",
        monotonic=lambda: timer[0],
    )
    service.check_setup()  # The same startup read-only check invoked by the runtime.
    first = get(client, token).json()
    assert first["kinds"][0]["limits_setup"] == {"state": "script_missing", "checked_ms": NOW}
    assert not any(s["windows"] for s in first["limits"])
    clock[0] += 1000
    timer[0] = 59
    assert get(client, token).json()["kinds"][0]["limits_setup"]["checked_ms"] == NOW
    timer[0] = 60
    assert get(client, token).json()["kinds"][0]["limits_setup"]["checked_ms"] == NOW + 1000


def test_cancelled_request_keeps_query_admission_until_worker_finishes(endpoint, monkeypatch):
    _, _, _, service, _, _ = endpoint
    started, release = threading.Event(), threading.Event()
    original = service._report

    def delayed(query, now):
        started.set()
        assert release.wait(3)
        return original(query, now)

    monkeypatch.setattr(service, "_report", delayed)

    async def check():
        query = UsageQuery("home-mac", "2026-10-11", "2026-10-11")
        task = asyncio.create_task(service.page(query, "owner", "device-a"))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The worker still holds the slot: a different report waits, then gets `usage_busy`.
        service.wait_s = 0.05
        other = UsageQuery("home-mac", "2026-10-10", "2026-10-10")
        refused = await service.page(other, "owner", "device-a")
        assert refused.status_code == 503
        assert refused.headers["Retry-After"] == "2"
        assert json.loads(refused.body) == {"error": "usage_busy"}
        release.set()
        while service._busy:
            await asyncio.sleep(0.001)
        assert (await service.page(query, "owner", "device-a")).status_code == 200

    asyncio.run(check())


# Concurrent requests wait instead of failing -------------------------------


def _slow_report(service, monkeypatch, started, release):
    original = service._report
    calls = []

    def delayed(query, now):
        calls.append(query)
        started.set()
        assert release.wait(3)
        return original(query, now)

    monkeypatch.setattr(service, "_report", delayed)
    return calls


def test_two_concurrent_identical_requests_both_succeed_with_one_computation(endpoint, monkeypatch):
    _, _, _, service, _, _ = endpoint
    started, release = threading.Event(), threading.Event()
    calls = _slow_report(service, monkeypatch, started, release)

    async def check():
        query = UsageQuery("home-mac", "2026-10-11", "2026-10-11")
        first = asyncio.create_task(service.page(query, "owner", "device-a"))
        while not started.is_set():
            await asyncio.sleep(0.001)
        second = asyncio.create_task(service.page(query, "owner", "device-a"))
        await asyncio.sleep(0.02)
        assert not second.done()
        release.set()
        one, two = await first, await second
        assert one.status_code == two.status_code == 200
        assert one.body == two.body
        assert len(calls) == 1  # coalesced: one computation served both

    asyncio.run(check())


def test_two_concurrent_different_requests_both_succeed_in_turn(endpoint, monkeypatch):
    _, _, _, service, _, _ = endpoint
    started, release = threading.Event(), threading.Event()
    calls = _slow_report(service, monkeypatch, started, release)

    async def check():
        today = UsageQuery("home-mac", "2026-10-11", "2026-10-11")
        week = UsageQuery("home-mac", "2026-10-05", "2026-10-11")
        first = asyncio.create_task(service.page(today, "owner", "device-a"))
        while not started.is_set():
            await asyncio.sleep(0.001)
        second = asyncio.create_task(service.page(week, "owner", "device-a"))
        await asyncio.sleep(0.02)
        assert not second.done()  # queued behind the running one, not refused
        release.set()
        assert (await first).status_code == 200
        assert (await second).status_code == 200
        assert len(calls) == 2

    asyncio.run(check())


def test_bounded_wait_expiry_is_usage_busy_with_retry_after(endpoint, monkeypatch):
    client, state, _, service, token, _ = endpoint
    started, release = threading.Event(), threading.Event()
    _slow_report(service, monkeypatch, started, release)
    service.wait_s = 0.05

    async def race():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            first = asyncio.create_task(http.get(URL, params=PARAMS, headers=auth(token)))
            while not started.is_set():
                await asyncio.sleep(0.001)
            other = {"from": "2026-10-05", "to": "2026-10-11"}
            busy = await http.get(URL, params=other, headers=auth(token))
            assert busy.status_code == 503
            assert busy.json() == {"error": "usage_busy"}
            assert busy.headers["Retry-After"] == "2"
            release.set()
            assert (await first).status_code == 200

    asyncio.run(race())
