"""the pairing address error names the real problem, content-free."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from relaydeck.client.components.design_system.connect import connect_hint_example
from relaydeck.client.packaged_runtime import (
    PAIRING_SETUP_FAILED,
    EndpointError,
    PackagedRuntime,
    private_endpoint,
)
from tests.support.fakes import FakeClipboard, FakeCredentialStore
from tests.support.host_harness import run

SECRET = "hunter2-secret-token"
NO_CREDENTIALS = "Remove the username and password from the address."


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "Enter your Mac's private HTTPS address."),
        ("   ", "Enter your Mac's private HTTPS address."),
        ("mac.tailnet.ts.net:8445", "Start the address with https://."),
        ("my-mac", "Start the address with https://."),
        ("http://mac.tailnet.ts.net", "Use https://, not http://."),
        ("ftp://mac.tailnet.ts.net", "The address must start with https://."),
        ("https://", "The address needs a host name."),
        (
            f"https://user:{SECRET}@mac.tailnet.ts.net",
            "Remove the username and password from the address.",
        ),
        (
            f"https://{SECRET}@mac.tailnet.ts.net",
            "Remove the username and password from the address.",
        ),
        (
            f"https://mac.tailnet.ts.net/{SECRET}",
            "Use only the host and port, without a path, query or fragment.",
        ),
        (
            f"https://mac.tailnet.ts.net?token={SECRET}",
            "Use only the host and port, without a path, query or fragment.",
        ),
        (
            f"https://mac.tailnet.ts.net#{SECRET}",
            "Use only the host and port, without a path, query or fragment.",
        ),
        ("https://mac.tailnet.ts.net:70000", "The port must be a number between 1 and 65535."),
        ("https://mac.tailnet.ts.net:abc", "The port must be a number between 1 and 65535."),
        ("https://mac.tailnet.ts.net:0", "The port must be a number between 1 and 65535."),
        ("https://" + "a" * 1100, "The address is too long."),
    ],
)
def test_each_bad_address_gets_its_own_content_free_reason(value: str, reason: str) -> None:
    with pytest.raises(EndpointError) as caught:
        private_endpoint(value)
    assert caught.value.reason == reason
    assert isinstance(caught.value, ValueError)  # existing callers keep working
    assert SECRET not in str(caught.value) and SECRET not in caught.value.reason
    assert value.strip() not in caught.value.reason or not value.strip()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://mac.tailnet.ts.net", "https://mac.tailnet.ts.net"),
        ("  https://mac.tailnet.ts.net:8445/  ", "https://mac.tailnet.ts.net:8445"),
        ("https://100.64.0.2:8443", "https://100.64.0.2:8443"),
    ],
)
def test_good_addresses_are_normalised(value: str, expected: str) -> None:
    assert private_endpoint(value) == expected


def test_the_hint_is_clearly_an_example() -> None:
    hint = connect_hint_example()
    assert hint.startswith("e.g. https://") and hint.endswith(":8445")


class Preferences:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.values[key] = value
        return True


class BrokenStorage(FakeCredentialStore):
    """Secure storage failure whose message carries a secret."""

    async def get(self, key: str) -> str | None:
        raise RuntimeError(f"keychain error: {SECRET}")


async def make_runtime(credentials: Any) -> PackagedRuntime:
    # A stub page: the pairing path never renders, and a real Flet session would
    # leave a pending scheduler task behind for the garbage collector to report.
    page = cast(Any, SimpleNamespace())

    async def browser(url: str) -> None:
        return None

    runtime = PackagedRuntime(
        page,
        credentials,
        Preferences(),
        browser,
        lambda: None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        clipboard=FakeClipboard(),
    )
    runtime.pairing_available = True
    return runtime


@pytest.mark.parametrize(
    ("endpoint", "reason"),
    [
        ("", "Enter your Mac's private HTTPS address."),
        ("http://mac.tailnet.ts.net", "Use https://, not http://."),
        (f"https://mac.tailnet.ts.net/{SECRET}", "Use only the host and port"),
    ],
)
def test_begin_pairing_shows_the_specific_address_reason(endpoint: str, reason: str) -> None:
    async def scenario() -> None:
        runtime = await make_runtime(FakeCredentialStore())
        await runtime.begin_pairing(endpoint, "home-mac", "iPad")
        snapshot = runtime.pairing.snapshot
        assert snapshot.phase == "failed" and not snapshot.busy
        assert snapshot.message.startswith(reason)
        assert SECRET not in snapshot.message
        assert "validated private HTTPS address" not in snapshot.message
        await runtime.close()

    run(scenario())


def test_a_non_address_failure_gets_a_distinct_message_and_never_echoes_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        runtime = await make_runtime(BrokenStorage())
        await runtime.begin_pairing("https://mac.tailnet.ts.net:8445", "home-mac", "iPad")
        snapshot = runtime.pairing.snapshot
        assert snapshot.phase == "failed" and not snapshot.busy
        assert snapshot.message == PAIRING_SETUP_FAILED
        assert "address" not in snapshot.message.lower().replace("secure storage", "")
        assert SECRET not in snapshot.message
        assert SECRET not in caplog.text and "category=RuntimeError" in caplog.text
        await runtime.close()

    caplog.set_level("WARNING")
    run(scenario())
