"""the injected recovery script is one coordinator with fixed strings,
never clears the Flet session id on an ordinary drop, and is served before Flutter's bootstrap.

No JavaScript harness exists in the repo; the script is checked as text and with a
syntax-only check in the gate. Synthetic HTML; no network.
"""

from __future__ import annotations

import asyncio
from typing import Any

from relaydeck.client.features.auth.web_recovery import RECOVERY_SCRIPT, BrowserRecoveryMiddleware


def _function(name: str) -> str:
    start = RECOVERY_SCRIPT.index(f"function {name}(")
    end = RECOVERY_SCRIPT.index("\n  }\n", start)
    return RECOVERY_SCRIPT[start:end]


def test_watchdog_uses_the_fixed_target_and_rate_limit() -> None:
    assert "const WATCHDOG_MS = 15000;" in RECOVERY_SCRIPT
    assert "const RELOAD_GAP_MS = 120000;" in RECOVERY_SCRIPT
    assert "const RELOAD_KEY = '_rd_watchdog_reload';" in RECOVERY_SCRIPT


def test_fixed_user_facing_strings_are_present() -> None:
    assert "Reconnecting is taking longer than expected" in RECOVERY_SCRIPT
    assert "You are offline - waiting for the network" in RECOVERY_SCRIPT
    assert "textContent = 'Reload'" in RECOVERY_SCRIPT
    assert "Connect to your Mac again" in RECOVERY_SCRIPT  # existing dialog kept
    assert "textContent = 'Continue'" in RECOVERY_SCRIPT


def test_reconnect_banner_is_shown_on_close_and_hidden_on_connect() -> None:
    assert "showBanner('Reconnecting…', false);" in _function("reconnecting")
    assert "banner.id = 'relaydeck-reconnect-banner';" in RECOVERY_SCRIPT
    assert "setAttribute('role', 'status')" in RECOVERY_SCRIPT
    assert "setAttribute('aria-live', 'polite')" in RECOVERY_SCRIPT
    assert "banner.remove();" in _function("hideBanner")
    assert "hideBanner();" in _function("clearWatchdog")
    assert "if (!everConnected || connected || visible) return;" in _function("reconnecting")
    close = RECOVERY_SCRIPT[RECOVERY_SCRIPT.index("socket.addEventListener('close'") :]
    assert "reconnecting();" in close[: close.index("});")]


def test_connected_means_a_message_after_open_and_clears_the_watchdog() -> None:
    assert "function markConnected()" in RECOVERY_SCRIPT
    assert "socket.addEventListener('message', markConnected);" in RECOVERY_SCRIPT
    assert "connected = true;" in _function("markConnected")
    assert "clearWatchdog();" in _function("markConnected")


def test_watchdog_triggers_are_visibility_pageshow_and_online() -> None:
    assert "document.visibilityState === 'visible'" in RECOVERY_SCRIPT
    assert "window.addEventListener('pageshow'" in RECOVERY_SCRIPT
    assert "window.addEventListener('online', startWatchdog);" in RECOVERY_SCRIPT


def test_watchdog_never_clears_the_flet_session_id_or_drafts() -> None:
    for name in ("watchdogFired", "startWatchdog", "clearWatchdog", "markConnected", "showBanner"):
        assert "_flet_session_id" not in _function(name), name


def test_auto_reload_is_once_per_gap_and_only_when_visible_and_online() -> None:
    body = _function("watchdogFired")
    assert "navigator.onLine === false" in body
    assert "document.visibilityState === 'visible' && now - last > RELOAD_GAP_MS" in body
    assert "window.location.reload()" in body
    assert "showBanner('Reconnecting is taking longer than expected', true);" in body


def test_dialog_defers_to_body_and_falls_back_when_showmodal_throws() -> None:
    body = _function("showRecovery")
    assert "if (!document.body)" in body and "DOMContentLoaded" in body
    assert "panel.node.showModal();" in body and "catch (_)" in body
    assert "recoveryPanel(false)" in body  # fixed full-screen fallback with the same controls


def test_bfcache_return_starts_the_watchdog_instead_of_reloading() -> None:
    body = RECOVERY_SCRIPT[RECOVERY_SCRIPT.index("window.addEventListener('pageshow'") :]
    body = body[: body.index("window.addEventListener('online'")]
    assert "reconnecting();" in body
    assert "reload" not in body


class _Index:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.paths.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html; charset=utf-8")],
            }
        )
        html = (
            b"<!doctype html><html><head><meta charset='utf-8'>"
            b"<script src='/main.dart.js'></script></head><body></body></html>"
        )
        await send({"type": "http.response.body", "body": html})


def test_served_index_has_the_recovery_script_before_the_flutter_bootstrap() -> None:
    async def scenario() -> bytes:
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        app = BrowserRecoveryMiddleware(_Index())
        await app(
            {"type": "http", "method": "GET", "path": "/agents", "headers": [], "extensions": {}},
            receive,
            send,
        )
        return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")

    html = asyncio.run(scenario()).decode()
    recovery = html.index('<script src="/v1/auth/recovery.js"></script>')
    assert html.index("<head>") < recovery < html.index("/main.dart.js")
