"""`pairing approve --code` without `--request` approves only an exact,
unambiguous match; zero or several matches approve nothing and list the candidates.

The store and host records are stubs, so no state file is touched.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from relaydeck.companion import pairing as pairing_module


class _Store:
    rows: list[dict[str, Any]] = []
    decided: list[tuple[str, str]] = []

    def __init__(self, _path: Path) -> None:
        pass

    def pending(self, *_args: Any) -> list[dict[str, Any]]:
        return list(_Store.rows)

    def decide(self, request_id: str, code: str, *_args: Any, approve: bool) -> None:
        _Store.decided.append((request_id, code))

    def close_window(self) -> None:
        return None


class _Hosts:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record_for_host(self, _host_id: str) -> object:
        return object()


def _row(request_id: str, code: str, label: str = "Phone") -> dict[str, Any]:
    return {
        "request_id": request_id,
        "matching_code": code,
        "device_label": label,
        "device_id": f"device-{request_id}",
        "host_id": "home-mac",
        "host_fingerprint": "fp",
        "capabilities": ["sessions.read"],
        "state": "pending",
        "expires_at": 9_999_999,
    }


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    _Store.rows = []
    _Store.decided = []
    monkeypatch.setattr(pairing_module, "PairingStore", _Store)
    monkeypatch.setattr(pairing_module, "HostStore", _Hosts)
    monkeypatch.setattr(pairing_module, "known_host", lambda _record: _StubHost())
    monkeypatch.setattr(pairing_module.time, "time", lambda: 1_000.0)


class _StubHost:
    host_id = "home-mac"
    fingerprint = "fp"


def _config(tmp_path: Path) -> Any:
    class _Config:
        state_dir = tmp_path
        advertised_origin = "https://mac.example.ts.net"
        host_id = "home-mac"

    return _Config()


def _args(**kwargs: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"pairing_action": "approve", "request": None, "code": "123-456"}
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_one_exact_match_is_approved_without_naming_the_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _Store.rows = [_row("req-a", "123-456"), _row("req-b", "654-321")]
    assert pairing_module.run_pairing(_config(tmp_path), _args()) == 0
    assert _Store.decided == [("req-a", "123-456")]
    assert "Device request approved" in capsys.readouterr().out


def test_no_match_approves_nothing_and_lists_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _Store.rows = [_row("req-a", "111-222"), _row("req-b", "333-444")]
    assert pairing_module.run_pairing(_config(tmp_path), _args(code="999-000")) == 1
    assert _Store.decided == []
    output = capsys.readouterr().out
    assert "Nothing was approved" in output
    assert "req-a" in output and "req-b" in output


def test_several_matches_approve_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _Store.rows = [_row("req-a", "123-456"), _row("req-b", "123-456")]
    assert pairing_module.run_pairing(_config(tmp_path), _args()) == 1
    assert _Store.decided == []
    assert "2 matched" in capsys.readouterr().out


def test_no_pending_requests_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert pairing_module.run_pairing(_config(tmp_path), _args()) == 1
    assert _Store.decided == []
    assert "no pending device requests" in capsys.readouterr().out


def test_deny_still_needs_an_explicit_request(tmp_path: Path) -> None:
    _Store.rows = [_row("req-a", "123-456")]
    with pytest.raises(ValueError, match="deny needs --request"):
        pairing_module.run_pairing(_config(tmp_path), _args(pairing_action="deny", code="123-456"))
    assert _Store.decided == []
