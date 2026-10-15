"""Usage page model. Pure: wire JSON in, view values out. No Flet, no I/O.

Rules (UI plan, wire contract v1):

- A null cost is unavailable (partially unpriced), never $0.
- Reasoning sits inside output, so it is not added to the token total.
- Sessions are not additive across days or models, so they are never summed.
- Limits are current host/account quotas: they ignore the period and space filters.
- A window is a capacity signal only when its status is `fresh`. Expired, stale or missing
  observations never imply remaining capacity.
- Display names come from `kinds[].display_name` only. Nothing here names a CLI.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
SCOPES = ("workspace", "overall", "not_linked")
LIMIT_STATUSES = ("fresh", "stale", "expired", "not_reported")
COVERAGES = ("complete", "partial", "unverified", "unavailable")
# The contract LIMIT_STALE_MS: a reading older than this is stale, whatever it said.
LIMIT_STALE_MS = 15 * 60 * 1000
SETUP_STATES = (
    "not_configured",
    "script_missing",
    "script_outdated",
    "settings_missing",
    "statusline_missing",
    "conflict",
    "installed",
    "invalid",
    "unreadable",
)


class UsageDocError(ValueError):
    """A usage document that does not match the wire contract. The message names no content."""


@dataclass(frozen=True)
class Counters:
    input_uncached: int = 0
    cache_read: int = 0
    cache_write_short: int = 0
    cache_write_long: int = 0
    cache_write_unknown: int = 0
    output: int = 0
    reasoning: int = 0

    @property
    def tokens(self) -> int:
        """Every bucket that is a real token count. Reasoning is inside output, so excluded."""
        return (
            self.input_uncached
            + self.cache_read
            + self.cache_write_short
            + self.cache_write_long
            + self.cache_write_unknown
            + self.output
        )

    def plus(self, other: Counters) -> Counters:
        return Counters(
            input_uncached=self.input_uncached + other.input_uncached,
            cache_read=self.cache_read + other.cache_read,
            cache_write_short=self.cache_write_short + other.cache_write_short,
            cache_write_long=self.cache_write_long + other.cache_write_long,
            cache_write_unknown=self.cache_write_unknown + other.cache_write_unknown,
            output=self.output + other.output,
            reasoning=self.reasoning + other.reasoning,
        )


@dataclass(frozen=True)
class Row:
    day: str
    workspace_id: str | None
    kind: str
    model: str | None
    counters: Counters
    sessions: int
    cost_micro_usd: int | None
    scope: str
    price_snapshot: str | None
    # Per-event aggregates. None means UNKNOWN (an older server): never zero.
    priced_cost_micro_usd: int | None = None
    priced_tokens: int | None = None
    unpriced_tokens: int | None = None
    hour: int | None = None  # host-local hour 0-23 for hourly rows
    # link evidence behind the row's sessions (None: an older server; 0 on Not linked rows).
    linked_sessions: int | None = None
    inferred_sessions: int | None = None


@dataclass(frozen=True)
class CreditRow:
    """One credit-metered row. Credits are a separate meter: they are never part of a token
    total or a cost, so they live apart from `UsagePage.rows`."""

    day: str
    workspace_id: str | None
    kind: str
    scope: str
    credits_milli: int
    sessions: int = 0
    hour: int | None = None


@dataclass(frozen=True)
class Window:
    window_key: str
    used_percent: float
    window_minutes: int | None
    resets_at_ms: int | None
    status: str
    # An amount-metered window (a credit plan): used and limit amounts and the unit's singular
    # name ("credit"). None from an older server or for a percent-only window.
    used_amount: float | None = None
    limit_amount: float | None = None
    unit: str | None = None
    over_allowance: bool = False  # the plan's allowance is used up and exceeded


@dataclass(frozen=True)
class Limit:
    kind: str
    quota_key: str
    observed_ms: int
    status: str
    windows: tuple[Window, ...]
    # Which provider plane produced the reading ("" for a status-line or log reading, which has no
    # account identity), the opaque account identity (None: unknown) and the fetch error the
    # server attached to it. Older servers send none of these.
    source: str = ""
    account_ref: str | None = None
    error: str | None = None
    plan_label: str | None = None  # the tool's plan name ("TOOL FREE"); None: not sent


@dataclass(frozen=True)
class CoverageEntry:
    kind: str
    coverage: str
    observed_ms: int | None
    unmapped_sessions: int
    # exact-linked and inferred-linked sessions, disjoint from unmapped. None: the server did
    # not
    # send the field (an older server). Never shown as zero.
    linked_sessions: int | None = None
    inferred_sessions: int | None = None


@dataclass(frozen=True)
class LimitsSetup:
    """Installation state of a managed limits hook (addendum D). Independent of limit freshness."""

    state: str
    checked_ms: int | None


@dataclass(frozen=True)
class SourceSetup:
    """One configured source of a kind: its format and its own limits-hook state."""

    format: str
    setup: LimitsSetup | None = None


@dataclass(frozen=True)
class RefreshAttempt:
    """The last on-demand read the companion finished: its id, when, and `ok` or an error code."""

    id: str
    finished_ms: int | None
    result: str


@dataclass(frozen=True)
class LimitsRefresh:
    """Whether the companion can read this tool's limits on demand (an on-demand `/usage` read),
    and where that read stands. Shared by every device: the server owns the cooldown."""

    available: bool
    reason: str | None = None  # why not available ("no_space": no Herdr space for the tool yet)
    in_progress: bool = False
    cooldown_until_ms: int | None = None
    last_error: str | None = None  # timeout | auth_required | parse | ...
    last_attempt: RefreshAttempt | None = None


@dataclass(frozen=True)
class Kind:
    kind: str
    display_name: str
    tokens_reported: bool
    setup: LimitsSetup | None = None  # None: no managed limits hook (unmanaged, not broken)
    sources: tuple[SourceSetup, ...] = ()  # empty for an older server
    credits_reported: bool = False  # the tool is metered in credits; False: older server
    refresh: LimitsRefresh | None = None  # None: an older server (no refresh button)


@dataclass(frozen=True)
class UsagePage:
    host_id: str
    generated_ms: int
    timezone: str
    price_table_date: str | None
    next_cursor: str | None
    rows: tuple[Row, ...]
    limits: tuple[Limit, ...]
    coverage: tuple[CoverageEntry, ...]
    snapshot_labels: tuple[tuple[str, str], ...]
    kinds: tuple[Kind, ...]
    # (workspace_id, label); the label falls back to the id
    workspaces: tuple[tuple[str, str], ...] = ()
    # Credit-metered rows, apart from `rows`. Empty from an older server.
    credits: tuple[CreditRow, ...] = ()
    # (workspace_id, last_used_day or None): None means no row in this report, never a guess
    workspace_days: tuple[tuple[str, str | None], ...] = ()
    # "ok", or "exceeded": the report was over the server cap and carries no rows
    capacity: str = "ok"


# -- parsing -------------------------------------------------------------------------------------


def _obj(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise UsageDocError("object expected")
    return value


def _arr(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise UsageDocError("array expected")
    return value


def _int(value: object, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageDocError("non-negative integer expected")
    return value


def _str(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise UsageDocError("string expected")
    return value


def _day(value: object) -> str | None:
    """A calendar day (YYYY-MM-DD), or None when absent or invalid: unknown, never a guessed
    date."""
    if not isinstance(value, str):
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _choice(value: object, allowed: tuple[str, ...]) -> str:
    text = _str(value)
    if text not in allowed:
        raise UsageDocError("unknown enum value")
    return text or ""


def _float_percent(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UsageDocError("percentage expected")
    if not 0 <= float(value) <= 100:
        raise UsageDocError("percentage out of range")
    return float(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise UsageDocError("boolean expected")
    return value


def _counters(value: object) -> Counters:
    doc = _obj(value)
    return Counters(
        input_uncached=_int(doc.get("input_uncached")) or 0,
        cache_read=_int(doc.get("cache_read")) or 0,
        cache_write_short=_int(doc.get("cache_write_short")) or 0,
        cache_write_long=_int(doc.get("cache_write_long")) or 0,
        cache_write_unknown=_int(doc.get("cache_write_unknown")) or 0,
        output=_int(doc.get("output")) or 0,
        reasoning=_int(doc.get("reasoning")) or 0,
    )


def _credit_row(value: object) -> CreditRow:
    doc = _obj(value)
    milli = _int(doc.get("credits_milli")) or 0
    if milli < 0:
        raise UsageDocError("negative credits")
    return CreditRow(
        day=_str(doc.get("day")) or "",
        workspace_id=_str(doc.get("workspace_id"), nullable=True),
        kind=_str(doc.get("kind")) or "",
        scope=_choice(doc.get("scope"), SCOPES),
        credits_milli=milli,
        sessions=_int(doc.get("sessions")) or 0,
        hour=_int(doc.get("hour"), nullable=True),
    )


def _row(value: object) -> Row:
    doc = _obj(value)
    counters = _counters(doc.get("counters"))
    cost = _int(doc.get("cost_micro_usd"), nullable=True)
    if "priced_tokens" in doc or "unpriced_tokens" in doc:
        priced_cost = _int(doc.get("priced_cost_micro_usd"), nullable=True)
        priced_tokens = _int(doc.get("priced_tokens"), nullable=True)
        unpriced_tokens = _int(doc.get("unpriced_tokens"), nullable=True)
    elif cost is not None:
        # An older server: its cost is None whenever any event is unpriced, so a number means
        # every token was priced. A None cost is unknown, not a partial split.
        priced_cost, priced_tokens, unpriced_tokens = cost, counters.tokens, 0
    else:
        priced_cost = priced_tokens = unpriced_tokens = None
    linked = _int(doc.get("linked_sessions"), nullable=True)
    inferred = _int(doc.get("inferred_sessions"), nullable=True)
    return Row(
        linked_sessions=linked,
        inferred_sessions=inferred,
        day=_str(doc.get("day")) or "",
        workspace_id=_str(doc.get("workspace_id"), nullable=True),
        kind=_str(doc.get("kind")) or "",
        model=_str(doc.get("model"), nullable=True),
        counters=counters,
        sessions=_int(doc.get("sessions")) or 0,
        cost_micro_usd=cost,
        scope=_choice(doc.get("scope"), SCOPES),
        price_snapshot=_str(doc.get("price_snapshot"), nullable=True),
        priced_cost_micro_usd=priced_cost,
        priced_tokens=priced_tokens,
        unpriced_tokens=unpriced_tokens,
        hour=_int(doc.get("hour"), nullable=True),
    )


def _amount(value: object) -> float | None:
    """An optional wire amount: a finite non-negative number, or None. A malformed amount is
    dropped (the percent still shows), never raised: these fields are additive."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if 0 <= number < float("inf") else None


