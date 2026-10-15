"""Usage client API: the query, whole-report pagination and the gateway adapter.

The fetch is injected as `fetch(query) -> document`, so this module never reaches into a
transport's internals (see `source.py` for the gateway adapter). A page is parsed and validated
before it is used. Totals are published only after the final page: a partial report is never shown
as a total.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace

from relaydeck.client.features.usage.model import UsageDocError, UsagePage, parse_usage_page

MAX_PAGES = 50
MAX_DAYS = 90


class UsageHttpError(Exception):
    """A gateway refusal, carrying only a fixed safe code (usage_disabled, usage_unavailable)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class UsageFetchError(Exception):
    """The report could not be completed. `code` is a fixed, content-free identifier."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StaleUsageRequest(Exception):
    """The request was superseded (host, filter or account changed) before it completed."""


@dataclass(frozen=True)
class UsageQuery:
    """Either `days` (the host's last N days, 1..90) or an explicit inclusive range, never both.

    `hour` asks for host-local hourly buckets. It needs exactly one day, and it is part of
    the
    query's identity: an hourly page is never reused as a daily one, or the reverse.
    """

    host_id: str
    days: int | None = None
    start: str | None = None
    end: str | None = None
    space_id: str | None = None
    cursor: str | None = None
    hour: bool = False

    def __post_init__(self) -> None:
        explicit = self.start is not None or self.end is not None
        if (self.days is None) == (not explicit):
            raise ValueError("choose days or a start and end")
        if self.days is not None and not 1 <= self.days <= MAX_DAYS:
            raise ValueError("days out of range")
        if self.hour and not (self.days == 1 or (explicit and self.start == self.end)):
            raise ValueError("hourly buckets need exactly one day")

    @property
    def identity(self) -> tuple[object, ...]:
        """What makes two reports the same report. Used to keep daily and hourly results apart."""
        return (self.host_id, self.days, self.start, self.end, self.space_id, self.hour)


@dataclass(frozen=True)
class RefreshOutcome:
    """The answer to "read this tool's limits now": `started` or `in_progress` (poll the report),
    `cooldown` (try again after `retry_after_s`), `not_configured`, or `error` with a fixed
    `code` (the request itself failed)."""

    state: str
    retry_after_s: int | None = None
    code: str | None = None
    attempt_id: str | None = None  # names this read in the report's `last_attempt`


UsageFetch = Callable[[UsageQuery], Awaitable[Mapping[str, object]]]
# (host_id, kind) -> outcome. Never raises: a failed request is `RefreshOutcome("error", ...)`.
LimitsRefreshCall = Callable[[str, str], Awaitable[RefreshOutcome]]
IsCurrent = Callable[[], bool]


def _merge(pages: list[UsagePage]) -> UsagePage:
    first = pages[0]
    for page in pages[1:]:
        if (
            page.host_id != first.host_id
            or page.generated_ms != first.generated_ms
            or page.timezone != first.timezone
        ):
            raise UsageFetchError("inconsistent_pages")
    rows = tuple(row for page in pages for row in page.rows)
    credits = tuple(row for page in pages for row in page.credits)
    return replace(first, next_cursor=None, rows=rows, credits=credits)


async def fetch_usage(
    fetch: UsageFetch,
    query: UsageQuery,
    *,
    is_current: IsCurrent,
    max_pages: int = MAX_PAGES,
) -> UsagePage:
    """Every page of one report, in order, as one complete UsagePage.

    Raises StaleUsageRequest as soon as `is_current()` is false (no further request is made),
    UsageFetchError for a malformed page, a host mismatch, inconsistent pages or an unfinished
    report, and passes UsageHttpError through.
    """
    pages: list[UsagePage] = []
    cursor = query.cursor
    for _ in range(max_pages):
        if not is_current():
            raise StaleUsageRequest
        document = await fetch(replace(query, cursor=cursor))
        if not is_current():
            raise StaleUsageRequest
        try:
            page = parse_usage_page(document)
        except UsageDocError as error:
            raise UsageFetchError("malformed_page") from error
        if page.host_id != query.host_id:
            raise UsageFetchError("host_mismatch")
        if any((row.hour is not None) != query.hour for row in page.rows):
            # A daily page never answers an hourly request, and an hourly page never a daily one.
            raise UsageFetchError("bucket_mismatch")
        pages.append(page)
        if page.next_cursor is None:
            return _merge(pages)
        cursor = page.next_cursor
    raise UsageFetchError("too_many_pages")
