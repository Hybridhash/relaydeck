"""Declarative management controls over observed gateway contracts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO
from relaydeck.client.components.design_system.primitives import text_style
from relaydeck.client.features.management.controller import ManagementController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.client.features.terminal.events import event_text
from relaydeck.client.theme import MIN_TOUCH, palette
from relaydeck.contracts.pubkeys import fingerprint_of_raw, openssh_to_raw


@ft.component
def ManagementPane(
    transport: HttpxGatewayTransport,
    account: str,
    destination: str,
    selected_host: str,
    on_hosts: Callable[[list[dict[str, Any]]], Any],
    on_logout: Callable[[], Any],
    dark: bool = True,
    embedded: bool = False,
    initial_fields: tuple[str, str, str] = ("", "", ""),
    on_fields: Callable[[tuple[str, str, str]], None] | None = None,
    main_screen_revision: int = 0,
) -> Any:
    """`embedded`: content only (no frame, title or own scroll) for a host
    panel that already provides them, such as the Settings page."""
    _, redraw = ft.use_state(0)
    label, set_label = ft.use_state(initial_fields[0])
    public, set_public = ft.use_state(initial_fields[1])
    expected, set_expected = ft.use_state(initial_fields[2])
    confirmed, set_confirmed = ft.use_state(False)
    enrolling, set_enrolling = ft.use_state(False)
    initial_confirmation: tuple[str, str, int] | None = None
    confirmation, set_confirmation = ft.use_state(initial_confirmation)
    current_context: Any = ft.use_ref(None)
    context = (account, destination, selected_host, main_screen_revision)
    current_context.current = context
    owner: Any = ft.use_ref(None)
    tasks: Any = ft.use_ref(set())
    if owner.current is None:
        owner.current = ManagementController(
            transport, account, lambda: redraw(time.monotonic_ns()), on_hosts, on_logout
        )
    manager: ManagementController = owner.current
    tokens = palette(dark)
    available = all(callable(getattr(transport, name, None)) for name in ("devices", "revoke_host"))

    def remember_fields() -> None:
        if on_fields is not None:
            on_fields((label, public, expected))

    ft.use_effect(remember_fields, dependencies=[label, public, expected, on_fields])

    def launch(operation: Coroutine[Any, Any, None]) -> None:
        if not manager.alive or current_context.current != context:
            operation.close()
            return

        async def run() -> None:
            try:
                await operation
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - safe error boundary, no raw request/secret
                if manager.alive:
                    manager.error = "Management action failed. Refresh before retrying."
                    manager.busy = False
                    manager.notify()

        task = asyncio.create_task(run())
        tasks.current.add(task)
        task.add_done_callback(tasks.current.discard)

    def start() -> None:
        if available:
            launch(manager.refresh())

            async def expire() -> None:
                while manager.alive:
                    await asyncio.sleep(1)
                    manager.expire()

            launch(expire())

    def stop() -> None:
        manager.dispose()
        for task in tuple(tasks.current):
            task.cancel()
        tasks.current.clear()

    ft.use_effect(start, dependencies=[transport, account], cleanup=stop)

    def reset_confirmation() -> None:
        set_confirmation(None)
        set_confirmed(False)
        set_enrolling(False)

    ft.use_effect(reset_confirmation, dependencies=[context])

    def button(title: str, key: str, action: Callable[[], Any], disabled: bool = False) -> Any:
        return ft.Button(
            title,
            key=key,
            height=MIN_TOUCH,
            style=chrome_style(dark),
            disabled=manager.busy or disabled,
            on_click=lambda e: action() if current_context.current == context else None,
        )

    _body = {"size": 16, "weight": 400, "height": 1.4}
    _name = {"size": 18, "weight": 700, "height": 22 / 18}
    _mono = {"size": 14, "weight": 400, "height": 1.4}

    def text(
        value: str,
        key: str,
        spec: dict[str, float] | None = None,
        color: str | None = None,
        family: str = FAMILY_INTERFACE,
    ) -> Any:
        return ft.Text(
            value,
            key=key,
            style=text_style(family, spec or _body, color or tokens["text"]),
            selectable=True,
        )

    def field(title: str, key: str, value: str, setter: Callable[[str], Any]) -> Any:
        return ft.TextField(
            label=title,
            key=key,
            value=value,
            color=tokens["text"],
            text_size=16,
            filled=True,
            fill_color=tokens["raised"],
            cursor_color=tokens["accent_text"],
            label_style=ft.TextStyle(color=tokens["muted"]),
            border={
                ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                    border_radius=T.RADIUS_KEY, side=ft.BorderSide(1, tokens["outline"])
                ),
                ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                    border_radius=T.RADIUS_KEY, side=ft.BorderSide(2, tokens["accent_text"])
                ),
            },
            content_padding=12,
            disabled=manager.busy,
            on_change=lambda e: (setter(event_text(e)), set_confirmed(False))
            if current_context.current == context
            else None,
        )

    def ask(kind: str, identity: str) -> None:
        set_confirmation((kind, identity, manager.revision))

    def confirm_revoke() -> None:
        if confirmation is None or confirmation[2] != manager.revision:
            set_confirmation(None)
            return
        kind, identity, _ = confirmation
        set_confirmation(None)
        launch(manager.revoke(kind, identity, True))

    controls: list[Any] = [
        text(
            "Hosts and devices"
            if destination == "settings"
            else "Host management"
            if destination == "hosts"
            else "Devices",
            "management-title",
            T.TYPE_AGENT_TITLE,
        ),
        ft.ProgressBar(key="management-loading", visible=manager.busy),
        text(manager.error, "management-error", color=tokens["danger"]),
        text(manager.notice, "management-notice"),
        button(
            "Refresh management",
            "management-refresh",
            lambda: launch(manager.refresh()),
            not available,
        ),
    ]
    if not available:
        controls.append(text("Management is unavailable.", "management-unavailable"))
    elif destination in ("hosts", "settings"):
        host = next((row for row in manager.hosts if row["host_id"] == selected_host), None)
        if host is not None:
            upstream = host["upstream"]
            controls.extend(
                [
                    text(host.get("label", selected_host), "managed-host-label", _name),
                    text(
                        "Reachable" if host["reachable"] else "Offline",
                        "managed-host-health",
                        color=tokens["success"] if host["reachable"] else tokens["muted"],
                    ),
                    text(
                        host.get("last_seen_at", "No observed heartbeat"),
                        "managed-host-last-seen",
                        color=tokens["muted"],
                    ),
                    text(
                        f"Herdr protocol {upstream['protocol']} — "
                        + (
                            "compatible" if upstream["compatible"] else "unverified or incompatible"
                        ),
                        "managed-host-protocol",
                        color=tokens["muted"],
                    ),
                    text(
                        host.get("fingerprint") or "No bound fingerprint",
                        "managed-host-fingerprint",
                        _mono,
                        family=FAMILY_MONO,
                    ),
                    text(
                        "Account actions: " + ", ".join(sorted(manager.capabilities)),
                        "managed-host-capabilities",
                        color=tokens["muted"],
                    ),
                    button(
                        "Revoke host access",
                        "managed-host-revoke",
                        lambda: ask("host", selected_host),
                        "hosts.enroll" not in manager.capabilities,
                    ),
                ]
            )
            catalog = host.get("agent_catalog")
            controls.append(
                text(
                    "Agent tools: "
                    + ", ".join(
                        entry["kind"] + (" available" if entry["available"] else " unavailable")
                        for entry in catalog["entries"]
                    )
                    if catalog and catalog["status"] == "ready"
                    else "Agent tools unverified",
                    "managed-host-catalog",
                    color=tokens["muted"],
                )
            )
        elif selected_host:
            controls.append(
                text(
                    "Host unavailable or revoked. Refresh to reconcile access.",
                    "managed-host-unavailable",
                )
            )
        elif not manager.hosts:
            controls.append(
                text(
                    "No managed hosts loaded. Refresh to load host detail.",
                    "managed-hosts-empty",
                    color=tokens["muted"],
                )
            )
        else:
            controls.append(
                text(
                    "Select a host to see its detail.",
                    "managed-host-select-hint",
                    color=tokens["muted"],
                )
            )
        controls.append(
            button(
                "Enroll a host",
                "enroll-host-open",
                lambda: set_enrolling(not enrolling),
                "hosts.enroll" not in manager.capabilities,
            )
        )
        if "hosts.enroll" not in manager.capabilities:
            controls.append(
                text(
                    "Enrollment needs the hosts.enroll capability.",
                    "enroll-host-disabled-reason",
                    color=tokens["muted"],
                )
            )
        if enrolling:
            try:
                preview = fingerprint_of_raw(openssh_to_raw(public))
            except ValueError:
                preview = "Paste the Mac connector's Ed25519 public key."
            controls.extend(
                [
                    field("Host label", "enroll-host-label", label, set_label),
                    field("Mac connector public key", "enroll-host-key", public, set_public),
                    text(preview, "enroll-fingerprint-preview", _mono, family=FAMILY_MONO),
                    field(
                        "Full SHA256 fingerprint from Mac",
                        "enroll-fingerprint",
                        expected,
                        set_expected,
                    ),
                    ft.Checkbox(
                        label="I checked this fingerprint on my Mac",
                        key="enroll-fingerprint-confirm",
                        value=confirmed,
                        height=MIN_TOUCH,
                        on_change=lambda e: set_confirmed(bool(e.control.value)),
                    ),
                    button(
                        "Create one-use invitation",
                        "enroll-host-create",
                        lambda: launch(manager.enroll(label, public, expected, confirmed)),
                        not confirmed,
                    ),
                    text(
                        "Share this short-lived code only with your Mac connector. "
                        "Pairing does not establish a private route or prove reachability.",
                        "enroll-host-explanation",
                        color=tokens["muted"],
                    ),
                ]
            )
        if manager.invitation:
            controls.extend(
                [
                    text(
                        manager.invitation["code"],
                        "enroll-one-use-code",
                        _mono,
                        family=FAMILY_MONO,
                    ),
                    text(
                        manager.invitation["fingerprint"],
                        "enroll-bound-fingerprint",
                        _mono,
                        family=FAMILY_MONO,
                    ),
                    text(
                        "Expires " + manager.invitation["expires_at"],
                        "enroll-expires",
                        color=tokens["muted"],
                    ),
                ]
            )
    if available and destination != "hosts":
        controls.extend(
            [
                text(
                    "Login devices and notification installations are listed separately. "
                    "A push registration does not prove connectivity.",
                    "managed-devices-explanation",
                    color=tokens["muted"],
                ),
                *[
                    ft.Container(
                        key=f"managed-device-{row['device_id']}",
                        padding=14,
                        border_radius=ft.BorderRadius.all(16),
                        border=ft.Border.all(1, tokens["line"]),
                        bgcolor=tokens["raised"],
                        content=ft.Column(
                            controls=[
                                text(
                                    row["device_id"] + (" (this login)" if row["current"] else ""),
                                    f"managed-device-name-{row['device_id']}",
                                    _mono,
                                    family=FAMILY_MONO,
                                ),
                                text(
                                    f"{row['platform']} / {row['status']} / "
                                    f"notifications {row['push_status']}",
                                    f"managed-device-state-{row['device_id']}",
                                    color=tokens["muted"],
                                ),
                                button(
                                    "Revoke this login"
                                    if row["current"]
                                    else "Revoke device access",
                                    f"managed-device-revoke-{row['device_id']}",
                                    partial(ask, "device", row["device_id"]),
                                ),
                            ]
                        ),
                    )
                    for row in manager.devices
                ],
            ]
        )
        if manager.truncated:
            controls.append(
                text(
                    "Showing the first 200 installations, including this login.",
                    "managed-devices-truncated",
                    color=tokens["muted"],
                )
            )
    if confirmation is not None:
        kind, identity, _ = confirmation
        controls.extend(
            [
                text(
                    f"Revoke {kind} access for {identity}? Live connections will close. "
                    "The remote process continues."
                    + (
                        " This signs you out."
                        if kind == "device" and identity == manager.current_device_id
                        else ""
                    ),
                    "management-revoke-disclosure",
                ),
                button("Confirm revoke", "management-revoke-confirm", confirm_revoke),
                button("Cancel", "management-revoke-cancel", lambda: set_confirmation(None)),
            ]
        )
    if embedded:
        # No title (the host panel has one) and no empty placeholders: an
        # idle progress bar and blank error/notice lines left a large gap.
        shown = [
            control
            for control in controls[1:]
            if getattr(control, "visible", True) is not False
            and not (isinstance(control, ft.Text) and not control.value)
        ]
        return ft.Column(key="management-bounds", spacing=12, controls=shown)
    return ft.Container(
        key="management-bounds",
        expand=True,
        padding=22,
        border_radius=ft.BorderRadius.all(T.RADIUS_PANE),
        border=ft.Border.all(1, tokens["line"]),
        bgcolor=tokens["surface"],
        content=ft.Column(
            controls=controls,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=12,
        ),
    )
