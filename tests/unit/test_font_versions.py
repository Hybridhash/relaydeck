"""Bundled font URLs carry a content fingerprint, and only a matching one is cached for a year."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from relaydeck.client.components.design_system import font_versions as fv
from relaydeck.client.components.design_system.fonts import WEIGHT_FILES, page_fonts
from relaydeck.companion.runtime import IMMUTABLE, NO_CACHE, cache_policy, static_cache

NAME = WEIGHT_FILES[0][1]  # Bricolage Regular: always bundled


async def _app(scope: Any, receive: Any, send: Any) -> None:
    await PlainTextResponse("font")(scope, receive, send)


def test_page_fonts_urls_carry_the_fingerprint() -> None:
    fonts = page_fonts()
    assert fonts
    for alias, name, _ in WEIGHT_FILES:
        version = fv.fingerprint(name)
        if not (fv.FONTS_DIR / name).exists():  # a listed file not yet added stays unversioned
            assert fonts[alias] == f"design_system/fonts/{name}"
            continue
        assert version is not None and len(version) == 12
        assert fonts[alias] == f"design_system/fonts/{name}?v={version}"


def test_a_matching_version_is_immutable() -> None:
    path = f"/design_system/fonts/{NAME}"
    assert cache_policy(path, f"v={fv.fingerprint(NAME)}") == IMMUTABLE
    assert cache_policy(path, f"v={fv.fingerprint(NAME)}".encode()) == IMMUTABLE


@pytest.mark.parametrize("query", ["", "v=", "v=000000000000", "x=1", "v={ok}&v=000000000000"])
def test_unversioned_or_mismatched_requests_keep_no_cache(query: str) -> None:
    path = f"/design_system/fonts/{NAME}"
    assert cache_policy(path, query.replace("{ok}", fv.fingerprint(NAME) or "")) == NO_CACHE


@pytest.mark.parametrize(
    "path",
    [
        "/design_system/fonts/missing.ttf",
        "/design_system/fonts/../fonts/" + NAME,
        "/design_system/dots/dot_tile.png",
        "/assets/fonts/MaterialIcons-Regular.otf",
    ],
)
def test_only_known_bundled_fonts_qualify(path: str) -> None:
    assert cache_policy(path, f"v={fv.fingerprint(NAME)}") == NO_CACHE


def test_the_middleware_reads_the_query_string() -> None:
    client = TestClient(static_cache(_app))
    path = f"/design_system/fonts/{NAME}"
    assert (
        client.get(path, params={"v": fv.fingerprint(NAME)}).headers["cache-control"] == IMMUTABLE
    )
    assert client.get(path).headers["cache-control"] == NO_CACHE
    assert client.get(path, params={"v": "nope"}).headers["cache-control"] == NO_CACHE
