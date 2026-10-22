"""Pairing flow harness shared by pairing and packaging tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from relaydeck.client.features.pairing.service import PairingFlow
from relaydeck.gateway.app import create_app
from tests.support.fakes import FakeCredentialStore
from tests.support.pairing import ORIGIN, foundation


class Harness:
    def __init__(self, path: Path, *, approve: bool = True, opened: bool = True) -> None:
        self.state, self.clock, _ = foundation(path)
        self.record = self.state.hosts.record_for_host("home-mac")
        if opened:
            self.state.pairing.open_window(self.record, ORIGIN, self.clock[0], 300)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.state)))
        self.credentials = FakeCredentialStore()
        self.calls: list[tuple[str, Any]] = []
        self.accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.approve = approve
        self.flow = PairingFlow(
            self.credentials,
            self.post,
            self.get,
            self.accept,
            now_ms=lambda: self.clock[0],
            sleep=self.sleep,
        )

    async def post(self, url: str, body: Any) -> Any:
        self.calls.append((url, body))
        response = await self.http.post(url, json=body)
        return response.status_code, response.json()

    async def get(self, url: str, headers: Any) -> Any:
        response = await self.http.get(url, headers=headers)
        return response.status_code, response.json()

    async def accept(self, tokens: Any, principal: Any, current: Any) -> bool:
        assert current()
        assert not self.credentials._data  # verifier is removed before publishing credentials
        self.accepted.append((tokens, principal))
        return True

    async def sleep(self, interval: float) -> None:
        assert interval == 2.0
        self.clock[0] += 2000
        if self.approve:
            self.decide(True)
            self.approve = False

    def decide(self, approve: bool) -> None:
        row = self.state.pairing.pending(self.record, ORIGIN, self.clock[0])[-1]
        self.state.pairing.decide(
            row["request_id"],
            row["matching_code"],
            self.record,
            ORIGIN,
            self.clock[0],
            approve=approve,
        )

    async def close(self) -> None:
        await self.http.aclose()
        self.state.push.close()
