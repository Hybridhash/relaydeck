"""The companion compresses the web client's static files only."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from relaydeck.companion.runtime import static_gzip

BODY = "x" * 4096


def _app() -> Starlette:
    async def text(_request: object) -> PlainTextResponse:
        return PlainTextResponse(BODY)

    return Starlette(
        routes=[
            Route("/", text),
            Route("/main.dart.js", text),
            Route("/v1/snapshot.json", text),
            Route("/v1/events", text),
        ]
    )


@pytest.mark.parametrize(
    ("path", "zipped"),
    [
        ("/", True),
        ("/main.dart.js", True),
        ("/v1/snapshot.json", False),
        ("/v1/events", False),
    ],
)
def test_only_static_paths_are_gzipped(path: str, zipped: bool) -> None:
    async def fetch() -> httpx.Response:
        transport = httpx.ASGITransport(app=static_gzip(_app()))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get(path, headers={"Accept-Encoding": "gzip"})

    response = asyncio.run(fetch())
    assert response.status_code == 200
    assert (response.headers.get("content-encoding") == "gzip") is zipped
    assert response.text == BODY
