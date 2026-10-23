"""Combined dynamic web/gateway and authenticated local connector resources."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import Any

from relaydeck.client.components.design_system.font_versions import is_current as fonts_is_current
from relaydeck.client.serve import WebSessionStore, build_web_app, make_relaydeck_main
from relaydeck.companion.config import CompanionConfig, UsageConfig
from relaydeck.companion.detect import CatalogError, load_catalog
from relaydeck.companion.private import private_dir, read_private
from relaydeck.connector.enrollment import load_creds
from relaydeck.connector.herdr import HerdrError
from relaydeck.connector.link import ConnectorLink, LinkConfig, TunnelTransport
from relaydeck.gateway import webpush, webpush_routes, webpush_store
from relaydeck.gateway.app import create_app
from relaydeck.gateway.bounds import GRACEFUL_SHUTDOWN_S, WS_MAX_SIZE
from relaydeck.gateway.cli import ServeOptions, build_serve_state
from relaydeck.gateway.identity import SessionStore
from relaydeck.gateway.usage import UsageService, live_workspace_labels
from relaydeck.gateway.webpush import WebPushProvider
from relaydeck.gateway.webpush_store import WebPushStore
from relaydeck.logging_setup import logging_config
from relaydeck.persistence_contracts import (
    KeyProvider,
    KeyUnavailable,
    SecretMaterial,
    StorageStatus,
    StorageUnavailable,
)
from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import (
    MappingGap,
    PaneCwdObservation,
    SessionMapping,
    UsageSource,
)
from relaydeck.usage.limits_plane import (
    PROVIDERS,
    STATE_FILENAME,
    TUI_USAGE_PANE_FORMAT_ID,
    LimitsProvider,
    LimitsScheduler,
    ProviderSpec,
    RefreshDecision,
    RefreshState,
    TuiBundle,
    recover_stale_tabs,
    run_bounded_command,
)
from relaydeck.usage.limits_plane.trust_store import ensure_probe_folder, probe_folder
from relaydeck.usage.link_digests import cwd_digest, load_or_create_link_key, repo_fingerprint
from relaydeck.usage.pricing import PriceTable, load_price_table
from relaydeck.usage.readers import READERS
from relaydeck.usage.store import SqliteUsageStore

LOG = logging.getLogger(__name__)

_uvicorn_logging_config = logging_config  # the shared setup; the old name stays for callers


def _route_first_server(config: Any, before_exit: Callable[[], None]) -> Any:
    import uvicorn

    class _RouteFirstServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            with suppress(Exception):
                before_exit()
            super().handle_exit(sig, frame)

    return _RouteFirstServer(config)


_GZIP_SUFFIXES = (".js", ".mjs", ".wasm", ".json", ".css", ".html", ".ttf", ".otf", ".map")


def static_gzip(app: Any) -> Any:
    """Compress the web client's static files (~8 MB of JS) for phones.

    Only GET requests for the page or static asset suffixes are wrapped;
    the gateway API, SSE streams and WebSockets pass through untouched.
    """
    from starlette.middleware.gzip import GZipMiddleware

    zipped = GZipMiddleware(app, minimum_size=1024)

    async def route(scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if (
            scope.get("method") == "GET"
            and not path.startswith("/v1/")
            and (path == "/" or path.endswith(_GZIP_SUFFIXES))
        ):
            await zipped(scope, receive, send)
        else:
            await app(scope, receive, send)

    return route


NO_CACHE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"


def cache_policy(path: str, query: bytes | str = b"") -> str | None:
    """Cache-Control value for a web path, or None to leave it untouched.

    Every non-API response from this origin is either a static web file or
    the SPA shell, and none of those URLs are content-addressed (a Flutter
    upgrade changes canvaskit.js/wasm, fonts and bundles at stable paths),
    so all of them revalidate with ETag/Last-Modified and the browser takes
    304s. There is intentionally no immutable/max-age anywhere: a year-long
    freshness stamp on an unversioned URL would pin phones to a stale engine
    after an upgrade. The one exception is the app's own bundled fonts requested with a
    `v` that matches the file's content fingerprint: that URL changes when the file does.
    /v1/*, the /ws handshake and /healthz are untouched.
    """
    if path in ("/ws", "/healthz"):
        return None
    if path.lstrip("/").startswith("v1/"):
        return None
    if fonts_is_current(path, query):
        return IMMUTABLE
    return NO_CACHE


def static_cache(app: Any) -> Any:
    """Stamp Cache-Control on the web client's static files (phones).

    Composes outside static_gzip; only successful GET/HEAD responses gain a
    header, and an app-set Cache-Control is replaced, never duplicated.
    """

    async def route(scope: Any, receive: Any, send: Any) -> None:
        policy = None
        if scope.get("type") == "http" and scope.get("method") in ("GET", "HEAD"):
            policy = cache_policy(scope.get("path", ""), scope.get("query_string", b""))
        if policy is None:
            await app(scope, receive, send)
            return

        async def send_cached(message: Any) -> None:
            if message["type"] == "http.response.start" and message["status"] < 400:
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", policy.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await app(scope, receive, send_cached)

    return route


class CompanionRuntime:
    """All resources are locally owned; Herdr/agents are never stopped."""

    def __init__(
        self,
        config: CompanionConfig,
        *,
        connect_herdr: bool = False,
        fixture: bool = False,
        server_factory: Callable[[Any], Any] | None = None,
        route_factory: Callable[[], Any] | None = None,
        source_factory: Callable[[], Any] | None = None,
        opener: Callable[[], Coroutine[Any, Any, TunnelTransport]] | None = None,
        durable_signins: bool | None = None,
        migrate_cookie_secret: bool | None = None,
        setup_persistence: bool | None = None,
        invalidate_restored_signins: bool | None = None,
        persistence_provider: KeyProvider | None = None,
    ) -> None:
        self.config = config
        self.connect_herdr = connect_herdr
        self.fixture = fixture
        self.server_factory = server_factory
        self.route_factory = route_factory
        self.route: Any = None
        self.source_factory = source_factory
        self.opener = opener
        self.durable_signins = (
            durable_signins
            if durable_signins is not None
            else os.getenv("RELAYDECK_DURABLE_SIGNINS") == "1"
        )
        self.migrate_cookie_secret = (
            migrate_cookie_secret
            if migrate_cookie_secret is not None
            else os.getenv("RELAYDECK_MIGRATE_COOKIE_SECRET") == "1"
        )
        self.setup_persistence = (
            setup_persistence
            if setup_persistence is not None
            else os.getenv("RELAYDECK_PERSISTENCE_SETUP") == "1"
        )
        self.invalidate_restored_signins = (
            invalidate_restored_signins
            if invalidate_restored_signins is not None
            else os.getenv("RELAYDECK_INVALIDATE_RESTORED_SIGNINS") == "1"
        )
        self.persistence_provider = persistence_provider
        self.persistence: Any = None
        self.persistence_task: asyncio.Task[Any] | None = None
        self.persistence_status = StorageStatus(False, "disabled")
        self.server: Any = None
        self.server_task: asyncio.Task[Any] | None = None
        self.link_task: asyncio.Task[Any] | None = None
        self.source: Any = None
        self.gateway: Any = None
        self.app: Any = None
        self.web_store: WebSessionStore | None = None
        self.webpush_store: WebPushStore | None = None
        self.webpush_provider: WebPushProvider | None = None
        self.webpush_key: Any = None
        self.webpush_topic: bytes = b""
        self.webpush_task: asyncio.Task[None] | None = None
        self.issue = "Start with --connect-herdr after local attachment authorization."
        self.stop: asyncio.Event | None = None
        self.usage_store: SqliteUsageStore | None = None
        self.usage_collector: UsageCollector | None = None
        self.limits_scheduler: LimitsScheduler | None = None
        self._usage_digest: Callable[[str], str | None] | None = None
        self._usage_repo_fp: Callable[[str], str | None] | None = None

    async def start(self, stop: asyncio.Event) -> None:
        self.stop = stop
        cookie, cursor = await asyncio.to_thread(self._storage_secrets)
        private_dir(self.config.state_dir)
        private_dir(self.config.state_dir / "revoke")
        self.gateway = build_serve_state(
            ServeOptions(
                host=self.config.bind_host,
                port=self.config.port,
                origin=self.config.advertised_origin,
                journal_path=str(self.config.state_dir / "gateway-journal.db"),
                push_path=str(self.config.state_dir / "push.db"),
                cursor_secret=cursor,
                log_config="",
                ephemeral_secret=False,
                enrollment_store=str(self.config.state_dir / "enrollments.json"),
                revoke_spool=str(self.config.state_dir / "revoke"),
                host_store=str(self.config.state_dir / "hosts.json"),
                pairing_store=str(self.config.state_dir / "pairing.json"),
                fcm_credentials=str(self.config.fcm_credentials)
                if self.config.fcm_credentials
                else "",
                identity_config=str(self.config.identity_config)
                if self.config.identity_config
                else "",
            )
        )
        if self.persistence is not None:
            provider = self.persistence_provider
            assert provider is not None  # noqa: S101
            try:
                self.gateway.sessions = SessionStore(
                    persistence=self.persistence,
                    origin=self.config.advertised_origin,
                    generation_reader=lambda: provider.restore_generation("authentication").value,
                )
            except (KeyUnavailable, StorageUnavailable):
                await asyncio.to_thread(self.persistence.close)
                self.persistence = None
                self.persistence_status = StorageStatus(False, "keys_unavailable")
                cookie = secrets.token_bytes(32)
            else:
                self.persistence_task = asyncio.create_task(self._maintain_storage())
        from relaydeck.companion.pictures import PictureStore

        self.gateway.pictures = PictureStore(host_id=self.config.host_id)
        if self.config.usage is not None:
            await asyncio.to_thread(self._prepare_usage)
            # Synchronous on this thread, with no awaits: startup cancellation cannot land
            # inside it, so no upgrade can still be running when shutdown starts.
            self.gateway.usage.run_startup_upgrades()
        gateway_app = create_app(self.gateway)
        if self.config.web_ui and self.config.notifications.web_push:
            # Web push is the default route: on with the web UI, switchable in [notifications].
            self._start_web_push(gateway_app)
        if self.config.web_ui:
            self.app = self._web_app(gateway_app, cookie)
        else:
            # Bridge only: the Android app pairs and talks to this API; no
            # browser UI, web sessions or Flet runtime are started.
            self.app = gateway_app
        await self._start_server()
        if self.config.tailscale_auto:
            self._open_route()
        await self._connect()
        if self.webpush_store is not None:
            self.webpush_task = asyncio.create_task(self._webpush_loop())
        if self.usage_collector is not None:
            self.usage_collector.start()
        if self.limits_scheduler is not None:
            self.limits_scheduler.start()

    def _start_web_push(self, gateway_app: Any) -> None:
        """keys, store, subscribe/unsubscribe routes and the event sink.

        The routes go on the gateway app before the web app copies its routes, so they sit
        ahead of the Flet mount."""
        from relaydeck.client.serve import COOKIE_NAME

        state = self.config.state_dir
        self.webpush_key = webpush.load_or_create_vapid(state)
        self.webpush_topic = webpush.load_or_create_topic_key(state)
        self.webpush_store = WebPushStore(state)
        self.webpush_provider = WebPushProvider(
            self.webpush_key, subject=self.config.notifications.vapid_subject
        )

        def identity(request: Any) -> tuple[str, str] | None:
            # A bearer credential (the packaged app's gateway calls) or the browser session cookie.
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer ") and self.gateway is not None:
                token = authorization[7:].strip()
                session = (
                    self.gateway.sessions.lookup(token, self.gateway.now_ms()) if token else None
                )
                if session is None or not session.device_id:
                    return None
                return (session.account, session.device_id)
            web = self.web_store
            if web is None:
                return None
            sid = web.verify_cookie(request.cookies.get(COOKIE_NAME, ""))
            current = web.identity(sid) if sid else None
            return None if current is None else (current.account, current.device_id)

        gateway_app.include_router(
            webpush_routes.build_router(
                store=self.webpush_store,
                identity=identity,
                origin=self.config.advertised_origin,
                vapid_key=self.webpush_key,
                topic_key=self.webpush_topic,
                preferences=self.gateway.push,
                settings=webpush_routes.WebPushSettings(
                    enabled=True,
                    lock_screen_detail=self.config.notifications.lock_screen_detail,
                    subject=self.config.notifications.vapid_subject,
                ),
            )
        )
        if self.gateway is not None:
            self.gateway.web_push_sink = self._webpush_enqueue

    def _webpush_enqueue(
        self,
        account: str,
        devices: frozenset[str],
        event_id: str,
        category: str,
        host: str,
        session: str,
    ) -> None:
        store, gateway = self.webpush_store, self.gateway
        if store is None or gateway is None:
            return
        now = gateway.now_ms()
        for device_id in devices:
            if not gateway.push.category_allowed(account, device_id, category):
                continue  # the device turned this category off
            with suppress(ValueError):  # a malformed event never blocks the other devices
                store.enqueue(
                    account,
                    device_id,
                    event_id,
                    category,
                    now=now,
                    label=host[:64],
                    host=host,
                    session=session,
                )

    async def _webpush_device_active(self, account: str, device_id: str) -> bool:
        gateway = self.gateway
        return gateway is not None and device_id in gateway.sessions.device_metadata(
            account, gateway.now_ms()
        )

    def _webpush_pass(self, loop: asyncio.AbstractEventLoop) -> None:
        """Runs in a worker thread. Each send is re-authorized on the event loop, where the session
        store lives, immediately before it happens."""
        store, provider, gateway = self.webpush_store, self.webpush_provider, self.gateway
        if store is None or provider is None or gateway is None:
            return

        def authorized(account: str, device_id: str) -> bool:
            future = asyncio.run_coroutine_threadsafe(
                self._webpush_device_active(account, device_id), loop
            )
            return bool(future.result(timeout=5))

        webpush_store.pump(
            store,
            provider,
            topic_key=self.webpush_topic,
            now=gateway.now_ms(),
            lock_screen_detail=self.config.notifications.lock_screen_detail,
            authorized=authorized,
            allowed=gateway.push.category_allowed,
        )
        store.purge(gateway.now_ms())

    async def _webpush_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self.stop is None or not self.stop.is_set():
            try:
                await asyncio.to_thread(self._webpush_pass, loop)
            except Exception:  # noqa: BLE001 - bounded: nothing about subscriptions is logged
                LOG.warning("webpush_pass_failed")
            if self.stop is None:
                await asyncio.sleep(2)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), 2)

    def _usage_mappings(self) -> tuple[SessionMapping, ...]:
        source = self.source
        provider = getattr(source, "usage_mappings", None)
        return tuple(provider()) if provider is not None else ()

    def _usage_mapping_gaps(self) -> tuple[MappingGap, ...]:
        provider = getattr(self.source, "usage_mapping_gaps", None)
        return tuple(provider()) if provider is not None else ()

    def _acknowledge_usage(
        self, source_key: str, mappings: tuple[SessionMapping, ...], gaps: tuple[MappingGap, ...]
    ) -> None:
        acknowledge = getattr(self.source, "acknowledge_usage", None)
        if acknowledge is not None:
            acknowledge(source_key, mappings, gaps)

    def _usage_cwd_observations(self) -> tuple[PaneCwdObservation, ...]:
        provider = getattr(self.source, "usage_cwd_observations", None)
        return tuple(provider()) if provider is not None else ()

    def _acknowledge_cwd(
        self, source_key: str, observations: tuple[PaneCwdObservation, ...]
    ) -> None:
        acknowledge = getattr(self.source, "acknowledge_cwd", None)
        if acknowledge is not None:
            acknowledge(source_key, observations)

    def _prepare_usage(self) -> None:
        config = self.config.usage
        assert config is not None  # noqa: S101
        prices = PriceTable()
        if config.price_tables:
            try:
                prices = load_price_table(config.price_tables)
            except (OSError, ValueError):
                LOG.warning("usage_prices_unavailable reason=invalid_price_table")
        try:
            self.usage_store = SqliteUsageStore(
                self.config.state_dir / "usage.db",
                timezone=config.timezone,
                prices=prices,
            )
            sources = tuple(
                UsageSource(
                    self.config.host_id, s.kind, s.format, s.root, s.limits_format, s.limits_cache
                )
                for s in config.sources
            )
            link_key = load_or_create_link_key(self.config.state_dir)
            if link_key is None:
                LOG.warning("usage_link_digests_unavailable reason=link_key_unavailable")
            self._usage_digest = (
                (lambda folder: cwd_digest(link_key, folder)) if link_key is not None else None
            )
            self._usage_repo_fp = (
                (lambda repo: repo_fingerprint(link_key, repo)) if link_key is not None else None
            )
            self.usage_collector = UsageCollector(
                self.usage_store,
                READERS,
                sources,
                self._usage_mappings,
                mapping_gaps=self._usage_mapping_gaps,
                acknowledge_mappings=self._acknowledge_usage,
                digest=self._usage_digest,
                cwd_observations=self._usage_cwd_observations,
                acknowledge_cwd=self._acknowledge_cwd,
                inferred_linking=config.inferred_linking,
            )
            self.limits_scheduler = self._prepare_limits(config)
            assert self.usage_store is not None  # noqa: S101
            retired = self.usage_store.retire_missing_sources(
                self.config.host_id,
                [s.source_key for s in sources],
                list(self.limits_scheduler.spec_keys) if self.limits_scheduler is not None else [],
            )
            if retired:
                LOG.info("usage_sources_retired count=%d", retired)
        except Exception:
            if self.usage_store is not None:
                self.usage_store.close()
            self.usage_store = None
            self.limits_scheduler = None
            LOG.warning("usage_unavailable reason=storage_unavailable")
        scheduler = self.limits_scheduler

        def request_refresh(kind: str) -> RefreshDecision:
            if scheduler is None:
                return RefreshDecision("not_configured")
            key = scheduler.on_demand_key(kind)
            if key is None:
                return RefreshDecision("not_configured")
            return scheduler.request_on_demand(key)

        self.gateway.usage = UsageService(
            self.usage_store,
            config,
            self.config.host_id,
            self.config.state_dir,
            self.gateway.now_ms,
            workspace_labels=lambda: live_workspace_labels(self.gateway, self.config.host_id),
            limits_status=(scheduler.status if scheduler is not None else None),
            limits_refresh_status=(scheduler.refresh_status if scheduler is not None else None),
            request_limits_refresh=request_refresh if scheduler is not None else None,
            on_usage_open=scheduler.notify_usage_open if scheduler is not None else None,
        )
        self.gateway.usage.check_setup()

    def _prepare_limits(self, config: UsageConfig) -> LimitsScheduler | None:
        """Build the limits-plane scheduler for enabled blocks with a known
        provider. Unknown formats are skipped (later providers register them);
        with nothing resolved, no scheduler runs and nothing changes."""
        assert self.usage_store is not None  # noqa: S101
        specs: dict[str, ProviderSpec] = {}
        providers: dict[str, LimitsProvider] = {}
        skipped = 0
        catalog: dict[str, Any] = {}
        try:
            for entry in load_catalog().entries:
                catalog[entry.id] = entry
                for alias in entry.aliases:
                    catalog.setdefault(alias, entry)
        except CatalogError:
            LOG.warning("usage_limits_skipped reason=catalog_unavailable")
        for block in config.limits:
            if not block.enabled:
                continue
            provider = PROVIDERS.get(block.format)
            if provider is None:
                skipped += 1
                continue
            bundle: TuiBundle | None = None
            if block.format == TUI_USAGE_PANE_FORMAT_ID:
                bundle = self._tui_bundle(config, block.kind, catalog)
                if bundle is None:
                    skipped += 1
                    continue
                if block.command:
                    LOG.warning(
                        "usage_limits_ignored reason=command_ignored_on_tui_block kind=%s",
                        block.kind,
                    )
            key = f"{self.config.host_id}/{block.kind}/{block.format}"
            specs[key] = ProviderSpec(
                key,
                block.kind,
                block.format,
                block.min_interval_seconds * 1000,
                float(block.timeout_seconds),
                command=block.command,
                on_demand=block.on_demand,
                tui=bundle,
                auto_interval_ms=block.auto_interval_seconds * 1000 if block.on_demand else 0,
            )
            providers[key] = provider
        if skipped:
            LOG.warning("usage_limits_skipped reason=unknown_format count=%d", skipped)
        if not specs:
            return None
        refresh: RefreshState | None = None
        if any(spec.on_demand for spec in specs.values()):
            refresh = RefreshState(self.config.state_dir / STATE_FILENAME)
            self._recover_refresh_tabs(refresh)
        return LimitsScheduler(
            self.usage_store,
            specs,
            providers,
            key_path=self.config.state_dir / "limits-account.key",
            refresh=refresh,
        )

    def _tui_bundle(
        self, config: UsageConfig, kind: str, catalog: dict[str, Any]
    ) -> TuiBundle | None:
        """Resolve one TUI block's recipe + herdr binary. None when the kind
        has no catalog recipe (the block is skipped, visibly)."""
        entry = catalog.get(kind)
        recipe = getattr(entry, "tui", None)
        if recipe is None:
            LOG.warning("usage_limits_skipped reason=no_tui_recipe kind=%s", kind)
            return None
        found = shutil.which("herdr")
        herdr: tuple[str, ...] = ()
        if found is not None and os.path.isabs(found) and os.access(found, os.X_OK):
            herdr = (found,)
        else:
            LOG.warning("usage_limits_no_herdr kind=%s", kind)
        probe = probe_folder(recipe.probe_dir, self.config.state_dir)
        if probe is not None and not ensure_probe_folder(probe):
            LOG.warning("usage_probe_folder_unavailable kind=%s", kind)
        return TuiBundle(
            recipe,
            herdr,
            self.config.state_dir / STATE_FILENAME,
            config.timezone,
            os.path.expanduser(self.config.herdr_socket),
        )

    def _recover_refresh_tabs(self, refresh: RefreshState) -> None:
        """Restart recovery: close proven-ours leftover refresh tabs (bounded)."""
        found = shutil.which("herdr")
        if found is None or not refresh.unresolved():
            return

        def run(argv: Any, timeout_s: float) -> bytes:
            return run_bounded_command(argv, timeout_s=timeout_s)

        try:
            outcome = recover_stale_tabs(
                refresh, (found,), os.path.expanduser(self.config.herdr_socket), run
            )
        except Exception:
            LOG.warning("usage_limits_recovery_failed")
            return
        if any(outcome.values()):
            LOG.info("usage_limits_recovered %r", outcome)

    def _storage_secrets(self) -> tuple[bytes, bytes]:
        if not self.durable_signins:
            return self.config.secrets()
        cursor = read_private(self.config.cursor_secret_file).strip().encode()
        if len(cursor) < 32:
            raise ValueError("cursor secret unavailable")
        from relaydeck.companion.credentials import MacKeyProvider
        from relaydeck.companion.persistence import open_store

        try:
            provider = self.persistence_provider or MacKeyProvider()
            self.persistence_provider = provider
            provider.restore_generation(
                "authentication",
                create=self.setup_persistence,
                allow_prompt=self.setup_persistence,
            )
            if self.migrate_cookie_secret:
                # Preserve the exact EFFECTIVE bytes today's configured signer uses.
                cookie = self.config.secrets()[0]
                provider.import_secret(
                    "cookie-signing", SecretMaterial(cookie), allow_prompt=self.setup_persistence
                )
            cookie = provider.read_secret("cookie-signing").value
            if len(cookie) < 32 or cookie == cursor:
                raise StorageUnavailable("invalid_record")
            if self.invalidate_restored_signins:
                provider.advance_restore_generation("authentication")
            self.persistence = open_store(self.config, provider)
            self.persistence_status = self.persistence.status()
            if not self.persistence_status.available:
                raise StorageUnavailable(self.persistence_status.reason)
            return cookie, cursor
        except (KeyUnavailable, StorageUnavailable, ValueError) as exc:
            self.persistence_status = StorageStatus(
                False, exc.code if isinstance(exc, StorageUnavailable) else "keys_unavailable"
            )
            if self.persistence is not None:
                self.persistence.close()
                self.persistence = None
            # Startup stays available with NEW in-memory pairing. Do not bypass
            # a locked Keychain by reading the retained plaintext signing file.
            return secrets.token_bytes(32), cursor

    async def _maintain_storage(self) -> None:
        assert self.stop is not None and self.persistence is not None  # noqa: S101
        while not self.stop.is_set():
            try:
                report = await asyncio.to_thread(self.persistence.sweep, self.gateway.now_ms())
                self.persistence_status = StorageStatus(True)
                delay = 0.1 if report.more else 30.0
            except StorageUnavailable as exc:
                self.persistence_status = exc.status
                delay = 30.0
            try:
                await asyncio.wait_for(self.stop.wait(), delay)
            except TimeoutError:
                pass

    def _open_route(self) -> None:
        if self.route_factory is not None:
            self.route = self.route_factory()
        else:
            from relaydeck.companion.tailscale import PrivateRoute

            self.route = PrivateRoute(self.config.tailscale_https_port, self.config.port)
        self.route.ensure()

    def _web_app(self, gateway_app: Any, cookie: bytes) -> Any:
        self.web_store = WebSessionStore(
            self.config.advertised_origin,
            cookie,
            persistence=self.persistence,
            gateway_sessions=self.gateway.sessions,
        )
        from relaydeck.client.adapters.server_commands import ServerCommandStore

        self.web_store.command_store = ServerCommandStore(
            self.config.state_dir / "pending-commands.db"
        )
        flet_main = make_relaydeck_main(
            store=self.web_store,
            base_url=self.config.local_endpoint,
            gateway_app=gateway_app,
            gateway_state=self.gateway,
            host_id=self.config.host_id,
            setup_report=self.setup_report,
        )
        app, _ = build_web_app(
            gateway_state=self.gateway,
            gateway_app=gateway_app,
            flet_main=flet_main,
            public_origin=self.config.advertised_origin,
            cookie_secret=cookie,
            store=self.web_store,
        )
        return static_cache(static_gzip(app))

    async def _start_server(self) -> None:
        if self.server_factory:
            self.server = self.server_factory(self.app)
        else:
            import uvicorn

            self.server = _route_first_server(
                uvicorn.Config(
                    self.app,
                    host=self.config.bind_host,
                    port=self.config.port,
                    workers=1,
                    log_config=_uvicorn_logging_config(),
                    access_log=False,
                    proxy_headers=False,
                    timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
                    ws_max_size=WS_MAX_SIZE,
                ),
                self._request_shutdown,
            )
        self.server_task = asyncio.create_task(self._serve_owned(), name="companion-web")
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.server_task.done():
                    cause = None if self.server_task.cancelled() else self.server_task.exception()
                    raise RuntimeError(
                        "loopback server could not start; preserve the existing listener"
                    ) from cause
                await asyncio.sleep(0.02)

    async def _connect(self) -> None:
        if not (self.connect_herdr or self.fixture):
            return
        credentials = self.config.connector_credentials
        if not credentials.exists():
            self.issue = (
                "Enroll this Mac's connector using a public-key-bound invitation, "
                "then restart explicitly."
            )
            return
        _ = read_private(credentials)
        creds = load_creds(str(credentials))
        if (
            creds is None
            or creds.host_id != self.config.host_id
            or creds.gateway_url.rstrip("/") != self.config.advertised_origin
            or self.gateway.hosts.lookup(creds.host_token) is None
        ):
            self.issue = (
                "Connector enrollment expired or belongs to another endpoint; renew it explicitly."
            )
            return
        if self.source_factory:
            self.source = self.source_factory()
        elif self.fixture:
            from relaydeck.connector.fixture import FixtureAdapter

            self.source = FixtureAdapter()
        else:
            from relaydeck.connector.herdr import HerdrAdapter
            from relaydeck.connector.source import HerdrSource

            self.source = HerdrSource(
                HerdrAdapter(socket_path=self.config.herdr_socket),
                agent_kinds=self.config.agent_kinds,
            )
        if hasattr(self.source, "bootstrap"):
            if self.usage_collector is not None and hasattr(self.source, "configure_usage"):
                self.source.configure_usage(
                    self.usage_collector.sources,
                    digest=self._usage_digest,
                    repo_fingerprint=self._usage_repo_fp,
                )
            try:
                async with asyncio.timeout(10):
                    await self.source.bootstrap()
            except (HerdrError, OSError, RuntimeError, TimeoutError):
                self.issue = (
                    "Herdr is offline or incompatible. "
                    "Check the socket and protocol 22/version baseline."
                )
                return
        link = ConnectorLink(
            LinkConfig(
                gateway_url=self.config.local_endpoint,
                host_id=creds.host_id,
                host_token=creds.host_token,
                journal_path=str(self.config.state_dir / "connector-journal.db"),
            ),
            self.source,
            opener=self.opener,
        )
        assert self.stop is not None  # noqa: S101
        self.link_task = asyncio.create_task(link.run(self.stop), name="companion-connector")
        self.issue = "Waiting for authenticated connector synchronization."

    async def _serve_owned(self) -> None:
        try:
            await self.server.serve()
        except SystemExit as exc:
            # Uvicorn exits on bind failure. Convert inside the owned task
            # so the foreground supervisor can clean every partial resource.
            raise RuntimeError("loopback server could not start") from exc

    def setup_report(self) -> tuple[tuple[str, str], ...]:
        connected = bool(self.gateway and self.gateway.sockets.host_count(self.config.host_id))
        return (
            (
                "Sign-in storage",
                "Durable encrypted sign-ins available."
                if self.persistence_status.available
                else "In-memory sign-ins only: " + self.persistence_status.reason + ".",
            ),
            ("Private address", self.config.advertised_origin),
            (
                "Device authorization",
                "Approve a device on this Mac with a matching code (relaydeck pairing)."
                if self.gateway and self.gateway.pairing is not None
                else "Device pairing is unavailable; inspect the foreground companion setup.",
            ),
            (
                "Mac connector enrollment",
                "Authenticated connector connected." if connected else self.issue,
            ),
            (
                "Push provider",
                "FCM configured; delivery/device proof pending."
                if self.gateway and self.gateway.push_provider is not None
                else "Not configured; foreground status remains available.",
            ),
            (
                "Private network",
                "Use doctor --probe-private to check HTTPS reachability. The Mac must stay awake.",
            ),
        )

    def report(self) -> dict[str, Any]:
        if self.persistence is not None:
            self.persistence_status = self.persistence.status()
        server_dead = self.server_task is None or self.server_task.done()
        connected = bool(self.gateway and self.gateway.sockets.host_count(self.config.host_id))
        synchronized = bool(
            self.gateway and self.gateway.backend.snapshots.get(self.config.host_id)
        )
        connector_dead = self.link_task is not None and self.link_task.done()
        if connector_dead:
            self.issue = "Connector stopped; check enrollment/Herdr, then restart explicitly."
        return {
            "signin_storage": self.persistence_status.reason,
            "durable_signins": self.persistence_status.available,
            "state": "failed" if server_dead or connector_dead else "setup_required",
            "failed": server_dead or connector_dead,
            "advertised_origin": self.config.advertised_origin,
            "local_endpoint": self.config.local_endpoint,
            "web_ui": self.config.web_ui,
            "web_ready": self.config.web_ui and not server_dead,
            "api_ready": not server_dead,
            "connector_ready": connected and synchronized,
            "identity_ready": bool(self.gateway and self.gateway.identity_provider is not None),
            "pairing_available": bool(self.gateway and self.gateway.pairing is not None),
            "synthetic": self.fixture,
            "diagnostics": dict(self.setup_report()),
        }

    def _remove_route(self) -> None:
        """Idempotent: drop this run's tailnet route before the listener closes."""
        route, self.route = self.route, None
        if route is not None:
            with suppress(Exception):
                route.remove()

    def _request_shutdown(self) -> None:
        """Shared stop/signal edge, before Uvicorn starts its graceful wait."""
        self._remove_route()
        if self.stop is not None:
            self.stop.set()
        if self.usage_store is not None:
            self.usage_store.begin_shutdown()

    async def close(self) -> None:
        self._request_shutdown()
        # Stop accepting work before joining producers, so joins never queue behind
        # usage requests in the default pool.
        if self.server is not None:
            self.server.should_exit = True
        if self.gateway is not None and self.gateway.usage is not None:
            await self.gateway.usage.close()
        await self._finish(self.webpush_task)
        if self.usage_collector is not None:
            await asyncio.to_thread(self.usage_collector.stop)
        if self.limits_scheduler is not None:
            await asyncio.to_thread(self.limits_scheduler.stop)
        await self._finish(self.persistence_task)
        try:
            await self._finish(self.link_task)
            if self.source is not None and hasattr(self.source, "close"):
                async with asyncio.timeout(6):
                    await self.source.close()
        finally:
            try:
                await self._finish(self.server_task)
            finally:
                if self.gateway is not None:
                    if self.usage_store is not None:
                        try:
                            await asyncio.to_thread(self.usage_store.close)
                        except Exception:
                            LOG.warning("companion_shutdown_pending resource=usage_store")
                    await asyncio.to_thread(self.gateway.backend.journal.close)
                    await asyncio.to_thread(self.gateway.push.close)
                if self.webpush_provider is not None:
                    self.webpush_provider.close()
                if self.webpush_store is not None:
                    self.webpush_store.close()
                if self.web_store is not None and self.web_store.command_store is not None:
                    self.web_store.command_store.close()
                if self.persistence is not None:
                    await asyncio.to_thread(self.persistence.close)

    @staticmethod
    async def _finish(task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), 6)
        except Exception:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
