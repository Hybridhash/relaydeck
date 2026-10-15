"""The documented operator enrollment ceremony, end to end.

`issue-enrollment --store` (operator CLI process) persists the invitation
into the file the serving `serve --enrollment-store` process reads, so the
connector's redeem succeeds. Proven two ways: exact-argv in-process
round-trips plus one full two-process test (real serve subprocess + real
CLI subprocess + HTTP redeem). Synthetic loopback only.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from relaydeck.connector.pubkey import (
    fingerprint_of_raw,
    generate,
    openssh_to_raw,
    raw_to_openssh,
)
from relaydeck.gateway import cli as gateway_cli
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from relaydeck.gateway.identity import EnrollmentStore
from tests.security.helpers import SECRET, snapshot_doc
from tests.support.companion.enrollment import _redeem_body

NOW = 1_000_000
CLI = [sys.executable, "-m", "relaydeck.gateway.cli"]


def _serving_app(store_path: str) -> tuple[TestClient, GatewayState]:
    backend = FakeBackend(now_ms=lambda: NOW)
    backend.snapshots["home-mac"] = snapshot_doc("sec-cursor-home-mac")
    state = GatewayState(
        backend=backend,
        cursor_secret=SECRET,
        now_ms=lambda: NOW,
        enrollments=EnrollmentStore(path=store_path),
    )
    return TestClient(create_app(state)), state


def _parse_issue(out: str) -> tuple[str, str]:
    invitation_id = code = ""
    for line in out.splitlines():
        if line.startswith("id "):
            invitation_id = line.split(" ", 1)[1].strip()
        if line.startswith("code "):
            code = line.split(" ", 1)[1].strip()
    assert invitation_id.startswith("enr_"), out
    assert len(code) == 14 and code.count("-") == 2, out  # XXXX-XXXX-XXXX
    return invitation_id, code


def test_issue_argv_persists_and_redeems(tmp_path: Any, capsys: Any) -> None:
    """Exact documented argv (plus --store): the serving process redeems it."""
    store = str(tmp_path / "enrollments.json")
    seed, public = generate()
    argv = [
        "issue-enrollment",
        "--host-label",
        "Home Mac",
        "--pubkey",
        public,
        "--account",
        "owner",
        "--origin",
        "https://gw.example",
        "--ttl-seconds",
        "600",
        "--store",
        store,
    ]
    assert gateway_cli.main(argv) == 0
    out = capsys.readouterr().out
    invitation_id, code = _parse_issue(out)
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600

    client, state = _serving_app(store)
    redeem = client.post(
        f"/v1/enrollments/{invitation_id}/redeem",
        json=_redeem_body(seed, public, invitation_id, code, "home-mac"),
    )
    assert redeem.status_code == 200, redeem.text
    assert redeem.json()["fingerprint"] == fingerprint_of_raw(openssh_to_raw(public))
    assert state.hosts.record_for_host("home-mac") is not None
    # Single-use holds across the shared file.
    replay = client.post(
        f"/v1/enrollments/{invitation_id}/redeem",
        json=_redeem_body(seed, public, invitation_id, code, "home-mac"),
    )
    assert replay.json()["code"] == "not_found"


def test_issue_without_store_stays_throwaway(capsys: Any) -> None:
    """No --store: prints the invitation but warns it cannot redeem."""
    _, public = generate()
    assert gateway_cli.main(["issue-enrollment", "--host-label", "H", "--pubkey", public]) == 0
    err = capsys.readouterr().err
    assert "--store" in err and "demo store" in err


def test_attempt_burn_shared_across_instances(tmp_path: Any) -> None:
    store = str(tmp_path / "enrollments.json")
    cli_side = EnrollmentStore(path=store)
    _, public = generate()
    raw = openssh_to_raw(public)
    invitation = cli_side.create("owner", "Home", raw_to_openssh(raw), fingerprint_of_raw(raw), NOW)
    serving_side = EnrollmentStore(path=store)
    for _ in range(5):
        assert serving_side.attempt(invitation.id, "ZZZZZZZZZZZZ", NOW) is None
    assert serving_side.attempt(invitation.id, invitation.code, NOW) is None  # burned
    # The burn persisted: a third reader agrees.
    assert EnrollmentStore(path=store).attempt(invitation.id, invitation.code, NOW) is None


def test_corrupt_store_fails_closed_then_recovers(tmp_path: Any) -> None:
    store = str(tmp_path / "enrollments.json")
    with open(store, "w", encoding="utf-8") as handle:
        handle.write("{corrupt")
    assert EnrollmentStore(path=store).attempt("enr_x", "AAAAAAAAAAAA", NOW) is None
    _, public = generate()
    raw = openssh_to_raw(public)
    invitation = EnrollmentStore(path=store).create(
        "owner", "Home", raw_to_openssh(raw), fingerprint_of_raw(raw), NOW
    )
    assert EnrollmentStore(path=store).attempt(invitation.id, invitation.code, NOW) is not None


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def test_ceremony_two_processes(tmp_path: Any) -> None:
    """Full loopback ceremony: serve subprocess + CLI subprocess + redeem."""
    store = str(tmp_path / "enrollments.json")
    port = _free_port()
    log_path = str(tmp_path / "serve.log")
    secret = secrets.token_urlsafe(32)
    seed, public = generate()
    serve = subprocess.Popen(
        [
            *CLI,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--origin",
            "https://gw.example",
            "--journal-path",
            str(tmp_path / "gw.db"),
            "--push-path",
            str(tmp_path / "push.db"),
            "--cursor-secret",
            secret,
            "--enrollment-store",
            store,
        ],
        stdout=open(log_path, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                r = httpx.get(f"{base}/healthz", timeout=2.0)
                if r.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            if serve.poll() is not None or time.monotonic() > deadline:
                logs = open(log_path, encoding="utf-8").read()
                pytest.fail(f"serve did not boot (rc={serve.poll()}): {logs[-2000:]}")
            time.sleep(0.05)

        issue = subprocess.run(
            [
                *CLI,
                "issue-enrollment",
                "--host-label",
                "Home Mac",
                "--pubkey",
                public,
                "--account",
                "owner",
                "--origin",
                "https://gw.example",
                "--ttl-seconds",
                "600",
                "--store",
                store,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert issue.returncode == 0, issue.stderr
        invitation_id, code = _parse_issue(issue.stdout)

        redeem = httpx.post(
            f"{base}/v1/enrollments/{invitation_id}/redeem",
            json=json.loads(
                json.dumps(_redeem_body(seed, public, invitation_id, code, "home-mac"))
            ),
            timeout=10.0,
        )
        assert redeem.status_code == 200, redeem.text
        body = redeem.json()
        assert body["fingerprint"] == fingerprint_of_raw(openssh_to_raw(public))
        assert body["host_token"].startswith("rdh_")

        replay = httpx.post(
            f"{base}/v1/enrollments/{invitation_id}/redeem",
            json=_redeem_body(seed, public, invitation_id, code, "home-mac"),
            timeout=10.0,
        )
        assert replay.json()["code"] == "not_found"
        assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
    finally:
        serve.terminate()
        try:
            serve.wait(timeout=15)
        except subprocess.TimeoutExpired:
            serve.kill()
            serve.wait(timeout=15)
