"""Actual schema/source/client catalog integration, no executable invocation."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaydeck.client.features.create.components import StartAgentSheet
from relaydeck.client.features.sync.controller import snapshot_from_wire
from relaydeck.connector.catalog import discover_agent_catalog
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.source import HerdrSource
from relaydeck.contracts.schemas import SchemaError, validate
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState
from relaydeck.gateway.app import create_app
from tests.security.helpers import auth, mint_token
from tests.support.host_harness import render_component, run
from tests.support.sync.transport_fixtures import _state

KINDS = ("agent-a", "agent-b", "agent-c")


def test_catalog_checks_only_policy_executables_and_never_exposes_paths() -> None:
    calls: list[str] = []

    def which(name: str) -> str | None:
        calls.append(name)
        return "/synthetic/private/path/tool-a" if name == "agent-a" else None

    doc = discover_agent_catalog(1000, kinds=KINDS, which=which)
    assert calls == list(KINDS)
    assert doc["entries"][0]["available"] is True
    assert sum(entry["available"] for entry in doc["entries"]) == 1
    assert "/synthetic/" not in json.dumps(doc)


def test_catalog_detects_executable_permission_without_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "agent-a"
    marker = tmp_path / "was-executed"
    executable.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert discover_agent_catalog(1000, kinds=KINDS)["entries"][0]["available"] is True
    assert not marker.exists()
    executable.chmod(0o600)
    assert discover_agent_catalog(1001, kinds=KINDS)["entries"][0]["available"] is False
    assert not marker.exists()


def test_catalog_error_is_explicit_and_not_a_default_picker() -> None:
    def denied(name: str) -> str | None:
        raise OSError("synthetic lookup failure")

    doc = discover_agent_catalog(1000, kinds=KINDS, which=denied)
    assert doc["status"] == "error" and doc["entries"] == []
    assert discover_agent_catalog(1000, kinds=KINDS, which=lambda name: None)["status"] == "ready"


@pytest.mark.parametrize("mutation", ["unknown_kind", "path", "string_available", "bool_time"])
def test_catalog_schema_rejects_unverified_wire_shapes(mutation: str) -> None:
    doc = json.loads(Path("tests/fixtures/gateway/snapshot.json").read_text())
    catalog = discover_agent_catalog(1000, kinds=KINDS, which=lambda name: None)
    doc["host"]["agent_catalog"] = catalog
    if mutation == "unknown_kind":
        catalog["entries"][0]["kind"] = "Arbitrary Exec"
    elif mutation == "path":
        catalog["entries"][0]["path"] = "/private/file"
    elif mutation == "string_available":
        catalog["entries"][0]["available"] = "true"
    else:
        catalog["observed_ms"] = True
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/snapshot", doc)


def test_real_source_reports_catalog_and_decode_preserves_host_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.connector import catalog

    monkeypatch.setattr(
        catalog,
        "discover_agent_catalog",
        lambda now, kinds=(): discover_agent_catalog(
            now, kinds=KINDS, which=lambda name: "/synthetic/agent-b" if name == "agent-b" else None
        ),
    )
    source = HerdrSource(HerdrAdapter(), clock=lambda: 1000, boot_tag="c4", agent_kinds=KINDS)
    source._snapshot = json.loads(
        Path("tests/fixtures/upstream/session_snapshot.json").read_text()
    )["result"]["snapshot"]
    source._agents = json.loads(Path("tests/fixtures/upstream/agent_list.json").read_text())[
        "result"
    ]["agents"]
    wire = source.snapshot_document("home-mac")
    validate("relaydeck/gateway/v1/snapshot", wire)
    state = reducers.apply_snapshot(AppState(), snapshot_from_wire(wire))
    assert state.inventory.hosts["home-mac"].agent_catalog is not None
    assert state.inventory.hosts["home-mac"].agent_catalog.available == ("agent-b",)
    twin = copy.deepcopy(wire)
    twin["host"]["id"] = "other-mac"
    for group in ("workspaces", "tabs", "panes", "agents"):
        for entry in twin[group]:
            entry["host_id"] = "other-mac"
    twin["host"]["agent_catalog"] = discover_agent_catalog(
        1000, kinds=KINDS, which=lambda name: None
    )
    state = reducers.apply_snapshot(state, snapshot_from_wire(twin))
    assert state.inventory.hosts["other-mac"].agent_catalog is not None
    assert state.inventory.hosts["other-mac"].agent_catalog.available == ()
    assert state.inventory.hosts["home-mac"].agent_catalog is not None
    assert state.inventory.hosts["home-mac"].agent_catalog.available == ("agent-b",)


def test_catalog_crosses_real_authenticated_host_listing(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, backend, _ = _state(tmp_path)
        now = state.now_ms()
        backend.snapshots["home-mac"]["host"]["agent_catalog"] = discover_agent_catalog(
            now, kinds=KINDS, which=lambda name: "/synthetic/agent-a" if name == "agent-a" else None
        )
        token = mint_token(state, now=now)
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        )
        try:
            response = await http.get("/v1/hosts", headers=auth(token))
            assert response.status_code == 200
            merged = reducers.merge_hosts(AppState(), response.json()["hosts"])
            assert merged.inventory.hosts["home-mac"].agent_catalog is not None
            assert merged.inventory.hosts["home-mac"].agent_catalog.available == ("agent-a",)
            response = await http.get("/v1/hosts/home-mac/snapshot", headers=auth(token))
            assert response.status_code == 200
            validate("relaydeck/gateway/v1/snapshot", response.json())
            response = await http.get("/v1/hosts/private-other/snapshot", headers=auth(token))
            assert response.status_code == 404
        finally:
            await http.aclose()
            backend.journal.close()
            state.push.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("catalogs", [{}, {"home-mac": ()}, {"home-mac": ("agent-b",)}])
def test_real_flet_picker_uses_only_selected_host_catalog(
    catalogs: dict[str, tuple[str, ...]],
) -> None:
    async def scenario() -> None:
        submits: list[Any] = []
        app = await render_component(
            StartAgentSheet,
            hosts=(("home-mac", "Home"), ("other-mac", "Other")),
            workspaces_by_host={"home-mac": (("w1", "Project"),)},
            kinds=KINDS,
            kind_catalogs=catalogs,
            submitting=False,
            error="",
            command_status="",
            on_submit=lambda *args: submits.append(args),
            on_cancel=lambda: None,
        )
        options = app.find("agent-kind").options
        assert tuple(option.key for option in options) == catalogs.get("home-mac", ())
        assert app.find("sheet-submit").disabled is (not bool(options))
        await app.set_props_and_fire("agent-host", {"value": "other-mac"}, "select", "other-mac")
        assert app.find("agent-kind").options == []
        assert app.find("sheet-submit").disabled is True
        assert app.find("agent-catalog-empty").visible is True
        assert submits == []

    run(scenario())
