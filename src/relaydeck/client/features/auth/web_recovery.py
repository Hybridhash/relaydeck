"""Browser recovery after a rejected Flet reconnect.

The pinned client retries policy closures indefinitely, hiding its startup
error while reconnecting. An independent browser prompt remains usable even
when Flutter never receives a page. Only a deliberate click removes this
tab's Flet session ID; cookies, credentials and other tabs remain untouched.
"""

from __future__ import annotations

from typing import Any

RECOVERY_PATH = "/v1/auth/recovery.js"
RECOVERY_REASON = "session_recovery_required"
MAX_INDEX_BYTES = 256 * 1024

RECOVERY_SCRIPT = """(() => {
  'use strict';
  const NativeWebSocket = window.WebSocket;
  // One coordinator for the tab. Session id, drafts and cookies are
  // never cleared here; reloads are rate limited so a bad network cannot loop the tab.
  const WATCHDOG_MS = 15000;
  const RELOAD_GAP_MS = 120000;
  const RELOAD_KEY = '_rd_watchdog_reload';
  let visible = false;
  let connected = false;
  let watchdogTimer = 0;
  let banner = null;
  let everConnected = false;
  function recoveryPanel(canModal) {
    const node = document.createElement(canModal ? 'dialog' : 'div');
    node.id = 'relaydeck-session-recovery';
    node.setAttribute('aria-labelledby', 'relaydeck-recovery-title');
    if (!canModal) {
      node.style.cssText = 'position:fixed;inset:0;display:flex;flex-direction:column;'
        + 'justify-content:center;align-items:center;gap:16px;padding:24px;'
        + 'background:#fff;color:#111;z-index:2147483647';
    }
    const title = document.createElement('h1');
    title.id = 'relaydeck-recovery-title';
    title.textContent = 'Connect to your Mac again';
    const explanation = document.createElement('p');
    explanation.textContent = 'Your browser session expired or changed. '
      + 'Continue with a new session. Unsent drafts from the previous session '
      + 'cannot be recovered here.';
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Continue';
    button.style.minHeight = '48px';
    button.style.minWidth = '96px';
    const error = document.createElement('p');
    error.setAttribute('role', 'alert');
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const response = await fetch('/v1/auth/session/recovery', {
          method:'POST', credentials:'same-origin', cache:'no-store'
        });
        if (response.status !== 204 && response.status !== 401) {
          error.textContent = 'Connection check unavailable. Try again.';
          button.disabled = false;
          return;
        }
        window.sessionStorage.removeItem('_flet_session_id');
        window.location.reload();
      } catch (_) {
        error.textContent = 'Browser storage is unavailable. '
          + 'Open this site in a new tab to connect again.';
        button.disabled = false;
      }
    });
    node.append(title, explanation, button, error);
    return {node, button};
  }
  function showRecovery() {
    if (visible) return;
    // Deferred until the page has a body (the script runs in <head>).
    if (!document.body) {
      document.addEventListener('DOMContentLoaded', showRecovery, {once:true});
      return;
    }
    visible = true;
    let panel = recoveryPanel(true);
    const canModal = typeof panel.node.showModal === 'function';
    if (!canModal) panel = recoveryPanel(false);
    document.body.appendChild(panel.node);
    if (canModal) {
      panel.node.addEventListener('cancel', (event) => event.preventDefault());
      try {
        panel.node.showModal();
      } catch (_) {
        // showModal threw: same text and button in a fixed full-screen block.
        panel.node.remove();
        panel = recoveryPanel(false);
        document.body.appendChild(panel.node);
      }
    }
    panel.button.focus();
  }
  function finishPairing() {
    const tab = window.sessionStorage.getItem('_flet_session_id');
    if (!tab || typeof fetch !== 'function') return Promise.resolve();
    return fetch('/v1/auth/pairing/complete', {
      method:'POST',credentials:'same-origin',cache:'no-store',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({flet_session_id:tab})
    }).then((done) => { if (done.ok) {
      window.sessionStorage.removeItem('_flet_session_id'); window.location.replace('/');
    }});
  }
  // One top banner for "Reconnecting", upgraded in place at the 15 s
  // watchdog. Shown only after a socket has connected once: the first load is the splash.
  function showBanner(text, withReload) {
    if (!document.body) return;
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'relaydeck-reconnect-banner';
      banner.setAttribute('role', 'status');
      banner.setAttribute('aria-live', 'polite');
      banner.style.cssText = 'position:fixed;top:0;left:50%;transform:translateX(-50%);'
        + 'min-height:44px;box-sizing:border-box;padding:10px 16px;background:#130F26;'
        + 'color:#fff;font:14px system-ui,sans-serif;z-index:2147483645;display:flex;'
        + 'gap:12px;align-items:center;justify-content:center';
      document.body.appendChild(banner);
    }
    banner.textContent = text;
    if (withReload) {
      const reload = document.createElement('button');
      reload.type = 'button';
      reload.textContent = 'Reload';
      reload.style.minHeight = '44px';
      reload.style.minWidth = '88px';
      reload.addEventListener('click', () => window.location.reload());
      banner.appendChild(reload);
    }
  }
  function hideBanner() {
    if (banner) { banner.remove(); banner = null; }
  }
  function clearWatchdog() {
    if (watchdogTimer) { window.clearTimeout(watchdogTimer); watchdogTimer = 0; }
    hideBanner();
  }
  function markConnected() {
    everConnected = true;
    connected = true;
    clearWatchdog();
  }
  function startWatchdog() {
    if (!everConnected || connected || watchdogTimer) return;
    watchdogTimer = window.setTimeout(watchdogFired, WATCHDOG_MS);
  }
  function reconnecting() {
    if (!everConnected || connected || visible) return;
    showBanner('Reconnecting\u2026', false);
    startWatchdog();
  }
  function watchdogFired() {
    watchdogTimer = 0;
    if (connected || visible) return;
    if (navigator.onLine === false) {
      showBanner('You are offline - waiting for the network', false);
      return;
    }
    const last = Number(window.sessionStorage.getItem(RELOAD_KEY)) || 0;
    const now = Date.now();
    if (document.visibilityState === 'visible' && now - last > RELOAD_GAP_MS) {
      try { window.sessionStorage.setItem(RELOAD_KEY, String(now)); } catch (_) {}
      window.location.reload();
      return;
    }
    showBanner('Reconnecting is taking longer than expected', true);
  }
  // HttpOnly browser binding is minted by HTTP before anonymous Flet pairing.
  // Existing authenticated sessions return204 and retain their page/drafts.
  if (typeof fetch === 'function') fetch('/v1/auth/pairing/browser', {
    method:'POST',credentials:'same-origin',cache:'no-store'
  }).then((r) => { if (r.status === 201) {
    window.sessionStorage.removeItem('_flet_session_id'); window.location.reload();
    return;
  }
  // A pairing approved while this tab was suspended (phone switched apps,
  // live connection dropped) finishes on the next load: same checks as the
  // in-page handoff (HttpOnly browser cookie + this tab's Flet ID + the
  // server-held, unexpired result). Nothing pending answers 404.
  return finishPairing();
  }).catch(() => {});
  // Returning to a suspended tab (phone app switch) also finishes it, so
  // an approval made meanwhile needs no manual reload. A return without a
  // received register message shows the banner and starts the watchdog.
  if (typeof document.addEventListener === 'function') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        finishPairing().catch(() => {});
        reconnecting();
      }
    });
  }
  // BFCache return: the page script resumes but the socket does not. The banner
  // shows and the watchdog starts instead of a blind reload.
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) connected = false;
    reconnecting();
  });
  window.addEventListener('online', startWatchdog);
  window.WebSocket = new Proxy(NativeWebSocket, {
    construct(target, args) {
      const socket = Reflect.construct(target, args);
      const url = new URL(socket.url, window.location.href);
      const sameOrigin = url.host === window.location.host
        && url.protocol === (window.location.protocol === 'https:' ? 'wss:' : 'ws:');
      if (sameOrigin && url.pathname === '/ws') {
        connected = false;
        // Connected means a message arrived after open (the register response).
        socket.addEventListener('message', markConnected);
        socket.addEventListener('close', (event) => {
          connected = false;
          if (event.code === 1008 && event.reason === 'session_recovery_required') {
            showRecovery();
          } else {
            reconnecting();
          }
        });
      }
      return socket;
    }
  });
  if (window.location.pathname === '/v1/auth/session/recovery') showRecovery();
})();
"""


