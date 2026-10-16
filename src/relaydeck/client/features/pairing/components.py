"""Controlled integration of the design-system ConnectPanel; no duplicated presentation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import copy_context
from dataclasses import asdict
from typing import Any

import flet as ft

from relaydeck.client.components.design_system.connect import ConnectPanel, ConnectState
from relaydeck.client.features.pairing.service import PairingFlow


@ft.component
def PairingPanel(
    flow: PairingFlow,
    *,
    endpoint: str = "",
    host_id: str = "home-mac",
    label: str = "My device",
    begin_callback: Callable[[str, str, str], Awaitable[Any]] | None = None,
) -> Any:
    snapshot, set_snapshot = ft.use_state(flow.snapshot)
    address, set_address = ft.use_state(endpoint)
    host, set_host = ft.use_state(host_id)
    name, set_name = ft.use_state(label)
    task: Any = ft.use_ref(None)

    def saved_endpoint() -> None:
        if endpoint and not address:
            set_address(endpoint)

    ft.use_effect(saved_endpoint, dependencies=[endpoint])

    def change(setter: Any, value: str) -> None:
        flow.invalidate()
        if task.current is not None:
            task.current.cancel()
        setter(value)

    def begin() -> None:
        if task.current is not None:
            task.current.cancel()

        async def connect() -> None:
            await (begin_callback or flow.begin)(address, host, name)

        task.current = asyncio.create_task(connect())

    def start() -> Any:
        context = copy_context()

        def deliver(snapshot: Any) -> None:
            try:
                context.run(set_snapshot, snapshot)
            except RuntimeError:
                # This page's Flet session is already destroyed (reload or
                # reconnect): nothing to update, and nothing to raise.
                pass

        return flow.subscribe(deliver)

    unsubscribe: Any = ft.use_ref(None)

    def effect() -> None:
        unsubscribe.current = start()

    def cleanup() -> None:
        unsubscribe.current()
        # Leaving the page must not void an attempt the Mac may still approve; only an
        # explicit cancel, a field edit or expiry does; the approval wait keeps running.

    ft.use_effect(effect, dependencies=[flow], cleanup=cleanup)
    state = ConnectState(
        **{**asdict(snapshot), "endpoint": address, "host_id": host, "device_label": name}
    )

    def cancel() -> None:
        flow.invalidate()
        if task.current is not None:
            task.current.cancel()

    return ConnectPanel(
        state,
        on_endpoint=lambda value: change(set_address, value),
        on_host_id=lambda value: change(set_host, value),
        on_label=lambda value: change(set_name, value),
        on_begin=begin,
        on_cancel=cancel,
        on_retry=begin,
    )
