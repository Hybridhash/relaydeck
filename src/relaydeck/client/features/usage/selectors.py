"""Usage selectors. Pure: parsed page in, values out. No Flet, no
I/O.

- One total selector for every screen: all spaces = the `overall` rows plus the `not_linked` rows,
  each row counted once. A selected space = its `workspace` rows. "Not linked" = its `not_linked`
  rows.
- Cost is never inferred client-side. The server's per-event priced and unpriced token counts decide
  zero, priced, partial and all-unpriced. An older server (no per-event fields) gives UNKNOWN.
- Limits ignore the period and the space, but respect the provider filter. A provider's windows come
  only from its latest reading, and are labelled by duration.
- Formatting follows. Every name shown comes from the page's display names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage.model import Row, UsagePage

ALL = "all"
UNLINKED = "unlinked"
PERIOD_DAYS: dict[str, int] = {"today": 1, "7d": 7, "30d": 30, "90d": 90}
RANK_COMPACT = 4  #: top N at compact width
RANK_REGULAR = 8  # top N at regular width
NEAR_LIMIT_PERCENT = 80
COMPACT_ATTENTION = 3
SUPPORTED_SORTS = ("most_tokens", "a_z", "recent")

# Provider status per.3 and the usage review. Blocking first, then stale, then near-limit.
STATUS_UNMANAGED = "unmanaged"  # no managed limits hook: not broken, not an attention item
STATUS_NOT_REPORTED = "not_reported"  # the format has no verified limit source
STATUS_SETUP_NEEDED = "setup_needed"
STATUS_CONFLICT = "conflict"
STATUS_NO_READING = "installed_no_reading"
STATUS_READING_UNAVAILABLE = "reading_unavailable"  # the last fetch failed; no reading is shown
STATUS_STALE = "stale"
STATUS_WINDOW_ENDED = "window_ended"
STATUS_NEAR_LIMIT = "near_limit"
STATUS_OK = "ok"
ATTENTION_ORDER: dict[str, int] = {
    STATUS_SETUP_NEEDED: 0,
    STATUS_CONFLICT: 0,
    STATUS_NO_READING: 1,
    STATUS_READING_UNAVAILABLE: 1,
    STATUS_STALE: 1,
    STATUS_WINDOW_ENDED: 1,
    STATUS_NEAR_LIMIT: 2,
}
_SETUP_NEEDED = {
    "script_missing",
    "settings_missing",
    "statusline_missing",
    "script_outdated",
    "invalid",
    "unreadable",
}


@dataclass(frozen=True)
class Filter:
    period: str = "7d"  # today | 7d | 30d | 90d
    space: str = ALL  # all | unlinked | a workspace id
    provider: str = ALL  # all | a kind


def _check(flt: Filter) -> None:
    if flt.period not in PERIOD_DAYS:
        raise ValueError("unknown period")


def window_bounds(period: str, today: date) -> tuple[date, date]:
    """The inclusive day range of a period ending on `today` (the host's day)."""
    return today - timedelta(days=PERIOD_DAYS[period] - 1), today


def _in_period(page: UsagePage, flt: Filter, today: date) -> tuple[Row, ...]:
    """Every scope's rows for the period and the provider. The space filter is applied by
    callers."""
    _check(flt)
    first, last = window_bounds(flt.period, today)
    selected = []
    for row in page.rows:
        try:
            day = date.fromisoformat(row.day)
        except ValueError:
            continue
        if first <= day <= last and (flt.provider == ALL or row.kind == flt.provider):
            selected.append(row)
    return tuple(selected)


def filtered_rows(page: UsagePage, flt: Filter, today: date) -> tuple[Row, ...]:
    """Period, then space, then provider. All spaces is overall plus not-linked, each row once; a
    space is its workspace rows; not-linked is its own rows. Limits are not computed here."""
    selected = []
    for row in _in_period(page, flt, today):
        if flt.space == ALL:
            if row.scope in ("overall", "not_linked"):
                selected.append(row)
        elif flt.space == UNLINKED:
            if row.scope == "not_linked":
                selected.append(row)
        elif row.scope == "workspace" and (row.workspace_id or "") == flt.space:
            selected.append(row)
    return tuple(selected)


# -- credits ------------------------------------------------------------------------------
# Credits are a separate meter. They are summed apart and never enter a token total, a chart of
# tokens or a cost.


def credits_text(milli: int) -> str:
    """`162.2`, with thousands separators and one decimal place (credits_milli is thousandths)."""
    value = (Decimal(milli) / 1000).quantize(Decimal("0.1"), ROUND_HALF_UP)
    return f"{value:,.1f}"


def credits_chart_text(milli: int) -> str:
    """The short chart form: `162.2`, `1,234.5`, `12.3K`."""
    if milli >= 10_000_000:
        return _fixed(milli / 1_000_000, 1) + "K"
    return credits_text(milli)


def _credit_rows_in(page: UsagePage, flt: Filter, today: date) -> list[model.CreditRow]:
    _check(flt)
    first, last = window_bounds(flt.period, today)
    out = []
    for row in page.credits:
        try:
            day = date.fromisoformat(row.day)
        except ValueError:
            continue
        if first <= day <= last and (flt.provider == ALL or row.kind == flt.provider):
            out.append(row)
    return out


def credit_rows(page: UsagePage, flt: Filter, today: date) -> tuple[model.CreditRow, ...]:
    """Period, space, provider: the same selection rule as `filtered_rows`, for credit rows."""
    selected = []
    for row in _credit_rows_in(page, flt, today):
        if flt.space == ALL:
            if row.scope in ("overall", "not_linked"):
                selected.append(row)
        elif flt.space == UNLINKED:
            if row.scope == "not_linked":
                selected.append(row)
        elif row.scope == "workspace" and (row.workspace_id or "") == flt.space:
            selected.append(row)
    return tuple(selected)


def credits_by_kind(page: UsagePage, flt: Filter, today: date) -> dict[str, int]:
    """Credits (thousandths) per tool for the selection. Empty from an older server."""
    out: dict[str, int] = {}
    for row in credit_rows(page, flt, today):
        out[row.kind] = out.get(row.kind, 0) + row.credits_milli
    return out


def credit_space_buckets(page: UsagePage, flt: Filter, today: date) -> dict[str, int]:
    """Credits per space (workspace id or `unlinked`), the space filter ignored."""
    out: dict[str, int] = {}
    for row in _credit_rows_in(page, flt, today):
        if row.scope == "not_linked":
            key = UNLINKED
        elif row.scope == "workspace" and row.workspace_id:
            key = row.workspace_id
        else:
            continue
        out[key] = out.get(key, 0) + row.credits_milli
    return out


def credit_chart_page(page: UsagePage) -> UsagePage:
    """A page whose rows are the credit rows, each carrying its credits (thousandths) where tokens
    go, so the chart builder can draw credits per day. It is used only for the credits chart and is
    never shown as tokens."""
    rows = tuple(
        model.Row(
            day=row.day,
            workspace_id=row.workspace_id,
            kind=row.kind,
            model=None,
            counters=model.Counters(input_uncached=row.credits_milli),
            sessions=row.sessions,
            cost_micro_usd=None,
            scope=row.scope,
            price_snapshot=None,
            priced_cost_micro_usd=0,
            priced_tokens=0,
            unpriced_tokens=0,
            hour=row.hour,
        )
        for row in page.credits
    )
    return replace(page, rows=rows)


# -- cost ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cost:
    """Priced and unpriced parts of a selection. All None: unknown (an older server)."""

    priced_micro_usd: int | None
    priced_tokens: int | None
    unpriced_tokens: int | None

    @property
    def state(self) -> str:
        """zero | priced | partial | unpriced | unknown."""
        if self.priced_tokens is None or self.unpriced_tokens is None:
            return "unknown"
        if self.priced_tokens == 0 and self.unpriced_tokens == 0:
            return "zero"
        if self.priced_tokens == 0:
            return "unpriced"
        if self.unpriced_tokens == 0:
            return "priced"
        return "partial"

    @property
    def amount_micro_usd(self) -> int | None:
        """The headline: the priced subtotal. None when nothing is priced (shown as an em dash)."""
        if self.state in ("zero", "priced", "partial"):
            return self.priced_micro_usd
        return None


UNKNOWN_COST = Cost(None, None, None)


def _cost_of(rows: Sequence[Row]) -> Cost:
    micro = priced = unpriced = 0
    for row in rows:
        if (
            row.priced_cost_micro_usd is None
            or row.priced_tokens is None
            or row.unpriced_tokens is None
        ):
            return UNKNOWN_COST
        micro += row.priced_cost_micro_usd
        priced += row.priced_tokens
        unpriced += row.unpriced_tokens
    return Cost(micro, priced, unpriced)


def _add_costs(costs: Sequence[Cost]) -> Cost:
    if any(cost.state == "unknown" for cost in costs):
        return UNKNOWN_COST
    return Cost(
        sum(c.priced_micro_usd or 0 for c in costs),
        sum(c.priced_tokens or 0 for c in costs),
        sum(c.unpriced_tokens or 0 for c in costs),
    )


@dataclass(frozen=True)
class Usage:
    """Tokens and cost of a selection. `tokens` is None when the report is unavailable (over the
    server cap): that is not zero, and no screen may show it as zero."""

    tokens: int | None
    cost: Cost

    @property
    def available(self) -> bool:
        return self.tokens is not None


UNAVAILABLE_USAGE = Usage(tokens=None, cost=UNKNOWN_COST)


def report_available(page: UsagePage) -> bool:
    """False when the report was over the server cap: its rows are absent, not zero."""
    return page.capacity == "ok"


def usage_of(rows: Sequence[Row]) -> Usage:
    """Tokens and cost of already selected rows. Reasoning is inside output, so it is not added."""
    tokens = sum(row.counters.tokens for row in rows)
    return Usage(tokens=tokens, cost=_cost_of(rows))


def totals(page: UsagePage, flt: Filter, today: date) -> Usage:
    """The one total for a filter. An over-capacity report is unavailable, never zero."""
    if not report_available(page):
        return UNAVAILABLE_USAGE
    return usage_of(filtered_rows(page, flt, today))


def _sum_usage(values: Sequence[Usage]) -> Usage:
    if any(v.tokens is None for v in values):
        return UNAVAILABLE_USAGE
    return Usage(
        tokens=sum(v.tokens or 0 for v in values),
        cost=_add_costs([v.cost for v in values]),
    )


def space_buckets(page: UsagePage, flt: Filter, today: date) -> dict[str, Usage]:
    """Usage per space for the filter's period and provider: one entry per workspace id, plus
    "unlinked". The space filter is ignored here, so the buckets partition the all-spaces total.
    An over-capacity report has no buckets at all (it is unavailable, not empty)."""
    if not report_available(page):
        return {}
    buckets: dict[str, list[Row]] = {}
    for row in _in_period(page, flt, today):
        # Workspace rows and not-linked rows partition the total. Overall rows are not added again.
        if row.scope == "not_linked":
            buckets.setdefault(UNLINKED, []).append(row)
        elif row.scope == "workspace" and row.workspace_id:
            buckets.setdefault(row.workspace_id, []).append(row)
    return {key: usage_of(value) for key, value in buckets.items()}


# -- ranking -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    key: str
    name: str
    usage: Usage


@dataclass(frozen=True)
class Rollup:
    count: int
    usage: Usage


@dataclass(frozen=True)
class Ranked:
    top: tuple[Bucket, ...]
    other: Rollup | None
    peak: int  # the largest value in the list: a share bar is value / peak


def rank(items: Sequence[Bucket], n: int) -> Ranked:
    """Top N by tokens (ties by name A-Z), zero values dropped. No rollup when count <= N + 1,
    so a rollup never stands for exactly one item."""
    if n < 1:
        raise ValueError("rank needs a positive size")
    ordered = sorted(
        (item for item in items if (item.usage.tokens or 0) > 0),
        key=lambda item: (-(item.usage.tokens or 0), item.name.casefold(), item.key),
    )
    peak = (ordered[0].usage.tokens or 0) if ordered else 0
    if len(ordered) <= n + 1:
        return Ranked(top=tuple(ordered), other=None, peak=peak)
    rest = ordered[n:]
    other = Rollup(count=len(rest), usage=_sum_usage([item.usage for item in rest]))
    return Ranked(top=tuple(ordered[:n]), other=other, peak=peak)


def share(value: int, peak: int) -> float:
    """Share bar fraction: value / peak, the top item is 1.0. A positive value is at least 3 px
    wide,
    which is the caller's job; this only returns the fraction."""
    if peak <= 0 or value <= 0:
        return 0.0
    return min(1.0, value / peak)


# -- providers and limits ------------------------------------------------------------------------


def latest_limit(page: UsagePage, kind: str) -> model.Limit | None:
    """The kind's latest reading only. Its windows are the only windows shown for the kind: an older
    reading's windows (for example a stale secondary) are never shown beside it."""
    readings = [limit for limit in model.current_limits(page.limits) if limit.kind == kind]
    if not readings:
        return None
    # Readings of one kind that name different accounts were removed above, so this picks among
    # one account's quotas (or the account-less status-line source), never across accounts.
    return max(readings, key=lambda limit: limit.observed_ms)


def window_label(minutes: int | None) -> str:
    """A label from the window's duration, never from its position (10080 min -> Weekly)."""
    if minutes is None:
        return "Window"
    if minutes == 10080:
        return "Weekly"
    if minutes == 1440:
        return "Daily"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-min"


def setup_states(kind: model.Kind) -> tuple[str, ...]:
    """The setup state of every configured source of the kind. A kind with no `sources` list (an
    older server) falls back to its single legacy `limits_setup` field."""
    if kind.sources:
        return tuple(src.setup.state for src in kind.sources if src.setup is not None)
    return (kind.setup.state,) if kind.setup is not None else ()


def _setup_status(kind: model.Kind, limit: model.Limit | None) -> str | None:
    """The most actionable state across all sources, independent of their order: a conflict wins
    over
    a missing or unreadable hook, which wins over a reading that is still to come."""
    states = setup_states(kind)
    if "conflict" in states:
        return STATUS_CONFLICT
    if any(state in _SETUP_NEEDED for state in states):
        return STATUS_SETUP_NEEDED
    if limit is None and "installed" in states:
        return STATUS_NO_READING
    return None


def _blocking_setup_state(kind: model.Kind) -> str | None:
    """The setup state that makes the provider need attention, most specific first."""
    states = setup_states(kind)
    for state in ("conflict", "statusline_missing", "script_missing", "script_outdated"):
        if state in states:
            return state
    return next((state for state in states if state in _SETUP_NEEDED), None)


def provider_status(kind: model.Kind, limit: model.Limit | None, now_ms: int) -> str:
    """Status of one connected kind at `now_ms`. Setup evidence comes from `limits_setup` only: the
    absence of a reading never proves that setup is needed."""
    blocking = _setup_status(kind, limit)
    if blocking is not None:
        return blocking
    if limit is None:
        return STATUS_UNMANAGED
    if limit.error is not None:
        return STATUS_READING_UNAVAILABLE
    if limit.status == "not_reported":
        return STATUS_NOT_REPORTED
    if limit.status == "stale" or now_ms - limit.observed_ms > model.LIMIT_STALE_MS:
        return STATUS_STALE
    windows = [model.effective_window_status(w, limit.observed_ms, now_ms) for w in limit.windows]
    if limit.status == "expired" or "expired" in windows:
        return STATUS_WINDOW_ENDED
    if any(
        status == "fresh" and w.used_percent >= NEAR_LIMIT_PERCENT
        for status, w in zip(windows, limit.windows, strict=True)
    ):
        return STATUS_NEAR_LIMIT
    return STATUS_OK


@dataclass(frozen=True)
class ProviderState:
    kind: str
    name: str
    status: str
    usage: Usage | None = None  # the kind's usage for the filter; None when no filter was given


def providers(page: UsagePage, now_ms: int, flt: Filter | None = None) -> tuple[ProviderState, ...]:
    """Every connected kind (configured in the page) with its status, and with its usage for the
    filter when one is given. Respects the provider filter."""
    provider = flt.provider if flt is not None else ALL
    today = model.host_day(now_ms, page.timezone)
    out = []
    for kind in page.kinds:
        if provider != ALL and kind.kind != provider:
            continue
        usage = None
        if flt is not None:
            usage = totals(page, replace(flt, provider=kind.kind), today)
        out.append(
            ProviderState(
                kind=kind.kind,
                name=kind.display_name or kind.kind,
                status=provider_status(kind, latest_limit(page, kind.kind), now_ms),
                usage=usage,
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class Attention:
    items: tuple[ProviderState, ...]  # at most `cap` when capped
    more: int  # how many were left out by the cap


def attention(
    page: UsagePage, now_ms: int, *, compact: bool, flt: Filter | None = None
) -> Attention:
    """Providers needing attention, blocking first, then stale, then near-limit."""
    found = [p for p in providers(page, now_ms, flt) if p.status in ATTENTION_ORDER]
    found.sort(key=lambda p: (ATTENTION_ORDER[p.status], p.name.casefold(), p.kind))
    cap = COMPACT_ATTENTION if compact else len(found)
    return Attention(items=tuple(found[:cap]), more=max(0, len(found) - cap))


@dataclass(frozen=True)
class LimitWindow:
    key: str
    label: str
    used_percent: float
    resets_at_ms: int | None
    status: str  # effective status at now: fresh | stale | expired | not_reported
    used_amount: float | None = None  # a credit plan: used and limit amounts, and the unit
    limit_amount: float | None = None
    unit: str | None = None
    over_allowance: bool = False


@dataclass(frozen=True)
class ProviderLimits:
    kind: str
    name: str
    status: str
    observed_ms: int | None
    windows: tuple[LimitWindow, ...]
    plan_label: str | None = None
    refresh: model.LimitsRefresh | None = None  # the kind's on-demand limits read (None: older)
    setup_state: str | None = None  # the hook's setup evidence that blocked the status, if any


def limits(page: UsagePage, now_ms: int, flt: Filter | None = None) -> tuple[ProviderLimits, ...]:
    """Plan limits per provider. The period and the space are ignored; the provider filter
    applies."""
    provider = flt.provider if flt is not None else ALL
    out = []
    for kind in page.kinds:
        if provider != ALL and kind.kind != provider:
            continue
        reading = latest_limit(page, kind.kind)
        windows: tuple[LimitWindow, ...] = ()
        if reading is not None:
            windows = tuple(
                LimitWindow(
                    key=window.window_key,
                    label=window_label(window.window_minutes),
                    used_percent=window.used_percent,
                    resets_at_ms=window.resets_at_ms,
                    status=model.effective_window_status(window, reading.observed_ms, now_ms),
                    used_amount=window.used_amount,
                    limit_amount=window.limit_amount,
                    unit=window.unit,
                    over_allowance=window.over_allowance,
                )
                for window in reading.windows
            )
        out.append(
            ProviderLimits(
                kind=kind.kind,
                name=kind.display_name or kind.kind,
                status=provider_status(kind, reading, now_ms),
                observed_ms=reading.observed_ms if reading is not None else None,
                windows=windows,
                plan_label=reading.plan_label if reading is not None else None,
                refresh=kind.refresh,
                setup_state=_blocking_setup_state(kind),
            )
        )
    return tuple(out)


# -- counts --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Counts:
    active_spaces: (
        int | None
    )  # linked spaces with tokens > 0 (Not linked excluded); None: unavailable
    active_providers: int | None  # kinds with tokens > 0; None: unavailable
    total_spaces: int  # every known space (the inventory, plus any space with rows)


def counts(page: UsagePage, flt: Filter, today: date) -> Counts:
    known_total = len(
        {space for space, _label in page.workspaces}
        | {row.workspace_id for row in page.rows if row.scope == "workspace" and row.workspace_id}
    )
    if not report_available(page):
        return Counts(None, None, known_total)
    period_rows = _in_period(page, flt, today)
    active_spaces = {
        row.workspace_id
        for row in period_rows
        if row.scope == "workspace" and row.workspace_id and row.counters.tokens > 0
    }
    active_providers = {
        row.kind for row in filtered_rows(page, flt, today) if row.counters.tokens > 0
    }
    return Counts(len(active_spaces), len(active_providers), known_total)


# -- chart ---------------------------------------------------------------------------------------

CHART_OTHER = "other"


@dataclass(frozen=True)
class ChartBucket:
    label: str
    start: str  # ISO day for day and week buckets; "hh" text for an hour
    tokens: tuple[int, ...]  # one value per series, in series order


@dataclass(frozen=True)
class Chart:
    grain: str  # hour | day | week
    buckets: tuple[ChartBucket, ...]
    series: tuple[str, ...]  # kind ids; "other" last when present
    series_names: tuple[str, ...]
    hourly_available: bool  # hour buckets exist only when the report carried hourly rows
    available: bool = True  # False: the report was over the cap; there are no buckets to show


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _day_label(day: date) -> str:
    return f"{day:%b} {day.day}"


def chart(page: UsagePage, flt: Filter, today: date, *, now_hour: int | None = None) -> Chart:
    """Today: 24 hourly buckets (host timezone). 7d and 30d: daily. 90d: weekly, Mon-Sun, with
    partial
    edge weeks and no day dropped. Zero stubs fill every label, and a DST-skipped hour is a zero."""
    _check(flt)
    if not report_available(page):
        grain = "hour" if flt.period == "today" else "week" if flt.period == "90d" else "day"
        return Chart(grain, (), (), (), hourly_available=False, available=False)
    rows = filtered_rows(page, replace(flt, provider=ALL), today)
    series, names = _series(page, rows, flt)
    width = len(series)
    first, _last = window_bounds(flt.period, today)
    if flt.period == "today":
        grain = "hour"
        hour_rows = [row for row in rows if row.hour is not None]
        buckets = [[0] * width for _ in range(24)]
        for row in hour_rows:
            index = _series_index(row.kind, series)
            # An hour after the host's current hour has no usage yet: it is never reported.
            if (
                index is not None
                and row.hour is not None
                and 0 <= row.hour <= 23
                and (now_hour is None or row.hour <= now_hour)
            ):
                buckets[row.hour][index] += row.counters.tokens
        labels = [f"{hour:02d}" for hour in range(24)]
        starts = [f"{hour:02d}" for hour in range(24)]
        out = tuple(
            ChartBucket(label=labels[h], start=starts[h], tokens=tuple(buckets[h]))
            for h in range(24)
        )
        return Chart(grain, out, series, names, hourly_available=bool(hour_rows))
    if flt.period in ("7d", "30d"):
        days = [first + timedelta(days=offset) for offset in range(PERIOD_DAYS[flt.period])]
        by_day = _day_index(rows, series)
        out_list = []
        for day in days:
            tokens = _day_tokens(by_day, day, width)
            out_list.append(ChartBucket(_day_label(day), day.isoformat(), tokens))
        return Chart("day", tuple(out_list), series, names, hourly_available=False)
    # 90d: week buckets, Monday to Sunday in the host's calendar; the first and last may be partial.
    by_day = _day_index(rows, series)
    out_list = []
    weeks: dict[date, list[date]] = {}
    for offset in range(PERIOD_DAYS[flt.period]):
        day = first + timedelta(days=offset)
        weeks.setdefault(_monday(day), []).append(day)
    for days in (weeks[week] for week in sorted(weeks)):
        total = [0] * width
        for day in days:
            for index, value in enumerate(_day_tokens(by_day, day, width)):
                total[index] += value
        out_list.append(ChartBucket(_day_label(days[0]), days[0].isoformat(), tuple(total)))
    return Chart("week", tuple(out_list), series, names, hourly_available=False)


def _series(
    page: UsagePage, rows: Sequence[Row], flt: Filter
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Series: the top two providers by tokens in the period, then Other. One provider filter gives
    one series and no Other."""
    if flt.provider != ALL:
        return (flt.provider,), (_kind_name(page, flt.provider),)
    totals: dict[str, int] = {}
    for row in rows:
        totals[row.kind] = totals.get(row.kind, 0) + row.counters.tokens
    ordered = sorted(
        (kind for kind, value in totals.items() if value > 0),
        key=lambda kind: (-totals[kind], _kind_name(page, kind).casefold(), kind),
    )
    top = ordered[:2]
    names = [_kind_name(page, kind) for kind in top]
    series = list(top)
    if len(ordered) > 2:
        series.append(CHART_OTHER)
        names.append("Other")
    return tuple(series), tuple(names)


def _kind_name(page: UsagePage, kind: str) -> str:
    for entry in page.kinds:
        if entry.kind == kind:
            return entry.display_name or kind
    return kind


def _series_index(kind: str, series: tuple[str, ...]) -> int | None:
    if kind in series:
        return series.index(kind)
    if CHART_OTHER in series and kind not in series:
        return series.index(CHART_OTHER)
    return None


def _day_index(rows: Sequence[Row], series: tuple[str, ...]) -> dict[str, list[int]]:
    """Tokens per series for every day that has rows, from one pass over the rows."""
    index: dict[str, list[int]] = {}
    for row in rows:
        slot = _series_index(row.kind, series)
        if slot is None:
            continue
        values = index.get(row.day)
        if values is None:
            values = index[row.day] = [0] * len(series)
        values[slot] += row.counters.tokens
    return index


def _day_tokens(by_day: dict[str, list[int]], day: date, width: int) -> tuple[int, ...]:
    return tuple(by_day.get(day.isoformat()) or [0] * width)


# -- spaces picker -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpaceEntry:
    space_id: str
    label: str
    usage: Usage
    last_used_day: str | None  # None: no row in the period's report; never invented


def spaces(
    page: UsagePage, flt: Filter, today: date, sort: str = "most_tokens"
) -> tuple[SpaceEntry, ...]:
    """Every known space (the inventory, plus any space with rows), for the period and provider.
    Not linked is excluded. Sort: most_tokens (ties A-Z), a_z, or recent (last day desc, unknown
    last)."""
    if sort not in SUPPORTED_SORTS:
        raise ValueError("unknown sort")
    buckets = space_buckets(page, flt, today)
    last_days = dict(page.workspace_days)
    labels = dict(page.workspaces)
    known = set(labels) | {key for key in buckets if key != UNLINKED}
    known.discard(UNLINKED)
    entries = [
        SpaceEntry(
            space_id=space,
            label=labels.get(space) or space,
            usage=(
                buckets.get(space, Usage(0, _cost_of(())))
                if report_available(page)
                else UNAVAILABLE_USAGE
            ),
            last_used_day=last_days.get(space),
        )
        for space in known
    ]
    if sort == "most_tokens":
        entries.sort(key=lambda e: (-(e.usage.tokens or 0), e.label.casefold(), e.space_id))
    elif sort == "a_z":
        entries.sort(key=lambda e: (e.label.casefold(), e.space_id))
    else:
        # Newest day first; an unknown day is "" and sorts last under reverse=True. The label sort
        # runs first, so equal days keep label order (sort is stable).
        entries.sort(key=lambda e: (e.label.casefold(), e.space_id))
        entries.sort(key=lambda e: e.last_used_day or "", reverse=True)
    return tuple(entries)


# -- formatting ------------------------------------------------------------------------

_UNITS = (("B", 1_000_000_000, 2), ("M", 1_000_000, 1), ("K", 1_000, 1))


def tokens_text(count: int) -> str:
    """3 significant figures; K and M with one decimal, B with two (741.0M, 9.4K, 3.34B)."""
    if count < 1000:
        return str(count)
    for suffix, size, places in _UNITS:
        if count >= size:
            value = (Decimal(count) / size).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
            if suffix != "B" and value >= 1000:
                return tokens_text(size * 1000)  # rounds up into the next unit (999.96K -> 1.0M)
            return f"{value:.{places}f}{suffix}"
    return str(count)


def tokens_chart_text(count: int) -> str:
    """Chart labels stay short: B from 100M up (0.31B, 2.02B, 12.5B), then M and K with two
    significant figures (18M, 9.4M, 940K)."""
    if count >= 100_000_000:
        value = count / 1_000_000_000
        return _fixed(value, 2 if value < 10 else 1) + "B"
    if count >= 1_000_000:
        return _sig2(count / 1_000_000) + "M"
    return _sig2(count / 1000) + "K"


def _sig2(value: float) -> str:
    """Two significant figures: one decimal below 10, none from 10 up."""
    return _fixed(value, 1 if value < 10 else 0)


def _fixed(value: float, places: int) -> str:
    quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
    return f"{quantized:.{places}f}"


def money_text(micro_usd: int | None) -> str:
    """$ with two decimals and separators (`$1,204.10`). None (nothing priced) is an em dash."""
    if micro_usd is None:
        return "—"
    dollars = (Decimal(micro_usd) / 1_000_000).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return f"${dollars:,.2f}"


def percent_text(value: float) -> str:
    """Integer percent with half-up rounding (39%)."""
    return f"{Decimal(str(value)).quantize(Decimal(1), ROUND_HALF_UP)}%"


def relative_time(now_ms: int, then_ms: int, timezone: str) -> str:
    """just now (< 1 min), N min ago, N h ago (< 24 h), otherwise Mon 06:53 in the host timezone."""
    seconds = max(0, now_ms - then_ms) // 1000
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86_400:
        return f"{seconds // 3600} h ago"
    when = datetime.fromtimestamp(then_ms / 1000, ZoneInfo(timezone))
    return f"{when:%a} {when:%H:%M}"


def reset_text(resets_at_ms: int, timezone: str) -> str:
    """`Resets Sun 06:58` (24 h clock, host timezone)."""
    when = datetime.fromtimestamp(resets_at_ms / 1000, ZoneInfo(timezone))
    return f"Resets {when:%a} {when:%H:%M}"


def date_range(first: date, last: date, today: date) -> str:
    """`Oct 6 – Oct 12`; a one-day range that is today reads `Today · Oct 6`."""
    if first == last == today:
        return f"Today · {_day_label(first)}"
    return f"{_day_label(first)} – {_day_label(last)}"


def monogram(name: str) -> str:
    """First letter of the first two words, or the first two letters of one word. A trailing CLI is
    dropped first (Alpha -> Al, Beta CLI -> Be, OpenThing -> Op). Built from the display name."""
    words = name.split()
    if len(words) > 1 and words[-1] == "CLI":
        words = words[:-1]
    if not words:
        return ""
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return words[0][:2].capitalize()


__all__ = [
    "ALL",
    "UNLINKED",
    "Bucket",
    "Chart",
    "ChartBucket",
    "Cost",
    "Counts",
    "Filter",
    "Ranked",
    "Rollup",
    "SpaceEntry",
    "Usage",
    "attention",
    "chart",
    "counts",
    "date_range",
    "filtered_rows",
    "limits",
    "monogram",
    "money_text",
    "percent_text",
    "providers",
    "rank",
    "relative_time",
    "reset_text",
    "share",
    "space_buckets",
    "spaces",
    "tokens_chart_text",
    "tokens_text",
    "usage_of",
    "window_label",
]