def _window(value: object) -> Window:
    doc = _obj(value)
    unit = doc.get("unit")
    used_amount = _amount(doc.get("used_amount"))
    over = doc.get("over_allowance") is True
    raw = doc.get("used_percent")
    if raw is None and used_amount is not None:
        percent = 0.0  # an amount-only window (no limit to divide by)
    elif over and isinstance(raw, int | float) and not isinstance(raw, bool):
        percent = min(100.0, max(0.0, float(raw)))  # over the plan: a full bar, never out of range
    else:
        percent = _float_percent(raw)
    return Window(
        window_key=_str(doc.get("window_key")) or "",
        used_percent=percent,
        window_minutes=_int(doc.get("window_minutes"), nullable=True),
        resets_at_ms=_int(doc.get("resets_at_ms"), nullable=True),
        status=_choice(doc.get("status"), LIMIT_STATUSES),
        used_amount=used_amount,
        limit_amount=_amount(doc.get("limit_amount")),
        unit=unit.strip() if isinstance(unit, str) and unit.strip() else None,
        over_allowance=over,
    )


def _limit(value: object) -> Limit:
    doc = _obj(value)
    return Limit(
        kind=_str(doc.get("kind")) or "",
        quota_key=_str(doc.get("quota_key")) or "",
        observed_ms=_int(doc.get("observed_ms")) or 0,
        status=_choice(doc.get("status"), LIMIT_STATUSES),
        windows=tuple(_window(item) for item in _arr(doc.get("windows"))),
        source=_str(doc.get("source"), nullable=True) or "",
        account_ref=_str(doc.get("account_ref"), nullable=True) or None,
        error=_str(doc.get("error"), nullable=True) or None,
        plan_label=_label(doc.get("plan_label")),
    )


