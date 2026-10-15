"""The shared picture operation: one lifecycle for every source, with the same fences."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from relaydeck.client.features.terminal.picture_operations import (
    Activation,
    RequestClaims,
    start_from_source,
)
from relaydeck.client.features.terminal.pictures import insert_picture
from tests.support.pictures.attachments import IDENTIFIER, PATH, _Api
from tests.support.pictures.paste_source import FakeHost, FakePasteSource


class _HookedApi(_Api):
    """The synthetic gateway with a callback at the grant and the result boundaries."""

    def __init__(self) -> None:
        super().__init__()
        self.begins = 0
        self.at_grant: Callable[[], None] | None = None
        self.at_result: Callable[[], None] | None = None

    async def begin(self, host: str) -> dict[str, Any]:
        self.begins += 1
        doc = await super().begin(host)
        if self.at_grant:
            self.at_grant()
        return doc

    async def result(self, identifier: str) -> dict[str, Any]:
        doc = await super().result(identifier)
        if self.at_result:
            self.at_result()
        return doc


def _activation(host: FakeHost, compose: bool, request_id: str = "request-1") -> Activation:
    return Activation(
        host.session,
        ("home-mac", "agent-1"),
        compose,
        ("token", 1, "controller"),
        draft_store=host.session.store,
        request_id=request_id,
    )


async def _start(
    host: FakeHost,
    source: FakePasteSource,
    *,
    compose: bool = True,
    request_id: str = "request-1",
    claims: RequestClaims | None = None,
) -> str:
    return await start_from_source(
        host,
        claims or RequestClaims(),
        source,
        _activation(host, compose, request_id),
        insert_picture,
    )


def _ready() -> FakePasteSource:
    source = FakePasteSource()
    source.release.set()
    return source


def test_composer_stages_the_picture_and_never_writes_to_the_terminal() -> None:
    async def scenario() -> None:
        host = FakeHost(_HookedApi())
        assert await _start(host, _ready(), compose=True) == "started"
        assert [p.path for p in host.session.store.pictures] == [PATH]
        assert host.session.drafts == [""]
        assert host.sent == [] and host.api.cancelled == []

    asyncio.run(scenario())


def test_direct_inserts_the_validated_path_once_without_enter() -> None:
    async def scenario() -> None:
        host = FakeHost(_HookedApi())
        assert await _start(host, _ready(), compose=False) == "started"
        assert host.sent == [PATH.encode()]
        assert not host.sent[0].endswith((b"\n", b"\r"))
        assert host.session.store.pictures == []
        assert host.api.cancelled == []
        assert host.notices == ["Picture path inserted. Press Enter when ready."]

    asyncio.run(scenario())


def test_direct_insertion_is_bracketed_when_the_terminal_asks_for_it() -> None:
    async def scenario() -> None:
        host = FakeHost(_HookedApi())
        host.session.bracketed_paste = True
        await _start(host, _ready(), compose=False)
        assert host.sent == [b"\x1b[200~" + PATH.encode() + b"\x1b[201~"]

    asyncio.run(scenario())


def test_the_offer_is_refused_when_the_session_changed_before_it_began() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        host.identity_problem = "session_changed"
        source = _ready()
        assert await _start(host, source) == "session_changed"
        assert api.begins == 0 and source.uploads == 0 and source.disposed == 1
        assert host.session.store.pictures == []

    asyncio.run(scenario())


def test_direct_is_refused_at_the_offer_without_control() -> None:
    async def scenario() -> None:
        host = FakeHost(_HookedApi())
        host.input = "no_control"
        assert await _start(host, _ready(), compose=False) == "no_control"
        assert host.api.begins == 0 and host.sent == []

    asyncio.run(scenario())


@pytest.mark.parametrize("barrier", ["grant", "upload", "result"])
@pytest.mark.parametrize("reason", ["session_changed", "disposed", "revoked", "draft_changed"])
def test_identity_changes_stop_every_await_and_delete_the_unaccepted_grant(
    barrier: str, reason: str
) -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        source = FakePasteSource()

        def change() -> None:
            host.identity_problem = reason

        if barrier == "grant":
            api.at_grant = change
            source.release.set()
        elif barrier == "result":
            api.at_result = change
            source.release.set()
        task = asyncio.create_task(_start(host, source, compose=True))
        if barrier == "upload":
            await source.entered.wait()
            change()
            source.release.set()
        assert await task == "started"
        assert host.session.store.pictures == []
        assert host.sent == []
        assert api.cancelled == [IDENTIFIER]
        assert host.stops and host.stops[-1][1] == reason

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["boundary_changed", "not_foreground", "paused", "no_control"])
def test_direct_boundary_change_before_insertion_keeps_the_file_and_never_writes(
    reason: str,
) -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)

        def change() -> None:
            host.input = reason

        api.at_result = change
        assert await _start(host, _ready(), compose=False) == "started"
        assert host.sent == []
        assert api.cancelled == []  # retained under the private retention policy
        assert host.stops[-1] == ("insert", reason)

    asyncio.run(scenario())


def test_composer_staging_survives_a_writer_loss_but_direct_does_not() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        api.at_result = lambda: setattr(host, "input", "no_control")
        await _start(host, _ready(), compose=True, request_id="composer")
        assert len(host.session.store.pictures) == 1
        await _start(host, _ready(), compose=False, request_id="direct")
        assert host.sent == []

    asyncio.run(scenario())


def test_a_cancel_after_insertion_may_have_begun_never_deletes_the_file() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        await _start(host, _ready(), compose=False)
        op_source_cancel = api.cancelled
        assert op_source_cancel == []

    asyncio.run(scenario())


def test_cancelling_during_upload_deletes_the_grant_and_stages_nothing() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        source = FakePasteSource()
        task = asyncio.create_task(_start(host, source, compose=True))
        await source.entered.wait()
        assert host.current is not None
        host.current.cancelled = True
        host.current.source.abort()  # type: ignore[union-attr]
        await task
        assert source.aborted and host.session.store.pictures == []
        assert api.cancelled == [IDENTIFIER]
        assert host.stops[-1] == ("upload", "cancelled")

    asyncio.run(scenario())


def test_a_repeated_request_id_never_makes_a_second_grant_chip_or_write() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        claims = RequestClaims()
        assert await _start(host, _ready(), request_id="same", claims=claims) == "started"
        again = _ready()
        assert await _start(host, again, request_id="same", claims=claims) == "already_started"
        assert api.begins == 1 and len(host.session.store.pictures) == 1
        assert again.uploads == 0 and again.disposed == 1 and host.sent == []

    asyncio.run(scenario())


def test_a_new_paste_after_completion_is_distinct_even_with_identical_pixels() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        claims = RequestClaims()
        await _start(host, _ready(), request_id="first", claims=claims)
        await _start(host, _ready(), request_id="second", claims=claims)
        assert api.begins == 2 and len(host.session.store.pictures) == 2

    asyncio.run(scenario())


def test_a_busy_operation_rejects_explicitly_and_queues_nothing() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        claims = RequestClaims()
        first = FakePasteSource()
        running = asyncio.create_task(_start(host, first, request_id="a", claims=claims))
        await first.entered.wait()
        second = _ready()
        assert await _start(host, second, request_id="b", claims=claims) == "busy"
        assert second.uploads == 0 and second.disposed == 1
        first.release.set()
        await running
        assert api.begins == 1 and len(host.session.store.pictures) == 1
        # The rejected request id was not consumed by the rejection.
        assert await _start(host, _ready(), request_id="b", claims=claims) == "started"

    asyncio.run(scenario())


def test_the_eight_picture_cap_is_checked_before_a_grant() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        host.session.store.pictures.extend([object()] * 8)
        assert await _start(host, _ready(), compose=True) == "picture_limit"
        assert api.begins == 0

    asyncio.run(scenario())


def test_a_failed_transfer_deletes_the_grant_and_reports_once() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        source = FakePasteSource(fail=True)
        source.release.set()
        await _start(host, source)
        assert api.cancelled == [IDENTIFIER]
        assert host.stops[-1] == ("upload", "upload_error")
        assert host.session.store.pictures == []

    asyncio.run(scenario())


def test_an_oversized_source_is_refused_before_a_grant() -> None:
    async def scenario() -> None:
        api = _HookedApi()
        host = FakeHost(api)
        source = FakePasteSource(size=10 * 1024 * 1024 + 1)
        source.release.set()
        await _start(host, source)
        assert api.begins == 0 and host.stops[-1] == ("start", "too_large")

    asyncio.run(scenario())
