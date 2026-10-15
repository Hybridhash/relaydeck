"""Connect-first adapters against temporary state; no listener or enrollment."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from relaydeck.client.adapters.fake import FakeCredentialStore
from relaydeck.client.features.pairing.service import PENDING_KEY, discard_pending
from tests.support.companion.pairing_harness import Harness
from tests.support.pairing import ORIGIN


def test_pairing_uses_real_gateway_and_keeps_secrets_out_of_controlled_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        snapshots: list[Any] = []
        h.flow.subscribe(snapshots.append)
        try:
            await h.flow.begin(ORIGIN, "home-mac", "Synthetic Android")
            assert h.flow.snapshot.phase == "ready"
            tokens, principal = h.accepted[0]
            assert principal["device_id"].startswith("device_")
            assert principal["hosts"] == ["home-mac"]
            proof = next(body for url, body in h.calls if url.endswith("/redeem"))
            assert all(proof["verifier"] not in str(row) for row in snapshots)
            assert tokens["access_token"] not in str(snapshots)
            assert any(
                row.phase == "waiting"
                and row.matching_code
                and row.fingerprint
                and row.scope
                and row.expiry
                for row in snapshots
            )
            assert not h.credentials._data
            assert len([url for url, _ in h.calls if url.endswith("/redeem")]) == 1
        finally:
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "closed",
        "denied",
        "expired",
        "restart",
        "wrong-proof",
        "extra",
        "state",
        "origin",
        "host",
        "device",
        "scope",
        "me-device",
        "me-host",
        "me-extra",
        "uncertain",
    ],
)
def test_pairing_fails_closed_without_replay_or_credential_publication(
    tmp_path: Path, fault: str
) -> None:
    async def scenario() -> None:
        h = Harness(
            tmp_path / "state",
            approve=fault not in {"denied", "expired", "restart"},
            opened=fault != "closed",
        )
        real_post, real_get = h.post, h.get

        async def post(url: str, body: Any) -> Any:
            if fault == "wrong-proof" and url.endswith("/status"):
                body = {**body, "verifier": "x" * 64}
            status, payload = await real_post(url, body)
            if url.endswith("/requests") and status == 201:
                changes = {
                    "extra": {"future": True},
                    "state": {"state": "ready"},
                    "origin": {"origin": "https://other.tail.ts.net"},
                    "host": {"host_id": "lab-mac"},
                    "scope": {"capabilities": ["hosts.enroll"]},
                }
                payload.update(changes.get(fault, {}))
            if fault == "device" and url.endswith("/status") and status == 200:
                payload["device_id"] = "device_" + "x" * 32
            if fault == "uncertain" and url.endswith("/redeem"):
                raise TimeoutError("response lost after the gateway consumed redemption")
            return status, payload

        async def get(url: str, headers: Any) -> Any:
            status, payload = await real_get(url, headers)
            changes = {
                "me-device": {"device_id": "different"},
                "me-host": {"hosts": ["lab-mac"]},
                "me-extra": {"future": True},
            }
            payload.update(changes.get(fault, {}))
            return status, payload

        async def sleep(interval: float) -> None:
            h.clock[0] += 2000
            if fault == "denied":
                h.decide(False)
            elif fault == "expired":
                h.clock[0] += 120_000
            elif fault == "restart":
                h.state.pairing.start(ORIGIN)
            else:
                await h.sleep(interval)

        h.flow.post, h.flow.get, h.flow.sleep = post, get, sleep
        try:
            await h.flow.begin(ORIGIN, "home-mac", "Synthetic browser")
            assert h.flow.snapshot.phase == "failed"
            assert not h.accepted and not h.credentials._data
            assert len([url for url, _ in h.calls if url.endswith("/redeem")]) <= 1
        finally:
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["request", "poll", "redeem", "me"])
def test_cancelled_generation_cannot_accept_delayed_results(tmp_path: Path, stage: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        reached, release = asyncio.Event(), asyncio.Event()
        real_post, real_get = h.post, h.get

        async def post(url: str, body: Any) -> Any:
            result = await real_post(url, body)
            suffix = {"request": "/requests", "poll": "/status", "redeem": "/redeem"}
            if stage in suffix and url.endswith(suffix[stage]):
                reached.set()
                await release.wait()
            return result

        async def get(url: str, headers: Any) -> Any:
            result = await real_get(url, headers)
            if stage == "me":
                reached.set()
                await release.wait()
            return result

        h.flow.post, h.flow.get = post, get
        task = asyncio.create_task(h.flow.begin(ORIGIN, "home-mac", "Synthetic"))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            await h.flow.cancel()  # same fence used by endpoint/host edits
            release.set()
            await task
            assert h.flow.snapshot.phase == "setup"
            assert not h.accepted and not h.credentials._data
        finally:
            release.set()
            await task
            await h.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["set", "delete", "accept"])
def test_secure_storage_failure_never_publishes_a_ready_connection(
    tmp_path: Path, operation: str
) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")

        async def fail(*args: Any) -> Any:
            raise RuntimeError("synthetic secure failure")

        if operation == "accept":
            h.flow.accept = fail
        else:
            setattr(h.credentials, operation, fail)
        try:
            await h.flow.begin(ORIGIN, "home-mac", "Synthetic")
            assert h.flow.snapshot.phase == "failed" and not h.accepted
        finally:
            await h.close()

    asyncio.run(scenario())


def test_process_death_discards_secure_pending_material_without_network_replay() -> None:
    async def scenario() -> None:
        credentials = FakeCredentialStore()
        await credentials.set("pairing_verifier_old", "private")
        await credentials.set(PENDING_KEY, "pairing_verifier_old")
        await credentials.set("refresh_token", "existing-account")
        await discard_pending(credentials)
        assert credentials._data == {"refresh_token": "existing-account"}

    asyncio.run(scenario())


def test_old_cancelled_task_does_not_reset_new_completed_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        old_reached = asyncio.Event()
        real_post = h.post
        calls = 0

        async def post(url: str, body: Any) -> Any:
            nonlocal calls
            result = await real_post(url, body)
            if url.endswith("/requests"):
                calls += 1
                if calls == 1:
                    old_reached.set()
                    await asyncio.Event().wait()
            return result

        h.flow.post = post
        old = asyncio.create_task(h.flow.begin(ORIGIN, "home-mac", "Old device"))
        try:
            await asyncio.wait_for(old_reached.wait(), 2)
            await h.flow.begin(ORIGIN, "home-mac", "New device")
            assert h.flow.snapshot.phase == "ready" and len(h.accepted) == 1
            new_generation = h.flow.generation
            old.cancel()
            with pytest.raises(asyncio.CancelledError):
                await old
            assert h.flow.generation == new_generation and h.flow.snapshot.phase == "ready"
            assert h.flow.snapshot.device_label == "New device" and not h.credentials._data
        finally:
            old.cancel()
            await h.close()

    asyncio.run(scenario())


def test_explicit_cancel_clears_material_while_request_is_in_flight(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        reached = asyncio.Event()

        async def post(url: str, body: Any) -> Any:
            reached.set()
            await asyncio.Event().wait()

        h.flow.post = post
        task = asyncio.create_task(h.flow.begin(ORIGIN, "home-mac", "Synthetic"))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            assert await h.credentials.get(PENDING_KEY)
            await h.flow.cancel()
            assert not h.credentials._data and not h.accepted
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await h.close()

    asyncio.run(scenario())


def test_pairing_token_storage_is_fenced_and_partial_failure_clears_account(tmp_path: Path) -> None:
    from relaydeck.client.features.auth.service import (
        REFRESH_KEY,
        AuthService,
        AuthState,
        GatewayConfig,
    )

    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        await h.flow.begin(ORIGIN, "home-mac", "Synthetic")
        tokens, _ = h.accepted[0]

        async def attempt(failure: str) -> None:
            credentials = FakeCredentialStore()
            auth = AuthService(GatewayConfig(ORIGIN), credentials, h.post, lambda url: None)
            current = [True]
            real_set = credentials.set

            async def set_value(key: str, value: str) -> None:
                if key == REFRESH_KEY:
                    if failure == "write":
                        raise RuntimeError("partial secure write failed")
                    current[0] = False
                await real_set(key, value)

            credentials.set = set_value  # type: ignore[method-assign]
            assert not await auth.accept_pairing(tokens, lambda: current[0])
            assert not credentials._data and auth.snapshot.state != AuthState.SIGNED_IN

        await attempt("cancel")
        await attempt("write")
        credentials = FakeCredentialStore()
        auth = AuthService(GatewayConfig(ORIGIN), credentials, h.post, lambda url: None)
        reached = asyncio.Event()
        real_set = credentials.set

        async def suspended_write(key: str, value: str) -> None:
            await real_set(key, value)
            if key == REFRESH_KEY:
                reached.set()
                await asyncio.Event().wait()

        credentials.set = suspended_write  # type: ignore[method-assign]
        task = asyncio.create_task(auth.accept_pairing(tokens, lambda: True))
        await asyncio.wait_for(reached.wait(), 2)
        assert credentials._data
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not credentials._data and auth.snapshot.state == AuthState.SIGNED_OUT
        await h.close()

    asyncio.run(scenario())