def _label(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def current_limits(limits: tuple[Limit, ...]) -> tuple[Limit, ...]:
    """The readings that may be shown as current. A kind's provider readings that name more than
    one account are ambiguous: none is shown, because a reading is never picked across accounts by
    timestamp. (The server already sends only the active account; this is the client's guard.)
    Readings with no source (status line, logs) are one account-less source and pass through."""
    accounts: dict[str, set[str | None]] = {}
    for limit in limits:
        if limit.source:
            accounts.setdefault(limit.kind, set()).add(limit.account_ref)
    ambiguous = {kind for kind, refs in accounts.items() if len(refs) > 1}
    return tuple(limit for limit in limits if not (limit.source and limit.kind in ambiguous))


def _coverage(value: object) -> CoverageEntry:
    doc = _obj(value)
    return CoverageEntry(
        kind=_str(doc.get("kind")) or "",
        coverage=_choice(doc.get("coverage"), COVERAGES),
        observed_ms=_int(doc.get("observed_ms"), nullable=True),
        unmapped_sessions=_int(doc.get("unmapped_sessions")) or 0,
        linked_sessions=_int(doc.get("linked_sessions"), nullable=True),
        inferred_sessions=_int(doc.get("inferred_sessions"), nullable=True),
    )


def _setup(value: object) -> LimitsSetup | None:
    if value is None:
        return None
    doc = _obj(value)
    return LimitsSetup(
        state=_choice(doc.get("state"), SETUP_STATES),
        checked_ms=_int(doc.get("checked_ms"), nullable=True),
    )


def _refresh(value: object) -> LimitsRefresh | None:
    """The kind's on-demand limits read. Absent or malformed means an older server: no button."""
    if not isinstance(value, Mapping):
        return None
    doc: Mapping[str, Any] = value
    available = doc.get("available")
    if not isinstance(available, bool):
        return None
    cooldown = doc.get("cooldown_until_ms")
    attempt = doc.get("last_attempt")
    last: RefreshAttempt | None = None
    if isinstance(attempt, Mapping):
        ident, result = attempt.get("id"), attempt.get("result")
        finished = attempt.get("finished_ms")
        if isinstance(ident, str) and ident and isinstance(result, str) and result:
            last = RefreshAttempt(
                ident,
                finished if isinstance(finished, int) and not isinstance(finished, bool) else None,
                result,
            )
    return LimitsRefresh(
        available=available,
        reason=_label(doc.get("reason")),
        in_progress=doc.get("in_progress") is True,
        cooldown_until_ms=cooldown
        if isinstance(cooldown, int) and not isinstance(cooldown, bool) and cooldown > 0
        else None,
        last_error=_label(doc.get("last_error")),
        last_attempt=last,
    )


def _kind(value: object) -> Kind:
    doc = _obj(value)
    sources = tuple(
        SourceSetup(
            format=_str(_obj(item).get("format")) or "",
            setup=_setup(_obj(item).get("limits_setup")),
        )
        for item in _arr(doc.get("sources") or [])
    )
    return Kind(
        kind=_str(doc.get("kind")) or "",
        display_name=_str(doc.get("display_name")) or "",
        tokens_reported=_bool(doc.get("tokens_reported")),
        setup=_setup(doc.get("limits_setup")),
        sources=sources,
        credits_reported=bool(doc.get("credits_reported") is True),
        refresh=_refresh(doc.get("limits_refresh")),
    )


def host_day(now_ms: int, timezone: str) -> date:
    """The host's calendar date at `now_ms` (the host's day boundary, not the device's)."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise UsageDocError("unknown timezone") from error
    return datetime.fromtimestamp(now_ms / 1000, zone).date()


def page_today(page: UsagePage) -> date:
    """The host's calendar date at the snapshot time."""
    return host_day(page.generated_ms, page.timezone)


def parse_usage_page(doc: object) -> UsagePage:
    """A validated page. Any mismatch with the contract raises UsageDocError."""
    body = _obj(doc)
    if body.get("schema_version") != SCHEMA_VERSION:
        raise UsageDocError("unsupported schema version")
    labels = []
    for item in _arr(body.get("snapshot_labels")):
        entry = _obj(item)
        labels.append((_str(entry.get("date")) or "", _str(entry.get("label")) or ""))
    workspaces = []
    workspace_days = []
    for item in _arr(body.get("workspaces")):
        entry = _obj(item)
        workspace_id = _str(entry.get("workspace_id")) or ""
        workspaces.append((workspace_id, _str(entry.get("label")) or workspace_id))
        workspace_days.append((workspace_id, _day(entry.get("last_used_day"))))
    capacity = body.get("capacity") or "ok"
    if capacity not in ("ok", "exceeded"):
        raise UsageDocError("unknown capacity value")
    # Credit rows (`meter` "credits") are kept apart so no token total, chart or cost can ever
    # include them. A row without `meter` (an older server) is a token row.
    token_items: list[object] = []
    credit_items: list[object] = []
    for item in _arr(body.get("rows")):
        meter = _obj(item).get("meter")
        (credit_items if meter == "credits" else token_items).append(item)
    return UsagePage(
        host_id=_str(body.get("host_id")) or "",
        generated_ms=_int(body.get("generated_ms")) or 0,
        timezone=_str(body.get("timezone")) or "",
        price_table_date=_str(body.get("price_table_date"), nullable=True),
        next_cursor=_str(body.get("next_cursor"), nullable=True),
        rows=tuple(_row(item) for item in token_items),
        credits=tuple(_credit_row(item) for item in credit_items),
        limits=tuple(_limit(item) for item in _arr(body.get("limits"))),
        coverage=tuple(_coverage(item) for item in _arr(body.get("coverage"))),
        snapshot_labels=tuple(labels),
        kinds=tuple(_kind(item) for item in _arr(body.get("kinds"))),
        workspaces=tuple(workspaces),
        workspace_days=tuple(workspace_days),
        capacity=capacity,
    )


# -- display names -------------------------------------------------------------------------------


def _display(page: UsagePage, kind: str) -> tuple[str, bool]:
    for entry in page.kinds:
        if entry.kind == kind:
            return entry.display_name or kind, entry.tokens_reported
    return kind, False


# -- limits --------------------------------------------------------------------------------------

LIMIT_FRESH = "fresh"


def effective_window_status(window: Window, observed_ms: int, now_ms: int) -> str:
    """The window's status at `now_ms`, not at the snapshot: a window past its reset is expired,
    and any reading older than the stale threshold is stale, whatever it said when recorded."""
    if window.status == "not_reported":
        return "not_reported"
    if window.status == "expired":
        return "expired"
    if window.resets_at_ms is not None and now_ms >= window.resets_at_ms:
        return "expired"
    if now_ms - observed_ms > LIMIT_STALE_MS:
        return "stale"
    return window.status


@dataclass(frozen=True)
class ConsolePick:
    display_name: str
    window_key: str
    used_percent: float
    resets_at_ms: int | None
    window_minutes: int | None = None  # names the window ("Weekly"), not its key


@dataclass(frozen=True)
class ConsoleCard:
    has_today: bool
    cost_micro_usd: int | None  # legacy: None unless every token today was priced
    tokens: int  # meaningful only when `available`; 0 is a placeholder otherwise
    pick: ConsolePick | None
    # Every tool's highest fresh window, ranked by used % (ties: the sooner reset first); `pick` is
    # the first. A tool appears once.
    picks: tuple[ConsolePick, ...] = ()
    # The tool with the most tokens today (display name, tokens), for when no tool has a limit
    # reading. None: no usage today.
    top_tool: tuple[str, int] | None = None
    stale_day: bool = False  # the snapshot is from an earlier host day: no "today" claim
    age_ms: int = 0  # how old the snapshot is when rendered
    priced_micro_usd: int | None = None  # the priced subtotal (None: nothing priced, or unknown)
    unpriced_tokens: int = 0  # tokens with no price today: the card shows Partial
    # False: the report was over the server cap. Then has_today is False and the totals are not
    # usage at all; a renderer must show "unavailable", never "No usage today" or $0.
    available: bool = True


def console_card(page: UsagePage, now_ms: int) -> ConsoleCard:
    """Today's overall totals (only when the snapshot is from the host's today) and the most-used
    window that is fresh at `now_ms`, across all limits: an aggregate that is expired does not hide
    a fresh sibling window. Ties go to the earliest reset. The pick carries its own reset."""
    # Imported here: the selectors module reads this module's types, so it cannot be imported
    # at the top of this file.
    from relaydeck.client.features.usage import selectors

    today = host_day(now_ms, page.timezone)
    current = page_today(page) == today
    available = selectors.report_available(page)
    day_rows: tuple[Row, ...] = ()
    if current and available:
        day_rows = selectors.filtered_rows(
            page,
            selectors.Filter(period="today", space=selectors.ALL, provider=selectors.ALL),
            today,
        )
    usage = selectors.usage_of(day_rows)
    priced = usage.cost.state in ("zero", "priced") and available
    best_by_tool: dict[str, ConsolePick] = {}
    for limit in current_limits(page.limits):
        for window in limit.windows:
            if effective_window_status(window, limit.observed_ms, now_ms) != LIMIT_FRESH:
                continue  # a stale or ended reading is never ranked
            candidate = ConsolePick(
                display_name=_display(page, limit.kind)[0],
                window_key=window.window_key,
                used_percent=window.used_percent,
                resets_at_ms=window.resets_at_ms,
                window_minutes=window.window_minutes,
            )
            held = best_by_tool.get(limit.kind)
            if held is None or _beats(candidate, held):
                best_by_tool[limit.kind] = candidate
    picks = [best_by_tool[key] for key in best_by_tool]
    ranked: list[ConsolePick] = []
    for candidate in picks:
        index = 0
        while index < len(ranked) and not _beats(candidate, ranked[index]):
            index += 1
        ranked.insert(index, candidate)
    best = ranked[0] if ranked else None
    by_kind: dict[str, int] = {}
    for row in day_rows:
        if _display(page, row.kind)[1]:  # a tool that reports tokens
            by_kind[row.kind] = by_kind.get(row.kind, 0) + row.counters.tokens
    top_tool: tuple[str, int] | None = None
    if by_kind:
        top_kind = max(sorted(by_kind), key=lambda kind: by_kind[kind])
        if by_kind[top_kind] > 0:
            top_tool = (_display(page, top_kind)[0], by_kind[top_kind])
    return ConsoleCard(
        has_today=bool(day_rows),
        stale_day=not current,
        cost_micro_usd=usage.cost.priced_micro_usd if priced else None,
        tokens=(usage.tokens or 0) if available else 0,  # 0 is a placeholder when unavailable
        pick=best,
        picks=tuple(ranked),
        top_tool=top_tool,
        age_ms=max(0, now_ms - page.generated_ms),
        priced_micro_usd=usage.cost.amount_micro_usd if available else None,
        unpriced_tokens=(usage.cost.unpriced_tokens or 0) if available else 0,
        available=available,
    )


def _beats(candidate: ConsolePick, best: ConsolePick) -> bool:
    if candidate.used_percent != best.used_percent:
        return candidate.used_percent > best.used_percent
    left = candidate.resets_at_ms if candidate.resets_at_ms is not None else float("inf")
    right = best.resets_at_ms if best.resets_at_ms is not None else float("inf")
    return left < right
