"""Visibility hints are page-bound; idle input never changes the writer."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from relaydeck.client import serve
from relaydeck.client.features.auth.web_recovery import RECOVERY_SCRIPT
from relaydeck.client.features.terminal.input_modes import WORKFLOW_AGENT_DIRECT
from relaydeck.client.resources import AppResources
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.contracts.wire.terminal import decode_input
from tests.support.gateway.flet_gateway import _gateway_state
from tests.support.scripted_sessions import make_scripted_sessions

ORIGIN = "https://private.test"


def _resources(now: list[float]) -> AppResources:
    return AppResources(
        props={},
        client=None,
        owns_client=False,
        retained=RetainedSessions(),
        bridge=None,
        controller=None,
        transport=None,
        commands=None,
        _clock=lambda: now[0],
    )  # type: ignore[arg-type]


async def _web(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    now = [1000.0]
    resources = _resources(now)
    store = serve.WebSessionStore(
        ORIGIN,
        b"synthetic-cookie-key" * 2,
        now_ms=lambda: int(now[0] * 1000),
        _visibility_clock=lambda: now[0],
    )
    sid = store.create_session("owner", "device", "access", "refresh", 2_000_000)
    # Activation renders, so it must run as a page task, never inline in the request.
    page = SimpleNamespace(
        session=SimpleNamespace(id="own-page", connection=object()),
        fonts={},
        run_task=lambda handler: asyncio.ensure_future(handler()),
    )
    store.bind_flet(sid, page.session.id, serve._Handshake("", "synthetic", ""))
    monkeypatch.setattr("relaydeck.client.app.run_app", lambda *a, **kw: resources)
    from relaydeck.gateway.app import create_app

    state = _gateway_state()
    gateway = create_app(state)
    main = serve.make_relaydeck_main(
        store=store,
        gateway_state=state,
        gateway_app=gateway,
        base_url=ORIGIN,
        host_id="home-mac",
    )
    main(page)
    live = {store.unique_key(page.session.id): page.session}

    async def get_session(key: str) -> Any:
        return live.get(key)

    monkeypatch.setattr("flet.fastapi.app_manager.get_session", get_session)
    app, _ = serve.build_web_app(
        gateway_state=state,
        gateway_app=gateway,
        flet_main=main,
        public_origin=ORIGIN,
        cookie_secret=store.cookie_secret,
        store=store,
    )
    cookie = store.cookie_headers(sid)["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    return app, store, resources, now, live, cookie, sid


@pytest.mark.parametrize(
    "case",
    [
        "no-cookie",
        "bad-cookie",
        "no-origin",
        "wrong-origin",
        "duplicate-origin",
        "foreign-page",
        "unknown-page",
        "dead-page",
        "replaced-page",
        "revoked",
    ],
)
def test_visibility_auth_matrix_has_one_uniform_no_effect_response(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    async def scenario() -> None:
        app, store, resources, now, live, cookie, sid = await _web(monkeypatch)
        headers = [("Origin", ORIGIN)]
        body = {"state": "visible", "persisted": False, "flet": "own-page"}
        if case == "no-cookie":
            cookie = ""
        elif case == "bad-cookie":
            cookie += "invalid"
        elif case == "no-origin":
            headers = []
        elif case == "wrong-origin":
            headers = [("Origin", "https://foreign.test")]
        elif case == "duplicate-origin":
            headers *= 2
        elif case == "foreign-page":
            other = store.create_session("other", "other-device", "a", "r", 2_000_000)
            store.bind_flet(other, "foreign-page", serve._Handshake("", "synthetic", ""))
            store._visibility_pages["foreign-page"] = serve._PageVisibility(
                store.identity(other), object(), lambda *_: pytest.fail("foreign callback")
            )
            body["flet"] = "foreign-page"
        elif case == "unknown-page":
            body["flet"] = "unknown-page"
        elif case == "dead-page":
            live.clear()
        elif case == "replaced-page":
            live[store.unique_key("own-page")] = object()
        elif case == "revoked":
            store.retire_local(sid)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as http:
            if cookie:
                headers.append(("Cookie", "rd_session=" + cookie))
            response = await http.post("/v1/ui/visibility", headers=headers, json=body)
        assert response.status_code == 204 and response.content == b""
        assert response.headers["cache-control"] == "no-store"
        assert resources.activations == 0 and resources._hidden_at is None

    asyncio.run(scenario())


def test_own_page_visibility_logs_and_activates_without_pausing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        app, store, resources, now, live, cookie, sid = await _web(monkeypatch)
        assert resources.retained.device_foreground
        diagnostic_log = serve.diagnostics_logger()
        monkeypatch.setattr(diagnostic_log, "propagate", True)
        with caplog.at_level(logging.INFO, logger=diagnostic_log.name):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=ORIGIN,
                headers={"Origin": ORIGIN, "Cookie": "rd_session=" + cookie},
            ) as http:
                hidden = {"state": "hidden", "persisted": False, "flet": "own-page"}
                assert (await http.post("/v1/ui/visibility", json=hidden)).status_code == 204
                assert resources.activations == 0 and resources._hidden_at == now[0]
                assert resources.retained.device_foreground
                now[0] += 20
                visible = {**hidden, "state": "visible", "persisted": True}
                assert (await http.post("/v1/ui/visibility", json=visible)).status_code == 204
                assert resources.activations == 0  # not inline, outside the page context
                await asyncio.sleep(0)
        assert resources.activations == 1 and resources._hidden_at is None
        assert resources.retained.device_foreground
        assert "ui_visibility state=hidden persisted=False since_hide_ms=0" in caplog.text
        assert "ui_visibility state=visible persisted=True since_hide_ms=20000" in caplog.text
        assert "own-page" not in caplog.text and cookie not in caplog.text

    asyncio.run(scenario())


def test_a_visible_report_before_the_socket_reconnects_leaves_activation_to_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app, store, resources, now, live, cookie, sid = await _web(monkeypatch)
        next(iter(live.values())).connection = None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, "Cookie": "rd_session=" + cookie},
        ) as http:
            visible = {"state": "visible", "persisted": False, "flet": "own-page"}
            assert (await http.post("/v1/ui/visibility", json=visible)).status_code == 204
            await asyncio.sleep(0)
        assert resources.activations == 0

    asyncio.run(scenario())


def test_visibility_limits_reports_per_page_without_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app, store, resources, now, live, cookie, sid = await _web(monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, "Cookie": "rd_session=" + cookie},
        ) as http:
            body = {"state": "visible", "persisted": False, "flet": "own-page"}
            for _ in range(12):
                assert (await http.post("/v1/ui/visibility", json=body)).status_code == 204
            await asyncio.sleep(0)
            assert resources.activations == 4
            now[0] += 1
            await http.post("/v1/ui/visibility", json=body)
            await asyncio.sleep(0)
            assert resources.activations == 5

    asyncio.run(scenario())


def test_page_must_still_be_live_after_identity_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app, store, resources, now, live, cookie, sid = await _web(monkeypatch)
        observe = store.observe
        reads = 0

        def revalidate(value: str) -> Any:
            nonlocal reads
            reads += 1
            if reads == 2:
                live.clear()
            return observe(value)

        monkeypatch.setattr(store, "observe", revalidate)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, "Cookie": "rd_session=" + cookie},
        ) as http:
            response = await http.post(
                "/v1/ui/visibility",
                json={"state": "visible", "persisted": False, "flet": "own-page"},
            )
        assert response.status_code == 204 and reads == 2
        assert resources.activations == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"not-json",
        b"[]",
        b"x" * 513,
        b'{"state":"visible","persisted":0,"flet":"own-page"}',
        b'{"state":"visible","persisted":false,"flet":null}',
        b'{"state":"hidden","state":"visible","persisted":false,"flet":"own-page"}',
    ],
)
def test_visibility_ignores_malformed_and_oversize_bodies(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    async def scenario() -> None:
        app, store, resources, now, live, cookie, sid = await _web(monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, "Cookie": "rd_session=" + cookie},
        ) as http:
            response = await http.post("/v1/ui/visibility", content=raw)
        assert response.status_code == 204
        assert resources.activations == 0 and resources._hidden_at is None

    asyncio.run(scenario())


def test_idle_input_activates_once_and_sustained_typing_never_probes() -> None:
    now = [1000.0]
    resources = _resources(now)
    resources.activate("connect", probe=False)
    for _ in range(400):
        now[0] += 0.1
        resources.note_input()
    assert resources.activations == 1
    now[0] += 20
    resources.note_input()
    resources.note_input()
    assert resources.activations == 2
    resources.note_hidden()
    now[0] += 20
    resources.note_input()
    assert resources.activations == 3 and resources._hidden_at is None
    resources.closing = True
    now[0] += 20
    resources.note_input()
    assert resources.activations == 3


@pytest.mark.parametrize("idle", [0, 20])
def test_input_reads_the_clock_only_once(idle: int) -> None:
    now = [1000.0]
    resources = _resources(now)
    reads: list[None] = []

    def clock() -> float:
        reads.append(None)
        return now[0]

    resources._clock = clock
    now[0] += idle
    resources.note_input()
    assert len(reads) == 1 and resources.activations == int(idle > 0)


def test_input_observation_failure_is_content_free_and_does_not_escape(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    resources = _resources(now)
    monkeypatch.setattr(
        "relaydeck.client.resources.diagnostics_logger",
        lambda: logging.getLogger("visibility-test"),
    )

    def fail(reason: str, **kwargs: object) -> None:
        raise RuntimeError("synthetic-bearer-cookie-path")

    resources.activate = fail
    now[0] += 20
    resources.note_input()
    assert resources._last_input == now[0]
    assert "ui_activation_failed reason=input" in caplog.text
    assert "synthetic-bearer-cookie-path" not in caplog.text


def test_composer_edit_and_send_are_signs_of_life_without_extra_delivery() -> None:
    async def scenario() -> None:
        now = [1000.0]
        resources = _resources(now)
        ports = make_scripted_sessions()
        ports.factory.note_input = resources.note_input
        session = ports.factory.make_agent(
            host_id="mac", host_epoch="h1", agent_id="pane", target="pane"
        )
        try:
            now[0] += 20
            session.on_draft_change("exact draft\nsecond line")
            assert resources.activations == 1
            now[0] += 20
            await session.on_send()
            assert resources.activations == 2
            assert ports.prompted == [("pane", "exact draft\nsecond line")]
            assert len(ports.submitted) == 1
            assert ports.submitted[0]["payload"] == {"text": "exact draft\nsecond line"}
            assert ports.streams.opened == []
        finally:
            await session.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_idle_input_preserves_exact_bytes_order_and_writer_identity(kind: str) -> None:
    from relaydeck_terminal.protocol import ORIGIN_TEXT

    async def scenario() -> None:
        now = [1000.0]
        resources = _resources(now)
        ports = make_scripted_sessions()
        ports.factory.note_input = resources.note_input
        if kind == "agent":
            session = ports.factory.make_agent(
                host_id="mac", host_epoch="h1", agent_id="pane", target="pane"
            )
            session.open()
            assert await session.confirm_takeover() == "live"
            session.set_workflow(WORKFLOW_AGENT_DIRECT)
        else:
            session = ports.factory.make_shell(host_id="mac")
            assert await session.open("pane") == "live"
        try:
            controller, token = session.controller, session.input_token
            resources.retained.foreground = SimpleNamespace(session=session)
            requested: list[str] = []
            resources.recovery = SimpleNamespace(
                request=lambda entry, reason: requested.append(reason)
            )
            resources._reconcile_terminal = lambda *a, **kw: pytest.fail("input must not fence")
            now[0] += 20
            assert await session.handle_view_input(ORIGIN_TEXT, b"a", token=token) == "pty"
            assert await session.press_key("enter")
            assert await session.handle_view_input(ORIGIN_TEXT, b"hello", token=token) == "pty"
            frames = [
                decode_input(data).data
                for _, _, socket in ports.streams.opened
                for typ, data in socket.sent
                if typ == "bytes"
            ]
            assert frames == [b"a", b"\r", b"hello"]
            assert resources.activations == 1 and requested == ["input"]
            assert session.controller is controller and session.input_token == token
            assert not controller.ui_input_fenced
        finally:
            await session.dispose()

    asyncio.run(scenario())


def test_visibility_script_behavior_preserves_pairing_and_recovery(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for script behavior")
    script = tmp_path / "visibility.cjs"
    script.write_text(
        """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = SCRIPT;
