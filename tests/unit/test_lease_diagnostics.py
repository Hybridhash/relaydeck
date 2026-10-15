"""Fixed diagnostic schema, failure attribution and privacy."""

from __future__ import annotations

import asyncio
import time

import pytest

from relaydeck.client.features.sync.transport import TransportError
from relaydeck.client.features.terminal.renewal import LeaseRenewalDriver
from relaydeck.companion.runtime import _uvicorn_logging_config
from relaydeck.lease_diagnostics import LeaseTrace

MARKER = "Bearer secret; ticket=grant; Cookie=secret; /private/source.py; terminal contents"


def test_trace_allowlist_keeps_only_opaque_correlations(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="relaydeck.lease_lifecycle")
    owner = object()
    first = LeaseTrace.for_session(owner, MARKER, MARKER, MARKER, MARKER, -1)
    second = LeaseTrace.for_session(owner, MARKER, MARKER, "lease-2", "view", 4)
    first.emit(
        MARKER, reason=MARKER, source=MARKER, current_lease_id=MARKER, ui=object(), socket=object()
    )
    second.emit("socket_close", reason="overflow", source="server", attempt=3, viewer=2)
    messages = [record.getMessage() for record in caplog.records]
    assert all(
        part not in caplog.text
        for part in ("Bearer", "ticket=", "Cookie=", "/private/", "contents")
    )
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    assert "event=unknown" in messages[0] and "reason=other source=other" in messages[0]
    assert "lease=lease-2" in messages[1] and "mode=view generation=4" in messages[1]
    assert "reason=overflow source=server attempt=3 viewer=2" in messages[1]
    for key in ("session", "terminal"):
        tokens = [
            next(item for item in message.split() if item.startswith(key + "="))
            for message in messages
        ]
        assert tokens[0] == tokens[1] and len(tokens[0].split("=")[1]) == 16
    assert all(
        float(next(item[2:] for item in m.split() if item.startswith("t="))) > 0 for m in messages
    )


@pytest.mark.parametrize(
    "failure", ["local_deadline", "server_refusal", "renew_timeout", "invalid_expiry", "unexpected"]
)
def test_renewal_failure_source_and_no_untrusted_exception_text(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="relaydeck.lease_lifecycle")

    async def scenario() -> None:
        async def renew(_: str) -> dict:
            if failure == "server_refusal":
                raise TransportError("lease_expired", MARKER, False) from OSError(MARKER)
            if failure == "renew_timeout":
                await asyncio.Event().wait()
            if failure == "invalid_expiry":
                return {"expires_at": MARKER}
            raise RuntimeError(MARKER) from OSError(MARKER)

        deadline = time.monotonic() + (0 if failure == "local_deadline" else 0.03)
        done = asyncio.Event()
        driver = LeaseRenewalDriver(
            renew,
            "lease-1",
            interval_s=0.001,
            expires_at=deadline,
            on_terminal_failure=lambda _: done.set(),
        )
        try:
            async with asyncio.timeout(2):
                driver.start()
                await done.wait()
            messages = [r.getMessage() for r in caplog.records]
            assert any("event=renew_failure" in m and f"source={failure}" in m for m in messages)
            assert MARKER not in caplog.text and "ticket=" not in caplog.text
            assert all(r.exc_info is None for r in caplog.records)
        finally:
            await driver.stop()

    asyncio.run(scenario())


def test_default_companion_routes_content_free_diagnostics() -> None:
    assert _uvicorn_logging_config()["loggers"]["relaydeck.lease_lifecycle"] == {
        "level": "INFO",
        "handlers": ["default"],
        "propagate": False,
    }
