"""the manual refresh POST and its GET surface, over HTTP.

A real scheduler and service back the app; only the provider is scripted
(the Herdr-backed fetch through the same scheduler is covered in
`tests/unit/test_usage_pane_limits.py`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from relaydeck.companion.config import UsageConfig, UsageSourceConfig
from relaydeck.gateway.app import create_app
from relaydeck.gateway.usage import UsageService
from relaydeck.usage.contracts import (
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
)
from relaydeck.usage.limits_plane.contracts import (
    FetchContext,
    ProviderResult,
    ProviderSpec,
    RefreshDecision,
    TuiBundle,
    tui_recipe_from,
)
from relaydeck.usage.limits_plane.providers.tui_usage_pane import FORMAT_ID
from relaydeck.usage.limits_plane.refresh_state import RefreshState
from relaydeck.usage.limits_plane.scheduler import COOLDOWN_MS, LimitsScheduler
from relaydeck.usage.store import SqliteUsageStore
from tests.security.helpers import auth, make_state
from tests.support.usage.store import NOW

HOST = "home-mac"
KIND = "kind-a"
KEY = f"{HOST}/{KIND}/{FORMAT_ID}"
URL = f"/v1/hosts/{HOST}/usage/limits/refresh"
GET_URL = f"/v1/hosts/{HOST}/usage"
PARAMS = {"from": "2026-10-11", "to": "2026-10-11"}

# A minimal invented recipe: the scripted provider never reads it, but the
# scheduler needs a bundle to offer the kind for refresh.
RECIPE = {
    "launch_new": ["tool-a", "--fresh"],
    "quit_text": "/quit",
    "prompt_marker": "PROMPT",
    "usage_marker": "ALLOWANCE",
    "binary": "tool-a",
    "windows": [{"key": "m", "pattern": "(?P<percent>[0-9]+)%"}],
}


class _Scripted:
    format_id = FORMAT_ID

    def __init__(self) -> None:
        self.calls: list[FetchContext] = []

    def fetch(self, ctx: FetchContext) -> ProviderResult:
        self.calls.append(ctx)
        window = LimitWindow("m", 6.0, None, None, LimitStatus.FRESH, 3.0, 50.0, "credit", False)
        snapshot = LimitSnapshot(
            KIND, "q", NOW, (window,), LimitStatus.FRESH, FORMAT_ID, None, None, "P"
        )
        return ProviderResult((snapshot,))


@pytest.fixture
def refresh(tmp_path: Path) -> Any:
    state, _ = make_state(NOW)
    clock = [NOW]
    state.now_ms = lambda: clock[0]
    state.hosts.enroll(HOST, account="owner")
    root = tmp_path.resolve()
    store = SqliteUsageStore(root / "state/usage.db", "UTC", clock=lambda: clock[0])
    refresh_state = RefreshState(root / "state/refresh.json")
    bundle = TuiBundle(
        tui_recipe_from(RECIPE),
        ("/usr/bin/herdr",),
        root / "state/refresh.json",
        "UTC",
        str(root / "h.sock"),
    )
    spec = ProviderSpec(KEY, KIND, FORMAT_ID, 300_000, 90.0, on_demand=True, tui=bundle)
    provider = _Scripted()
    scheduler = LimitsScheduler(
        store,
        {KEY: spec},
        {KEY: provider},
        clock=lambda: clock[0],
        jitter=lambda: 0.5,
        refresh=refresh_state,
    )
    config = UsageConfig(
        "UTC",
        (
            UsageSourceConfig(KIND, "jsonl-message-usage-v1", root, "Kind A"),
            UsageSourceConfig("kind-b", "jsonl-message-usage-v1", root, "Kind B"),
        ),
    )

    def request_refresh(kind: str) -> RefreshDecision:
        key = scheduler.on_demand_key(kind)
        if key is None:
            return RefreshDecision("not_configured")
        return scheduler.request_on_demand(key)

    service = UsageService(
        store,
        config,
        HOST,
        root / "state",
        state.now_ms,
        limits_status=scheduler.status,
        limits_refresh_status=scheduler.refresh_status,
        request_limits_refresh=request_refresh,
    )
    state.usage = service
    token, _ = state.sessions.create("owner", {"sessions.read"}, {HOST}, NOW, device_id="device-a")
    client = TestClient(create_app(state))
    try:
        yield client, state, store, service, scheduler, provider, token, clock
    finally:
        client.close()
        asyncio.run(service.close())
        store.close()


def _post(client: TestClient, token: str, body: Any, origin: str | None = None) -> Any:
    headers = auth(token)
    if origin is not None:
        headers["origin"] = origin
    return client.post(URL, json=body, headers=headers)


def _kinds(client: TestClient, token: str) -> Any:
    response = client.get(GET_URL, params=PARAMS, headers=auth(token))
    assert response.status_code == 200
    return response.json()["kinds"]


def test_post_starts_and_joins_one_attempt(refresh: Any) -> None:
    client, _state, _store, _service, _scheduler, _provider, token, _clock = refresh
    first = _post(client, token, {"kind": KIND})
    assert first.status_code == 202
    assert first.json()["state"] == "started"
    attempt = first.json()["attempt_id"]
    assert attempt
    second = _post(client, token, {"kind": KIND})
    assert second.status_code == 202
    assert second.json() == {"state": "in_progress", "attempt_id": attempt}


def test_a_committed_attempt_cools_down_and_get_shows_it(refresh: Any) -> None:
    client, _state, _store, _service, scheduler, provider, token, clock = refresh
    attempt = _post(client, token, {"kind": KIND}).json()["attempt_id"]
    assert scheduler.run_once(clock[0]) == 1
    assert provider.calls[0].attempt_id == attempt
    kinds = {kind["kind"]: kind for kind in _kinds(client, token)}
    shown = kinds[KIND]["limits_refresh"]
    assert shown["available"] is True and shown["in_progress"] is False
    assert shown["cooldown_until_ms"] == NOW + COOLDOWN_MS
    assert shown["last_attempt"]["id"] == attempt
    assert shown["last_attempt"]["result"] == "ok"
    # The cooldown is only set after the commit, so a 429 proves the chain.
    limited = _post(client, token, {"kind": KIND})
    assert limited.status_code == 429
    assert limited.json() == {"state": "cooldown", "retry_after_s": COOLDOWN_MS // 1000}
    assert limited.headers["retry-after"] == str(COOLDOWN_MS // 1000)


def test_get_reports_not_configured_without_a_block(refresh: Any) -> None:
    client, _state, _store, _service, _scheduler, _provider, token, _clock = refresh
    kinds = {kind["kind"]: kind for kind in _kinds(client, token)}
    assert kinds["kind-b"]["limits_refresh"] == {
        "available": False,
        "reason": "not_configured",
        "in_progress": False,
        "cooldown_until_ms": None,
        "last_error": None,
        "last_attempt": None,
    }


def test_post_unknown_kind_is_not_configured(refresh: Any) -> None:
    client, _state, _store, _service, _scheduler, _provider, token, _clock = refresh
    response = _post(client, token, {"kind": "kind-zzz"})
    assert response.status_code == 404


def test_post_rejects_bad_bodies(refresh: Any) -> None:
    client, _state, _store, _service, _scheduler, _provider, token, _clock = refresh
    assert _post(client, token, {}).status_code == 400
    assert _post(client, token, {"kind": 7}).status_code == 400
    assert _post(client, token, {"kind": KIND, "extra": 1}).status_code == 400
    assert _post(client, token, {"kind": "bad\x01kind"}).status_code == 400
    raw = client.post(URL, content=b"not json", headers=auth(token))
    assert raw.status_code == 400


def test_post_rejects_a_foreign_origin(refresh: Any) -> None:
    client, state, _store, _service, _scheduler, _provider, token, _clock = refresh
    assert _post(client, token, {"kind": KIND}, origin="https://evil.example").status_code == 403
    state.public_origin = "https://mac.example-tailnet.ts.net"
    allowed = _post(client, token, {"kind": KIND}, origin=state.public_origin)
    assert allowed.status_code == 202