const docEvents = {}, winEvents = {}, sent = [], storage = new Map([['_flet_session_id','tab-id']]);
const document = {visibilityState:'visible', addEventListener:(k, v) => docEvents[k]=v};
const window = {WebSocket:class {}, sessionStorage:{getItem:k=>storage.get(k)},
  location:{pathname:'/agents'},
  addEventListener:(k,v)=>winEvents[k]=v};
const fetch = (url, options) => {sent.push([url, options]); return Promise.resolve({status:204});};
vm.runInNewContext(source, {window, document, fetch});
async function check() {
  await Promise.resolve(); await Promise.resolve();
  const oldPairing = sent.filter(([u]) => u !== '/v1/ui/visibility');
  document.visibilityState='hidden'; docEvents.visibilitychange();
  document.visibilityState='visible'; docEvents.visibilitychange();
  winEvents.pageshow({persisted:true});
  winEvents.pageshow({persisted:false});
  const reports = sent.filter(([u]) => u === '/v1/ui/visibility');
  assert.equal(reports.length,4);
  assert.deepEqual(reports.map(([,o])=>JSON.parse(o.body)),[
    {state:'hidden',persisted:false,flet:'tab-id'},
    {state:'visible',persisted:false,flet:'tab-id'},
    {state:'visible',persisted:true,flet:'tab-id'},
    {state:'visible',persisted:false,flet:'tab-id'}]);
  for (const [,o] of reports) {
    assert.equal(o.method,'POST'); assert.equal(o.credentials,'same-origin');
    assert.equal(o.cache,'no-store'); assert.equal(o.keepalive,true);
  }
  assert.equal(oldPairing[0][0],'/v1/auth/pairing/browser');
  assert(sent.some(([u])=>u === '/v1/auth/pairing/complete'));
  window.sessionStorage.getItem=()=>{throw Error('storage');};
  document.visibilityState='hidden';
  docEvents.visibilitychange();
}
check().catch(e=>{console.error(e);process.exitCode=1;});
""".replace("SCRIPT", json.dumps(RECOVERY_SCRIPT))
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
