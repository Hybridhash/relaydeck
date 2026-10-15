"""The paste bridge: grant URL and id validation, and the upload source over a fake region."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from relaydeck_terminal.picture_paste import (
    RelayDeckPicturePaste,
    bounded_upload_ms,
    valid_request_id,
    valid_upload_url,
)

from relaydeck.client.features.terminal.picture_operations import UploadFailed
from relaydeck.client.features.terminal.picture_paste_region import (
    BrowserPictureSource,
    picture_paste_region,
    take_paste_props,
    wrap_with_paste_region,
)

GRANT_URL = "/v1/pictures/abc_123-x/content?grant=Zz09_-secret"


class _Region:
    def __init__(self, status: str = "ok") -> None:
        self.status = status
        self.uploads: list[tuple[str, str, int]] = []
        self.aborts: list[str] = []

    async def upload(self, request_id: str, url: str, wait_ms: int) -> str:
        self.uploads.append((request_id, url, wait_ms))
        return self.status

    async def abort(self, request_id: str) -> None:
        self.aborts.append(request_id)


def _grant(url: str = GRANT_URL, expires_ms: int = 1_060_000) -> dict[str, Any]:
    return {"picture_id": "abc_123-x", "upload_url": url, "expires_ms": expires_ms}


def _source(region: _Region, request_id: str = "p1-0") -> BrowserPictureSource:
    return BrowserPictureSource(region, request_id, 1234, now_ms=lambda: 1_000_000)


def test_only_the_companion_picture_content_path_is_accepted() -> None:
    assert valid_upload_url(GRANT_URL)
    for bad in (
        "https://evil.example.org/v1/pictures/abc/content?grant=x",
        "//evil.example.org/v1/pictures/abc/content?grant=x",
        "/v1/pictures/abc/other?grant=x",
        "/v1/pictures/abc/content",
        "/v1/pictures/abc/content?grant=x&extra=1",
        "/v1/pictures/../secret/content?grant=x",
        "/v1/pictures/abc/content?grant=x#frag",
        "javascript:alert(1)",
        "",
        None,
        7,
    ):
        assert not valid_upload_url(bad), bad


def test_request_ids_are_opaque_tokens() -> None:
    assert valid_request_id("p1700000000-3")
    for bad in ("", "a b", "../x", "x" * 65, None, 5):
        assert not valid_request_id(bad), bad


def test_the_upload_wait_is_bounded_by_the_grant_life() -> None:
    assert bounded_upload_ms(1_060_000, 1_000_000) == 60_000
    assert bounded_upload_ms(1_000_100, 1_000_000) == 1_000
    assert bounded_upload_ms(9_000_000, 1_000_000) == 120_000
    assert bounded_upload_ms(500, 1_000_000) == 1_000


def test_an_uploaded_picture_releases_without_a_second_abort() -> None:
    async def scenario() -> None:
        region = _Region("ok")
        source = _source(region)
        assert source.metadata.size == 1234
        await source.upload(_grant(), lambda: True, lambda _v: None)
        assert region.uploads == [("p1-0", GRANT_URL, 60_000)]
        source.dispose()
        source.abort()
        await asyncio.sleep(0)
        assert region.aborts == []

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["rejected", "network", "aborted", "gone", "bad_url"])
def test_a_failed_browser_upload_is_an_upload_failure(status: str) -> None:
    async def scenario() -> None:
        region = _Region(status)
        with pytest.raises(UploadFailed):
            await _source(region).upload(_grant(), lambda: True, lambda _v: None)

    asyncio.run(scenario())


def test_a_grant_pointing_elsewhere_never_reaches_the_browser() -> None:
    async def scenario() -> None:
        region = _Region("ok")
        source = _source(region)
        with pytest.raises(UploadFailed):
            await source.upload(
                _grant("https://evil.example.org/v1/pictures/a/content?grant=x"),
                lambda: True,
                lambda _v: None,
            )
        assert region.uploads == []
        source.dispose()  # the operation always disposes its source
        await asyncio.sleep(0)
        assert region.aborts == ["p1-0"]  # the retained Blob is released

    asyncio.run(scenario())


def test_an_operation_that_ended_during_upload_fails_after_the_transfer() -> None:
    async def scenario() -> None:
        region = _Region("ok")
        with pytest.raises(UploadFailed):
            await _source(region).upload(_grant(), lambda: False, lambda _v: None)

    asyncio.run(scenario())


def test_disposing_an_unused_source_aborts_the_blob_once() -> None:
    async def scenario() -> None:
        region = _Region()
        source = _source(region, "p9-9")
        source.dispose()
        source.abort()
        await asyncio.sleep(0)
        assert region.aborts == ["p9-9"]

    asyncio.run(scenario())


def test_the_upload_diagnostics_never_name_the_grant(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        region = _Region("rejected")
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(UploadFailed):
                await _source(region).upload(_grant(), lambda: True, lambda _v: None)

    asyncio.run(scenario())
    assert "Zz09_-secret" not in caplog.text and "abc_123-x" not in caplog.text


def test_the_region_adds_no_layout_of_its_own() -> None:
    inner = object()
    region = picture_paste_region(
        inner,
        capture=lambda *_a: None,
        attach=lambda *_a: None,
        reject=lambda _r: None,
        binding="rev-1",
        current_binding=lambda: "rev-1",
        key="r",
    )
    assert isinstance(region, RelayDeckPicturePaste)
    assert region.content is inner and region.expand is True and region.key == "r"
    assert region.binding == "rev-1"


def test_pictures_unavailable_leaves_the_screen_untouched() -> None:
    props: dict[str, Any] = {"picture_paste_available": False, "on_attach_picture": 1}
    paste = take_paste_props(props)
    assert props == {"on_attach_picture": 1}
    screen = object()
    assert wrap_with_paste_region(screen, paste, key="k") is screen


class _Control:
    def __init__(self) -> None:
        self.aborts: list[str] = []

    async def abort(self, request_id: str) -> None:
        self.aborts.append(request_id)

    async def upload(self, request_id: str, url: str, wait_ms: int) -> str:
        return "ok"


def _offer(binding: str, control: _Control, request_id: str = "p5-1") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        request_id=request_id, size=100, mime="image/png", binding=binding, control=control
    )


def test_an_offer_with_a_stale_binding_is_refused_and_its_blob_released() -> None:
    async def scenario() -> None:
        control, seen, refused = _Control(), [], []

        async def attach(source: Any, activation: Any) -> str:
            seen.append(activation)
            return "started"

        region = picture_paste_region(
            object(),
            capture=lambda request_id=None: request_id,
            attach=attach,
            reject=refused.append,
            binding="rev-1",
            current_binding=lambda: "rev-2",
            key="r",
        )
        await region.on_offer(_offer("rev-1", control))
        await asyncio.sleep(0)
        assert seen == [] and refused == ["stale"] and control.aborts == ["p5-1"]

    asyncio.run(scenario())


def test_an_offer_with_the_current_binding_attaches_with_the_browser_request_id() -> None:
    async def scenario() -> None:
        control, seen = _Control(), []

        async def attach(source: Any, activation: Any) -> str:
            seen.append((source.metadata.size, activation))
            return "started"

        region = picture_paste_region(
            object(),
            capture=lambda request_id=None: request_id,
            attach=attach,
            reject=lambda _r: None,
            binding="rev-1",
            current_binding=lambda: "rev-1",
            key="r",
        )
        await region.on_offer(_offer("rev-1", control))
        assert seen == [(100, "p5-1")] and control.aborts == []

    asyncio.run(scenario())
