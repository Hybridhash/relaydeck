"""Usage refresh lifecycle. Pure state plus one async loop; no Flet.

- `UsageFeed` keeps the last good page and a generation counter. Every request is tagged with
  the generation it started under; a result from an older generation (host, filter or account
  changed) is discarded. A failure keeps the last good page, with its age and the error code.
- `run_usage_loop` refreshes while active: one request at a time (the next wait starts only after
  the previous request finished), then waits `interval` seconds. It stops as soon as `active()`
  is false, and it refreshes immediately when it starts, which is how a resume fetches.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, replace

from relaydeck.client.features.usage.model import UsagePage

INTERVAL_SECONDS = 5.0
# A busy server, a transport timeout or a refusal without a page yet is not a verdict: the screen
# keeps loading and retries with a backoff (1 s, 2 s, 4 s, ... up to RETRY_CAP_SECONDS) until
# UNAVAILABLE_AFTER_SECONDS have passed since the first failure of the current query.
RETRY_CAP_SECONDS = 8.0
UNAVAILABLE_AFTER_SECONDS = 30.0
# Recently loaded pages are kept in memory per query identity, for the current account and host
# only: switching back to a period shows its page at once, marked "updating…" while it refreshes.
CACHE_ENTRIES = 4
CACHE_MAX_AGE_SECONDS = 300.0
TRANSIENT_CODES = frozenset({"usage_busy", "usage_unavailable", "host_offline", "usage_timeout"})

STATUS_IDLE = "idle"
STATUS_LOADING = "loading"
STATUS_READY = "ready"
STATUS_ERROR = "error"  # a failure after a good page: the page stays, with the error
STATUS_UNAVAILABLE = "unavailable"  # no good page yet and the report cannot be produced
STATUS_DISABLED = "disabled"  # the host has usage turned off


@dataclass(frozen=True)
class FeedState:
    status: str = STATUS_IDLE
    page: UsagePage | None = None
    observed_ms: int | None = None
    error: str | None = None
    # The identity-scoped inventory (known spaces and providers) of an over-capacity report. It is
    # not usage: its totals are unknown, but the choices it names stay available.
    inventory: UsagePage | None = None


class UsageFeed:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._key: Hashable | None = None
        self._generation = 0
        self._clock = clock
        self._failures = 0  # consecutive failures of the current query
        self._first_failure: float | None = None
        self._scope: Hashable | None = None
        self._cache: OrderedDict[Hashable, tuple[UsagePage, int, float]] = OrderedDict()
        self.state = FeedState()

    @property
    def generation(self) -> int:
        return self._generation

    def _cached(self, key: Hashable) -> FeedState | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        page, observed_ms, stored = entry
        if self._clock() - stored > CACHE_MAX_AGE_SECONDS:
            del self._cache[key]
            return None
        return FeedState(status=STATUS_LOADING, page=page, observed_ms=observed_ms)

    def set_key(self, key: Hashable, scope: Hashable | None = None) -> int:
        """A new identity (account, transport, host), query (days or space) or filter starts a new
        generation and clears the page: data from another context is never shown under this one.
        The same key keeps the last good page.

        `scope` is the account and host. A new scope clears the page cache; a new query inside the
        same scope starts from that query's cached page when it is recent, still "loading" so the
        refresh runs and the screen says "updating…"."""
        if key != self._key:
            if scope != self._scope:
                self._cache.clear()
                self._scope = scope
            self._key = key
            self._generation += 1
            self._failures = 0
            self._first_failure = None
            self.state = self._cached(key) or FeedState(status=STATUS_LOADING)
        return self._generation

    def begin(self) -> int:
        """A refresh starts under the current generation."""
        if self.state.status != STATUS_READY:
            self.state = FeedState(
                status=STATUS_LOADING,
                page=self.state.page,
                observed_ms=self.state.observed_ms,
            )
        return self._generation

    def accept(self, generation: int, page: UsagePage, observed_ms: int) -> bool:
        if generation != self._generation:
            return False
        self._failures = 0
        self._first_failure = None
        if page.capacity != "ok":
            # Over the server's report cap: the report has no usable totals, so it is unavailable
            # and
            # never zero usage. Its spaces and providers are kept as the inventory for the picker
            # choices.
            self.state = FeedState(
                status=STATUS_UNAVAILABLE, error="capacity_exceeded", inventory=page
            )
            return True
        self.state = FeedState(status=STATUS_READY, page=page, observed_ms=observed_ms, error=None)
        if self._key is not None:
            self._cache[self._key] = (page, observed_ms, self._clock())
            self._cache.move_to_end(self._key)
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return True

    def fail(self, generation: int, code: str, *, disabled: bool = False) -> bool:
        if generation != self._generation:
            return False
        now = self._clock()
        self._failures += 1
        if self._first_failure is None:
            self._first_failure = now
        if disabled:
            self.state = FeedState(status=STATUS_DISABLED, page=None, observed_ms=None, error=code)
        elif self.state.page is None:
            if code in TRANSIENT_CODES and now - self._first_failure < UNAVAILABLE_AFTER_SECONDS:
                # Still loading: a busy server or a slow link is retried, not reported.
                self.state = FeedState(status=STATUS_LOADING, error=code)
            else:
                self.state = FeedState(status=STATUS_UNAVAILABLE, error=code)
        else:
            self.state = FeedState(
                status=STATUS_ERROR,
                page=self.state.page,
                observed_ms=self.state.observed_ms,
                error=code,
            )
        return True

    def retry_delay(self) -> float:
        """Seconds before the next request: the normal interval, or the backoff while the first
        load of this query keeps failing transiently."""
        if self._failures and self.state.page is None and self.state.status == STATUS_LOADING:
            return min(RETRY_CAP_SECONDS, float(2 ** (self._failures - 1)))
        return INTERVAL_SECONDS

    def stop(self) -> None:
        """Polling stops (hidden, backgrounded or unmounted). In-flight results are obsolete, so
        they
        are dropped. The last page stays: it is the same context's data, and the view shows its age.
        A change of host, account, query or space is what clears data (see `set_key`)."""
        self._generation += 1

    def visible(self, key: Hashable, active: bool, scope: Hashable | None = None) -> FeedState:
        """What one render may show. Polling eligibility and display are separate: the same
        context's
        last page is shown, aged, while inactive or offline. Another context never shows this data:
        a render that changes the context runs before the effect that clears the feed, so this gate
        is what keeps the previous page off the screen."""
        if key != self._key:
            # Only the same account and host may show a cached page for another query.
            if scope is not None and scope == self._scope:
                cached = self._cached(key)
                if cached is not None:
                    return cached if active else replace(cached, status=STATUS_IDLE)
            return FeedState(status=STATUS_LOADING if active else STATUS_IDLE)
        return self.state


class RefreshGate:
    """Manual refresh: at most one request in flight. A press while one runs is refused, not
    queued."""

    def __init__(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def try_start(self) -> bool:
        if self._running:
            return False
        self._running = True
        return True

    def finish(self) -> None:
        self._running = False


async def run_usage_loop(
    *,
    active: Callable[[], bool],
    refresh: Callable[[], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval: float = INTERVAL_SECONDS,
    delay: Callable[[], float] | None = None,
) -> None:
    """Refresh while active. Each refresh is awaited before the wait starts, so refreshes never
    overlap. `delay`, when given, returns the wait after each refresh (the feed's backoff).
    Cancellation (the caller's task) propagates out of the awaited refresh or wait."""
    while active():
        await refresh()
        if not active():
            break
        await sleep(delay() if delay is not None else interval)
