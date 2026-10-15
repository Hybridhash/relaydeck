"""the connector is the allowlist. A kind must be configured AND on PATH;
a shape-valid kind that is not configured is refused as agent_kind_unavailable.

Synthetic kinds and a temporary PATH only; no Herdr socket is contacted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.connector.herdr import HerdrAdapter, HerdrError
from relaydeck.connector.source import HerdrSource


def _source(kinds: tuple[str, ...]) -> HerdrSource:
    return HerdrSource(
        HerdrAdapter(socket_path="/nonexistent/synthetic.sock"),
        clock=lambda: 1000,
        agent_kinds=kinds,
    )


def _executable(directory: Path, name: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)


def test_a_configured_kind_on_path_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _executable(tmp_path, "agent-a")
    monkeypatch.setenv("PATH", str(tmp_path))
    _source(("agent-a",)).verify_agent_kind("agent-a")


def test_a_kind_on_path_but_not_configured_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _executable(tmp_path, "agent-b")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(HerdrError) as refused:
        _source(("agent-a",)).verify_agent_kind("agent-b")
    assert refused.value.code == "agent_kind_unavailable"


def test_a_pattern_valid_unconfigured_kind_is_refused_with_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _executable(tmp_path, "agent-a")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(HerdrError) as refused:
        _source(()).verify_agent_kind("agent-a")
    assert refused.value.code == "agent_kind_unavailable"
