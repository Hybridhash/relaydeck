"""Web Push HTTP routes: public key, subscribe, unsubscribe and tap-time route checks (rule 3).

Identity comes only from the authenticated browser session (the ``identity`` callable), never from
the request body. Each mutation needs the exact public Origin and the per-session CSRF token that
``GET /v1/push/web/key`` returns. A cross-site page cannot read that response, so it cannot forge
the header. Every response is ``Cache-Control: no-store``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from relaydeck.gateway import webpush
from relaydeck.gateway.push import PREF_CATEGORY_TO_UI, PREF_UI_TO_CATEGORY
from relaydeck.gateway.webpush_store import WebPushStore

MAX_BODY_BYTES = 2048
DEVICE_LIMIT = 20  # registrations per device per hour
ACCOUNT_LIMIT = 60  # registrations per account per hour
RATE_WINDOW_SECONDS = 3600
ROUTE_RESOLVE_LIMIT = 120  # tap checks per device per hour
_SUBSCRIPTION_FIELDS = frozenset({"endpoint", "keys"})

Identity = Callable[[Request], "tuple[str, str] | None"]


@dataclass(frozen=True)
class WebPushSettings:
    enabled: bool = True
    lock_screen_detail: bool = False
    subject: str = webpush.VAPID_SUBJECT_DEFAULT


@dataclass
class _Window:
    limit: int
    events: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str, now: float) -> bool:
        window = self.events.setdefault(key, deque())
        while window and now - window[0] >= RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True


def csrf_token(topic_key: bytes, account: str, device_id: str) -> str:
    message = f"csrf-v1:{account}:{device_id}".encode()
    return webpush.b64u(hmac.new(topic_key, message, hashlib.sha256).digest())


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(status: int, code: str) -> Response:
    return _no_store(JSONResponse({"error": {"code": code}}, status_code=status))


def build_router(
    *,
    store: WebPushStore,
    identity: Identity,
    origin: str,
    vapid_key: Any,
    topic_key: bytes,
    settings: WebPushSettings,
    preferences: Any,
    clock: Callable[[], float] = time.time,
) -> APIRouter:
    """`preferences` is the push store (per-device category choices)."""
    router = APIRouter()
    registrations = _Window(DEVICE_LIMIT)
    account_registrations = _Window(ACCOUNT_LIMIT)
    resolves = _Window(ROUTE_RESOLVE_LIMIT)

    def _mutation_refused(request: Request, who: tuple[str, str]) -> Response | None:
        # A bearer credential is never ambient: a cross-site page cannot attach an Authorization
        # header without a preflight, which the gateway refuses. The CSRF gate is for cookies.
        if request.headers.get("authorization"):
            return None
        # Exact Origin: browsers send it on cross-origin mutations; a missing or other one is out.
        if request.headers.get("origin") != origin:
            return _error(403, "forbidden")
        expected = csrf_token(topic_key, who[0], who[1])
        supplied = request.headers.get("x-relaydeck-csrf", "")
        if not hmac.compare_digest(supplied, expected):
            return _error(403, "forbidden")
        return None

    @router.get("/v1/push/web/key")
    async def public_key(request: Request) -> Response:
        who = identity(request)
        if who is None:
            return _error(401, "unauthorized")
        return _no_store(
            JSONResponse(
                {
                    "publicKey": webpush.public_key_b64u(vapid_key),
                    "enabled": settings.enabled and store.sending_enabled(),
                    # Whether THIS device has a subscription: the banner and Settings read it.
                    "subscribed": store.subscription(who[0], who[1]) is not None,
                    "csrf": csrf_token(topic_key, who[0], who[1]),
                    # THIS device's choice: a change brings back a bar the user closed.
                    "choice": sorted(
                        PREF_CATEGORY_TO_UI[name]
                        for name in preferences.preferences(who[0], who[1])
                    ),
                }
            )
        )

    @router.put("/v1/push/web/subscription")
    async def subscribe(request: Request) -> Response:
        who = identity(request)
        if who is None:
            return _error(401, "unauthorized")
        refused = _mutation_refused(request, who)
        if refused is not None:
            return refused
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _error(413, "too_large")
        try:
            doc: Any = json.loads(raw)
        except ValueError:
            return _error(400, "invalid")
        if not isinstance(doc, dict) or set(doc) != _SUBSCRIPTION_FIELDS:
            return _error(400, "invalid")
        keys = doc.get("keys")
        if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
            return _error(400, "invalid")
        now = clock()
        if not registrations.allow(who[1], now) or not account_registrations.allow(who[0], now):
            return _error(429, "rate_limited")
        try:
            store.put_subscription(
                who[0],
                who[1],
                str(doc["endpoint"]),
                str(keys["p256dh"]),
                str(keys["auth"]),
                int(now * 1000),
            )
        except (webpush.EndpointRejected, ValueError, TypeError):
            # Generic refusal: the reason could otherwise be used to probe the allowlist.
            return _error(400, "invalid")
        return _no_store(JSONResponse({"ok": True}, status_code=201))

    @router.delete("/v1/push/web/subscription")
    async def unsubscribe(request: Request) -> Response:
        who = identity(request)
        if who is None:
            return _error(401, "unauthorized")
        refused = _mutation_refused(request, who)
        if refused is not None:
            return refused
        store.delete_subscription(who[0], who[1])
        return _no_store(Response(status_code=204))

    @router.post("/v1/push/web/resolve")
    async def resolve(request: Request) -> Response:
        """Tap path: returns the category only when the route belongs to this device and is current.
        It authorizes nothing and sends no input. The app then fetches current state as usual."""
        who = identity(request)
        if who is None:
            return _error(401, "unauthorized")
        refused = _mutation_refused(request, who)
        if refused is not None:
            return refused
        if not resolves.allow(who[1], clock()):
            return _error(429, "rate_limited")
        raw = await request.body()
        try:
            doc: Any = json.loads(raw) if len(raw) <= 256 else None
        except ValueError:
            doc = None
        token = doc.get("route") if isinstance(doc, dict) else None
        if not isinstance(token, str):
            return _error(400, "invalid")
        found = store.resolve_route(who[0], who[1], token, int(clock() * 1000))
        if found is None:
            return _error(404, "not_found")
        return _no_store(JSONResponse(found))

    @router.get("/v1/push/prefs")
    async def get_preferences(request: Request) -> Response:
        who = identity(request)
        if who is None:
            return _error(401, "unauthorized")
        chosen = preferences.preferences(who[0], who[1])
        return _no_store(
            JSONResponse({"categories": sorted(PREF_CATEGORY_TO_UI[name] for name in chosen)})
        )

    @router.put("/v1/push/prefs")
    async def put_preferences(request: Request) -> Response:
        """The choice for THIS device only. Unknown values are refused; [] means none."""
        who = identity(request)
        if who is None:
            return _error(401, "unauthorized")
        refused = _mutation_refused(request, who)
        if refused is not None:
            return refused
        raw = await request.body()
        if len(raw) > 1024:
            return _error(413, "too_large")
        try:
            doc: Any = json.loads(raw)
        except ValueError:
            return _error(400, "invalid")
        if not isinstance(doc, dict) or set(doc) != {"categories"}:
            return _error(400, "invalid")
        values = doc["categories"]
        if (
            not isinstance(values, list)
            or len(values) > len(PREF_UI_TO_CATEGORY)
            or not all(isinstance(value, str) for value in values)
            or len(set(values)) != len(values)
            or not set(values) <= set(PREF_UI_TO_CATEGORY)
        ):
            return _error(400, "invalid")
        preferences.set_preferences(
            who[0],
            who[1],
            [PREF_UI_TO_CATEGORY[value] for value in values],
            int(clock() * 1000),
        )
        return _no_store(JSONResponse({"categories": sorted(values)}))

    return router
