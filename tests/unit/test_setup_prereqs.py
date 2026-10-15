"""read-only prerequisites gate `relaydeck setup`; doctor shows the same results.

Every probe is injected and synthetic. No real tailnet, user, host name, socket or binary is read.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from relaydeck.companion import prereqs
from relaydeck.companion import setup as setup_mod
from relaydeck.connector.herdr import HerdrError, HerdrProtocolError
from tests.support.setup.prereqs import (
    SYNTHETIC_DNS,
    Serve,
    _done,
    _status,
    probes,
)

SYNTHETIC_TAILNET = "synthetic-tailnet.example.ts.net"
SECRET_WORDS = ("AuthURL", "login.tailscale", "alice-login", SYNTHETIC_TAILNET, "STDERR-SECRET")


def tailscale_result(p: prereqs.Probes) -> prereqs.Result:
    return prereqs.check_tailscale(p)[0]


# -- Tailscale states -----------------------------------------------------------------------


def test_a_ready_tailscale_is_ok_and_its_dns_is_normalised() -> None:
    result, dns = prereqs.check_tailscale(probes())
    assert result.state == "ok" and dns == SYNTHETIC_DNS.rstrip(".").lower()


def test_missing_cli_is_missing_with_fixed_guidance() -> None:
    def no_cli() -> str:
        raise setup_mod.tailscale.TailscaleError("anything at all")

    result = tailscale_result(probes(find_tailscale=no_cli))
    assert result.state == "missing" and result.guidance == prereqs.GUIDANCE["tailscale_missing"]


@pytest.mark.parametrize(
    ("backend", "state"),
    [
        ("Stopped", "not_running"),
        ("NeedsLogin", "not_logged_in"),
        ("NeedsMachineAuth", "needs_action"),
        ("Starting", "unknown"),
        ("NoState", "unknown"),
    ],
)
def test_backend_states_map_to_the_fixed_states(backend: str, state: str) -> None:
    assert tailscale_result(probes(Serve(_status(BackendState=backend)))).state == state


def test_a_failed_status_call_is_unknown_and_its_stderr_is_never_shown() -> None:
    result = tailscale_result(probes(Serve(status_code=1)))
    assert result.state == "unknown"
    assert "STDERR-SECRET" not in result.line()


@pytest.mark.parametrize("bad", ["not json", "[1, 2]"])
def test_malformed_status_output_is_unknown(bad: str) -> None:
    assert tailscale_result(probes(Serve(status=bad))).state == "unknown"


def test_magicdns_off_or_no_ts_net_name_needs_action() -> None:
    off = _status(CurrentTailnet={"MagicDNSEnabled": False})
    assert tailscale_result(probes(Serve(off))).guidance == prereqs.GUIDANCE["tailscale_dns"]
    no_name = _status(Self={"DNSName": "mac-a.example.com."})
    assert tailscale_result(probes(Serve(no_name))).state == "needs_action"


def test_a_name_missing_from_certdomains_needs_action() -> None:
    certs = _status(CertDomains=["other.example.ts.net"])
    assert tailscale_result(probes(Serve(certs))).state == "needs_action"


@pytest.mark.parametrize(
    "entry",
    [SYNTHETIC_DNS, SYNTHETIC_DNS.rstrip("."), SYNTHETIC_DNS.upper(), "  " + SYNTHETIC_DNS + " "],
)
def test_certdomains_membership_is_normalised(entry: str) -> None:
    result = tailscale_result(probes(Serve(_status(CertDomains=[entry]))))
    assert result.state == "ok"


# -- Serve: Funnel refusal ---------------------------------------------------------------------


def test_funnel_on_the_target_port_is_refused() -> None:
    serve = Serve(serve={"Web": {}, "AllowFunnel": {f"{SYNTHETIC_DNS.rstrip('.')}:8445": True}})
    result = prereqs.check_serve(probes(serve), 8445)
    assert result.state == "needs_action" and result.guidance == prereqs.GUIDANCE["serve_funnel"]


def test_funnel_on_another_port_does_not_block_the_target() -> None:
    serve = Serve(serve={"Web": {}, "AllowFunnel": {f"{SYNTHETIC_DNS.rstrip('.')}:443": True}})
    assert prereqs.check_serve(probes(serve), 8445).state == "ok"


def test_an_unreadable_serve_status_is_unknown() -> None:
    serve = Serve()
    serve.serve = "not a dict"  # type: ignore[assignment]  # a non-object status document
    assert prereqs.check_serve(probes(serve), 8445).state == "unknown"


# -- Herdr ---------------------------------------------------------------------------------------


def _herdr(version: str = "herdr 0.9.1", status: str = '{"running": true}', code: int = 0) -> Any:
    def run(args: Any) -> subprocess.CompletedProcess[str]:
        if list(args)[:2] == ["status", "server"]:
            return _done(args, code, status)
        return _done(args, 0, version)

    return run


def test_a_running_herdr_is_ok_and_reports_its_version_only() -> None:
    result = prereqs.check_herdr(probes(herdr=_herdr()), "/synthetic/herdr.sock")
    assert result.state == "ok" and result.detail == "0.9.1"


def test_missing_herdr_binary_is_missing() -> None:
    result = prereqs.check_herdr(probes(which=lambda name: None), "/synthetic/herdr.sock")
    assert result.state == "missing"


def test_an_unknown_herdr_version_is_unknown() -> None:
    result = prereqs.check_herdr(probes(herdr=_herdr(version="???")), "/synthetic/herdr.sock")
    assert result.state == "unknown"


def test_a_stopped_herdr_server_is_not_running() -> None:
    result = prereqs.check_herdr(probes(herdr=_herdr(status='{"running": false}')), "/s")
    assert result.state == "not_running"


def test_an_unclassifiable_herdr_status_is_unknown() -> None:
    result = prereqs.check_herdr(probes(herdr=_herdr(status="{}")), "/s")
    assert result.state == "unknown"


def test_a_missing_socket_is_not_running() -> None:
    result = prereqs.check_herdr(probes(socket_exists=lambda path: False), "/s")
    assert result.state == "not_running"


def test_a_protocol_mismatch_needs_action() -> None:
    async def ping(socket: str) -> None:
        raise HerdrProtocolError("protocol_mismatch", "expected protocol 22")

    result = prereqs.check_herdr(probes(ping=ping), "/s")
    assert result.state == "needs_action" and result.guidance == prereqs.GUIDANCE["herdr_protocol"]


def test_a_server_that_refuses_the_connection_is_not_running() -> None:
    async def ping(socket: str) -> None:
        raise ConnectionRefusedError("synthetic refusal")

    assert prereqs.check_herdr(probes(ping=ping), "/s").state == "not_running"


def test_another_herdr_error_is_unknown_not_a_success() -> None:
    async def ping(socket: str) -> None:
        raise HerdrError("bad_envelope", "synthetic")

    assert prereqs.check_herdr(probes(ping=ping), "/s").state == "unknown"


# -- uv and the runtime --------------------------------------------------------------------------


def test_uv_missing_and_uv_version_reporting() -> None:
    assert prereqs.check_uv(probes(which=lambda name: None)).state == "missing"
    ok = prereqs.check_uv(probes())
    assert ok.state == "ok" and ok.detail == "0.12.15"
    garbled = prereqs.check_uv(probes(uv=lambda args: _done(args, 0, "nonsense")))
    assert garbled.state == "unknown"


@pytest.mark.parametrize(
    ("version", "state"), [((3, 12), "needs_action"), ((3, 13), "ok"), ((3, 14), "needs_action")]
)
def test_the_runtime_python_range_is_3_13_up_to_3_14(version: tuple[int, int], state: str) -> None:
    assert prereqs.check_python(probes(python_version=version)).state == state


# -- the preflight and setup gating ---------------------------------------------------------------


def test_a_fully_ready_preflight_passes_and_reports_one_line_per_check() -> None:
    report = prereqs.run_prereqs(socket="/s", https_port=8445, probes=probes())
    assert report.ready and len(report.lines()) == 5


@pytest.mark.parametrize(
    "bad",
    [
        probes(Serve(_status(BackendState="NeedsLogin"))),
        probes(Serve(_status(BackendState="Stopped"))),
        probes(Serve(_status(BackendState="NeedsMachineAuth"))),
        probes(Serve(_status(BackendState="Starting"))),
        probes(Serve(status_code=1)),
        probes(which=lambda name: None),
        probes(python_version=(3, 12)),
        probes(uv=lambda args: _done(args, 0, "garbled")),
        probes(socket_exists=lambda path: False),
    ],
    ids=[
        "not-logged-in",
        "stopped",
        "machine-auth",
        "starting",
        "status-failed",
        "herdr-missing",
        "python-range",
        "uv-unknown",
        "herdr-socket-missing",
    ],
)
def test_every_failed_or_unknown_prerequisite_blocks_setup_with_zero_writes_and_zero_serve_calls(
    tmp_path: Path, bad: prereqs.Probes
) -> None:
    config = tmp_path / "companion.toml"
    for allowed in (False, True):
        report = setup_mod.run_setup(
            config_path=config,
            assume_yes=allowed,  # --yes never bypasses a prerequisite
            confirm=lambda _p: True,
            probes=bad,
        )
        assert report.exit_code != 0
        assert "Nothing was written" in "\n".join(report.lines)
        assert not config.exists()  # nothing created
        serve_calls = bad.tailscale.mutations if isinstance(bad.tailscale, Serve) else []
        assert serve_calls == []


def test_a_ready_setup_still_creates_the_config_after_the_preflight(tmp_path: Path) -> None:
    config = tmp_path / "companion.toml"
    asked: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return "Create the owner-only config" in prompt

    report = setup_mod.run_setup(
        config_path=config, assume_yes=False, confirm=confirm, probes=probes()
    )
    assert config.is_file()
    assert "Config created" in "\n".join(report.lines)
    assert any("Create the owner-only config" in prompt for prompt in asked)


def test_the_output_never_shows_auth_urls_users_tailnet_names_or_stderr(tmp_path: Path) -> None:
    serve = Serve(_status(BackendState="NeedsLogin"), status_code=0)
    serve.status = {
        "BackendState": "NeedsLogin",
        "AuthURL": "https://login.tailscale.com/a/synthetic",
        "User": {"LoginName": "alice-login"},
        "CurrentTailnet": {"Name": SYNTHETIC_TAILNET, "MagicDNSEnabled": True},
    }
    report = setup_mod.run_setup(
        config_path=tmp_path / "c.toml",
        assume_yes=True,
        confirm=lambda _p: True,
        probes=probes(serve),
    )
    text = "\n".join(report.lines)
    for secret in SECRET_WORDS:
        assert secret not in text
    assert prereqs.GUIDANCE["tailscale_login"] in text


def test_a_failed_status_never_prints_the_raw_stderr(tmp_path: Path) -> None:
    report = setup_mod.run_setup(
        config_path=tmp_path / "c.toml",
        assume_yes=False,
        confirm=lambda _p: False,
        probes=probes(Serve(status_code=2)),
    )
    assert "STDERR-SECRET" not in "\n".join(report.lines)


def test_usage_root_problems_are_warnings_and_never_block(tmp_path: Path) -> None:
    report = prereqs.run_prereqs(
        socket="/s", https_port=8445, probes=probes(), usage_roots=(tmp_path / "absent",)
    )
    assert report.ready
    assert any(line.startswith("warning: usage source 1") for line in report.lines())


def test_the_preflight_writes_nothing_and_only_reads_serve_status(tmp_path: Path) -> None:
    serve = Serve()
    prereqs.run_prereqs(socket="/s", https_port=8445, probes=probes(serve))
    assert serve.mutations == []
    assert not any(c[:1] == ["serve"] and c[1:2] != ["status"] for c in serve.calls)


def test_doctor_and_setup_share_the_same_results(tmp_path: Path) -> None:
    from relaydeck.companion import cli as companion_cli

    config = tmp_path / "companion.toml"
    fake = probes(Serve(_status(BackendState="NeedsLogin")))
    setup_report = setup_mod.run_setup(
        config_path=config, assume_yes=False, confirm=lambda _p: False, probes=fake
    )
    doc = setup_mod.preflight(config, fake).as_doc()
    setup_states = {
        line.split(":")[0]: line.split(":")[1].split()[0]
        for line in setup_report.lines
        if ": " in line and not line.startswith("Setup")
    }
    doctor_states = {entry["check"]: entry["state"] for entry in doc["checks"]}
    assert {k: v for k, v in setup_states.items() if k in doctor_states} == doctor_states
    assert doc["ready"] is False
    assert companion_cli is not None