class BrowserRecoveryMiddleware:
    """Add a same-origin recovery script before the pinned Flutter bootstrap.

    Transform only the application index, including client routes. Asset and
    API responses pass through. Disable conditional index responses so an old
    cached document cannot miss the recovery hook after an upgrade.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        path = scope.get("path", "")
        index = path in ("/", "/index.html") or any(
            path == f"/{destination}" or path.startswith(f"/{destination}/")
            for destination in ("agents", "terminals", "usage", "settings", "hosts")
        )
        if scope["type"] != "http" or scope.get("method") != "GET" or not index:
            return await self._app(scope, receive, send)

        scope = dict(scope)
        scope["headers"] = [
            (key, value)
            for key, value in scope.get("headers", [])
            if key.lower() not in (b"if-none-match", b"if-modified-since", b"range", b"if-range")
        ]
        scope["extensions"] = {
            key: value
            for key, value in scope.get("extensions", {}).items()
            if key != "http.response.pathsend"
        }
        start: dict[str, Any] | None = None
        parts: list[bytes] = []
        total = 0
        rejected = False

        async def inject(message: dict[str, Any]) -> None:
            nonlocal start, total, rejected
            if rejected:
                return
            if message["type"] == "http.response.start":
                headers = {key.lower(): value for key, value in message.get("headers", [])}
                if (
                    message["status"] == 200
                    and headers.get(b"content-type", b"").startswith(b"text/html")
                    and headers.get(b"content-encoding", b"identity") == b"identity"
                ):
                    start = message
                    return
            elif message["type"] == "http.response.body" and start is not None:
                part = message.get("body", b"")
                total += len(part)
                if total > MAX_INDEX_BYTES:
                    rejected = True
                    parts.clear()
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 503,
                            "headers": [
                                (b"content-type", b"text/plain; charset=utf-8"),
                                (b"cache-control", b"no-store"),
                            ],
                        }
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"The connection page is unavailable. Please try again later.",
                        }
                    )
                    return
                parts.append(part)
                if not message.get("more_body", False):
                    body = b"".join(parts)
                    marker = b"<head>"
                    script = f'<script src="{RECOVERY_PATH}"></script>'.encode()
                    body = body.replace(marker, marker + script, 1)
                    response_headers = [
                        (key, value)
                        for key, value in start.get("headers", [])
                        if key.lower()
                        not in (b"content-length", b"etag", b"last-modified", b"cache-control")
                    ]
                    response_headers.extend(
                        [
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store"),
                        ]
                    )
                    await send({**start, "headers": response_headers})
                    await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        return await self._app(scope, receive, inject)
