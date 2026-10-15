"""Login boundary components.

Presentational: they render an AuthSnapshot and report user intent through
callbacks. The AuthService owns the flow; AppRoot wires the two.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.features.auth.service import AuthSnapshot, AuthState
from relaydeck.client.theme import MIN_TOUCH


@ft.component
def LoginScreen(
    snapshot: AuthSnapshot,
    on_sign_in: Callable[[], Any],
    on_cancel: Callable[[], Any],
    on_retry: Callable[[], Any],
    on_open_browser: Callable[[], Any] | None = None,
) -> Any:
    state = snapshot.state
    if state == AuthState.SIGNED_IN:
        return ft.Text("Signed in", key="auth-signed-in")
    if state in (AuthState.WAITING_CALLBACK, AuthState.EXCHANGING):
        return ft.Column(
            key="auth-waiting",
            controls=[
                ft.ProgressRing(key="auth-spinner"),
                ft.Text(
                    "Complete sign-in in the browser, then return here.",
                    key="auth-waiting-text",
                ),
                ft.Button(
                    "Cancel",
                    key="auth-cancel",
                    height=MIN_TOUCH,
                    on_click=lambda e: on_cancel(),
                ),
            ],
        )
    if state == AuthState.REFRESHING:
        return ft.Column(
            key="auth-refreshing",
            controls=[
                ft.ProgressRing(key="auth-spinner"),
                ft.Text("Refreshing session…", key="auth-refreshing-text"),
            ],
        )
    if state == AuthState.ERROR:
        return ft.Column(
            key="auth-error",
            controls=[
                ft.Text("Sign-in failed", key="auth-error-title"),
                ft.Text(snapshot.error or "Unknown error", key="auth-error-text"),
                ft.Row(
                    wrap=True,
                    controls=[
                        ft.Button(
                            "Try again",
                            key="auth-retry",
                            height=MIN_TOUCH,
                            on_click=lambda e: on_retry(),
                            visible=snapshot.can_retry,
                        ),
                        ft.Button(
                            "Cancel",
                            key="auth-cancel",
                            height=MIN_TOUCH,
                            on_click=lambda e: on_cancel(),
                        ),
                    ],
                ),
            ],
        )
    controls: list[Any] = [
        ft.Text("RelayDeck", key="auth-title"),
        ft.Text(
            "Sign in through your gateway to reach your home host.",
            key="auth-subtitle",
        ),
        ft.Button("Sign in", key="auth-sign-in", height=MIN_TOUCH, on_click=lambda e: on_sign_in()),
    ]
    if on_open_browser is not None:
        controls.append(
            ft.TextButton(
                "Open browser again",
                key="auth-reopen",
                height=MIN_TOUCH,
                on_click=lambda e: on_open_browser(),
            )
        )
    return ft.Column(key="auth-signed-out", controls=controls)


@ft.component
def AccountRow(account: str, on_logout: Callable[[], Any], enabled: bool = True) -> Any:
    return ft.Row(
        key="account-row",
        controls=[
            ft.Text(account or "Account", key="account-label"),
            ft.Button(
                "Log out",
                key="account-logout",
                height=MIN_TOUCH,
                on_click=lambda e: on_logout(),
                disabled=not enabled,
            ),
        ],
    )
