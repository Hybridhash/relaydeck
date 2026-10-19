"""The Usage screen's view-model. Pure: no Flet, no I/O.

Every number, ranking, rollup, state and label the screen shows is computed here, from the selectors
(`selectors.py`). The components only render these values and do no arithmetic.
Provider and space names come from the page only.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from relaydeck.client.features.usage import model
from relaydeck.client.features.usage import selectors as s
from relaydeck.client.features.usage.model import UsagePage

# Widths are classified by the layout's own width, never by the device.
WIDTH_COMPACT = "compact"  # < 600
WIDTH_MEDIUM = "medium"  # 600 to 1023
WIDTH_WIDE = "wide"  # >= 1024
COMPACT_LIMIT = 600
WIDE_LIMIT = 1024

# Screen states, expressed for the screen from the feed's state.
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_OFFLINE = "offline"  # the same context's last page, kept, with its age
STATE_CAPACITY = "capacity"  # over the server cap: an explanation, never zeros
STATE_UNAVAILABLE = "unavailable"
STATE_DISABLED = "disabled"
STATE_NO_PROVIDERS = "no_providers"

PERIODS: tuple[tuple[str, str], ...] = (
    ("today", "Today"),
    ("7d", "7d"),
    ("30d", "30d"),
    ("90d", "90d"),
)
TONE_NORMAL = "normal"
TONE_WARN = "warn"
TONE_NEUTRAL = "neutral"
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_NOT_LINKED = "Not linked"
NOT_LINKED_ID = "not_linked"  # the screen's local selection id for the Not linked group
_ESTIMATE = "Standard-tier, token-only API-equivalent estimate"


def width_class(width: float | None) -> str:
    """Compact below 600, medium up to 1023, wide from 1024. Unknown width is compact."""
    if width is None or width < COMPACT_LIMIT:
        return WIDTH_COMPACT
    return WIDTH_WIDE if width >= WIDE_LIMIT else WIDTH_MEDIUM


def top_n(width: str) -> int:
    return s.RANK_COMPACT if width == WIDTH_COMPACT else s.RANK_REGULAR


# -- copy helpers ---------------------------------------------------------------------------------


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def tokens_words(count: int) -> str:
    """The spoken form of a token count, for accessibility labels (3.22 billion tokens)."""
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.2f} billion tokens"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f} million tokens"
    if count >= 1_000:
        return f"{count / 1_000:.1f} thousand tokens"
    return f"{count} tokens"


def money(micro: int | None) -> str:
    """`$543.85`, `$1,204.10`. None is an em dash (nothing priced to show)."""
    if micro is None:
        return "—"
    return f"${micro / 1_000_000:,.2f}"


def _weekday(day: date) -> str:
    return day.strftime("%a")


def _short_day(day: date) -> str:
    return f"{day:%b} {day.day}"


def _relative(now_ms: int, then_ms: int, tz: str) -> str:
    return s.relative_time(now_ms, then_ms, tz)


def zone_label(zone_name: str) -> str:
    """The host's zone for people: the IANA name's last segment with underscores as spaces plus
    "time" (`Asia/Riyadh` -> "Riyadh time", `America/New_York` -> "New York time"). A name with no
    city (`UTC`, `Etc/GMT+3`) is said as it is."""
    last = zone_name.rsplit("/", 1)[-1].replace("_", " ")
    if "/" not in zone_name or zone_name.startswith("Etc/"):
        return last
    return f"{last} time"


def _as_of(page: UsagePage) -> str | None:
    if page.as_of_ms is None:
        return None
    when = datetime.fromtimestamp(page.as_of_ms / 1000, ZoneInfo(page.timezone))
    return f"as of {when:%d %b %H:%M:%S} {zone_label(page.timezone)}"


def _reset(resets_at_ms: int, tz: str) -> str:
    """The reset moment alone, as `Sun 06:58`; callers add the word 'Resets' when needed."""
    when = datetime.fromtimestamp(resets_at_ms / 1000, ZoneInfo(tz))
    return f"{when:%a} {when:%H:%M}"


# -- models ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RowModel:
    """One ranked row: a space or a provider. `share` is value / peak, from 0 to 1."""

    key: str
    name: str
    value: str
    share: float
    subline: str | None
    unpriced: bool
    italic: bool  # the synthetic Not linked group
    monogram: str
    series: str | None  # a series colour role for the bar, or None for the default
    honest: str | None  # the honest status shown in place of a number (never 0 tokens)
    a11y: str
    amount: str = ""  # the cost amount alone, so the table can show it without the partial suffix
    partial: bool = False  # priced in part: the table marks it with a Partial badge
    # the provider's link line, the "includes inferred" marker, and a row's
    # note.
    links: str | None = None
    inferred: bool = False
    note: str | None = None


@dataclass(frozen=True)
class RollupModel:
    text: str
    value: str
    count: int


@dataclass(frozen=True)
class RankedModel:
    rows: tuple[RowModel, ...]
    rollup: RollupModel | None
    total: int  # the entries with a value, before the rollup
    overflow: tuple[
        str, ...
    ] = ()  # names of status-only rows beyond the bound (accessible overflow)


@dataclass(frozen=True)
class SummaryModel:
    tokens: str
    context: str
    cost: str  # money text, or an em dash
    cost_caption: str | None
    partial: bool
    partial_note: str | None
    busiest_value: str
    busiest_caption: str
    # credits are a separate meter, shown as their own small line ("+ 162.2 credits (Tool-a)")
    # and never added into the tokens or the cost. "" when no tool reports credits.
    credits_line: str = ""


@dataclass(frozen=True)
class AttentionItem:
    key: str
    name: str
    monogram: str
    reason: str
    tone: str
    action: str | None  # "setup", "refresh", or None
    action_label: str | None


@dataclass(frozen=True)
class AttentionModel:
    items: tuple[AttentionItem, ...]
    more: int


@dataclass(frozen=True)
class SegmentModel:
    role: str  # series_1 | series_2 | series_other
    fraction: float  # of the bar's full height, from 0 to 1


@dataclass(frozen=True)
class BarModel:
    label: str  # the x-axis label (two lines allowed)
    value: str  # the value shown above the bar: the peak only
    segments: tuple[SegmentModel, ...]
    stub: bool  # no usage in this bucket: a baseline stub
    peak: bool
    tooltip: str
    future: bool = False  # an hour after the host's current hour: no usage yet
    now: bool = False  # the host's current hour: the "now" marker


@dataclass(frozen=True)
class ChartModel:
    available: bool
    meta: str  # "Peak 2.02B · Thu"
    summary: str  # the chart's accessible summary
    bars: tuple[BarModel, ...]
    legend: tuple[tuple[str, str], ...]  # (series role, name); empty when there is one series
    axis_max: str
    axis_mid: str
    hourly_missing: bool
    grain: str = "day"
    title: str = ""  # "" keeps the panel's tokens title; "Daily credits" for a credit tool


@dataclass(frozen=True)
class WindowModel:
    label: str
    percent: str  # "39%", or an em dash for an ended window
    fraction: float
    role: str  # series_1 | warn | danger | neutral
    resets: str
    ended: bool
    # A credit window: "0.15 of 50 credits", shown in place of the window's name; "" for percent.
    amount: str = ""
    bar: bool = True  # False: an amount with no limit, so no bar and no percentage


# The on-demand limits read (a refresh button on a tool's Plan limits row).
REFRESH_RUNNING = "running"
REFRESH_DONE = "done"
REFRESH_FAILED = "failed"
REFRESH_IDLE = "idle"  # nothing to show for it any more
REFRESH_COOLDOWN = "cooldown"  # the server refused for now; `until_ms` says until when
REFRESH_DONE_SHOWN_MS = 60_000
REFRESH_WINDOW_MS = 40_000  # how long a started read is waited for
REFRESH_POLL_SECONDS = (2.0, 4.0, 8.0, 8.0, 8.0, 8.0)
_REFRESH_ERRORS = {
    "timeout": "couldn't read it, try again",
    "auth_required": "sign in to the tool first",
    "parse": "unexpected screen",
    "parse_no_window": "unexpected screen",
    "not_reported": "the tool isn't reporting limits right now",
    "not_configured": "not set up on this Mac yet",
    "needs_attention": "needs attention on the Mac: finish its start-up or sign-in, then refresh",
    "exited_before_prompt": "the tool closed before it was ready",
    "not_shell_at_start": "couldn't start a clean reading space",
    "no_usage_marker": "the usage panel didn't open",
}
_REFRESH_ERROR_FALLBACK = "couldn't read it, try again"
NO_SPACE_HINT = "open it in a Herdr space once"


@dataclass(frozen=True)
class RefreshLocal:
    """What this device knows about a refresh it started: running, done or failed (with the
    fixed error code), and a cooldown the server answered with. Kept per tool by the panel."""

    phase: str
    error: str | None = None
    until_ms: int | None = None  # a cooldown end learnt from a 429 answer
    at_ms: int = 0  # when the phase was set
    base_ms: int | None = None  # the reading's observed time when the read was started
    seen_busy: bool = False  # the server reported the read in progress at least once
    attempt_id: str | None = None  # the POST's attempt id, matched against `last_attempt`


def settle_refresh(
    local: RefreshLocal, info: model.LimitsRefresh | None, observed_ms: int | None, now_ms: int
) -> RefreshLocal:
    """Where a started read stands after a poll. Success needs the report's `last_attempt` to be
    this read's attempt (same id), `ok`, and a reading newer than before: `in_progress` ending is
    never success. A matching failed attempt is a failure named by its result. After the wait
    (about 40 s) a read still in progress keeps "still checking…" (the next normal report
    reconciles); one that ended unmatched is a failure, or dropped when a newer reading did
    arrive some other way."""
    if local.phase != REFRESH_RUNNING:
        return local
    attempt = info.last_attempt if info is not None else None
    mine = attempt is not None and local.attempt_id is not None and attempt.id == local.attempt_id
    newer = observed_ms is not None and (local.base_ms is None or observed_ms > local.base_ms)
    if attempt is not None and mine:
        if attempt.result != "ok":
            return RefreshLocal(REFRESH_FAILED, error=attempt.result, at_ms=now_ms)
        if newer:
            return RefreshLocal(REFRESH_DONE, at_ms=now_ms)
    busy = info is not None and info.in_progress
    seen = local.seen_busy or busy
    if now_ms - local.at_ms >= REFRESH_WINDOW_MS and not busy:
        if newer:
            return RefreshLocal(REFRESH_IDLE, at_ms=now_ms)
        error = (info.last_error if info is not None else None) or "timeout"
        return RefreshLocal(REFRESH_FAILED, error=error, at_ms=now_ms)
    return replace(local, seen_busy=seen) if seen != local.seen_busy else local


@dataclass(frozen=True)
class RefreshModel:
    """The small refresh button of one Plan limits row and the line beside it."""

    kind: str
    label: str  # the accessible name: "Refresh <tool> limits"
    busy: bool  # a spinner instead of the icon
    enabled: bool
    note: str  # "checked just now", "next refresh in 3 min", a short failure; "" for none
    # Whole minutes until the server allows the next read (0: under a minute); None: not paused.
    cooldown_minutes: int | None = None


@dataclass(frozen=True)
class LimitRowModel:
    key: str
    name: str
    monogram: str
    subline: str
    windows: tuple[WindowModel, ...]
    status: str
    refresh: RefreshModel | None = None


@dataclass(frozen=True)
class CommandModel:
    kind: str
    lines: tuple[str, ...]
    outdated: bool = False


@dataclass(frozen=True)
class ScreenModel:
    state: str
    message: str  # the state's explanation when there is no data; empty otherwise
    meta: str
    meta_tone: str
    period: str
    period_options: tuple[tuple[str, str, bool], ...]
    range_text: str
    space_value: str
    space_active: bool
    space_label: str
    provider_value: str
    provider_active: bool
    summary: SummaryModel | None
    attention: AttentionModel
    attention_all: AttentionModel  # every item, for the temporary list behind "+k more"
    chart: ChartModel | None
    plan: tuple[LimitRowModel, ...]
    spaces: RankedModel | None  # None: the space filter is active, so the list is hidden
    space_total: int  # every row the spaces list holds, Not linked included (the "See all" count)
    spaces_more: bool  # True when the list hides some rows: only then is there a "See all" link
    providers: RankedModel | None  # None: hidden (one provider, or a provider filter)
    provider_zero: str | None
    empty: bool  # the period has no usage at all
    footnote: str
    space_options: tuple[tuple[str, str, str], ...]  # (space id, label, tokens text)
    provider_options: tuple[tuple[str, str, str], ...]  # (kind, name, tokens text)
    commands: tuple[CommandModel, ...]
    space_rows: tuple[SpaceRow, ...] = ()  # every space for the Space picker, period totals
    connected: tuple[ProviderLine, ...] = ()  # every connected CLI for the Providers screen
    # names the tools left out of the token totals; "" when none is excluded.
    caveat: str = ""


@dataclass(frozen=True)
class ConsoleCardModel:
    """The Console's Today card. `available` False means unavailable, never zero or no usage."""

    available: bool
    capacity: bool
    headline: str
    pick: str
    age: str
    stale_day: bool
    # "Partial" is its own badge, not part of the headline text.
    partial: bool = False
    # Line 2 inputs (see `console_line2`): each tool's top fresh window as "Agent A 51% weekly", in
    # rank order; the first one's reset as "resets Sun 06:58"; and the fallback line shown when no
    # tool has a limit reading ("Most used: Agent A 1.2B", or "" for none).
    picks: tuple[str, ...] = ()
    first_reset: str = ""
    fallback: str = ""


@dataclass(frozen=True)
class SpaceRow:
    """One space in the Space picker: every space of the account, Not linked included."""

    key: str  # the server's workspace id, or NOT_LINKED_ID (never sent to the server)
    name: str
    tokens: int | None  # None: unknown (an over-capacity report), never shown as zero
    tokens_text: str
    unlinked: bool  # the synthetic Not linked row: italic, never pinnable
    zero: bool  # no usage in the period: sorts last, drawn faint
    last_used_day: str | None = None  # YYYY-MM-DD from the report; None: unknown, never guessed


@dataclass(frozen=True)
class ProviderLine:
    """One connected CLI on the Providers screen. `status` is the one status line shown there, from
    the
    same selector result as the Plan limits row (or the honest token status), never a second
    wording."""

    kind: str
    name: str
    monogram: str
    status: str
    warn: bool
    action: str | None  # "setup" (the command dialog), "refresh", or None (a chevron row)
    links: str | None = (
        None  # "12 of 80 sessions linked · 9 inferred", only when the server sent it
    )


@dataclass(frozen=True)
class SpaceArrangement:
    pinned: tuple[SpaceRow, ...]
    rest: tuple[SpaceRow, ...]  # the ALL section, sorted; zero-usage spaces last
    results: tuple[SpaceRow, ...] | None  # the search results; None when the search is empty


SORT_MOST = "most"
SORT_AZ = "az"
SORT_RECENT = "recent"
_LIMIT_PROBLEMS = (
    s.STATUS_SETUP_NEEDED,
    s.STATUS_CONFLICT,
    s.STATUS_NO_READING,
    s.STATUS_READING_UNAVAILABLE,
    s.STATUS_STALE,
    s.STATUS_WINDOW_ENDED,
)


def _fuzzy(query: str, name: str) -> bool:
    """Every character of the query appears in the name, in order (the fuzzy pass)."""
    position = 0
    for char in query:
        found = name.find(char, position)
        if found < 0:
            return False
        position = found + 1
    return True


def arrange_spaces(
    rows: Sequence[SpaceRow],
    *,
    query: str,
    sort: str,
    pins: Sequence[str],
) -> SpaceArrangement:
    """The picker's sections. Search: case-insensitive substring matches first, then fuzzy matches.
    Browse: pinned first (in pin order, hidden when empty), then every space sorted by tokens or
    A-Z,
    with zero-usage spaces last. Not linked is never pinned."""
    by_key = {row.key: row for row in rows}
    pinned = tuple(by_key[key] for key in pins if key in by_key and not by_key[key].unlinked)
    needle = query.strip().casefold()

    def name_key(row: SpaceRow) -> tuple[bool, str, str]:
        return (row.zero, row.name.casefold(), row.key)

    def tokens_key(row: SpaceRow) -> tuple[bool, int, str, str]:
        return (row.zero or row.tokens is None, -(row.tokens or 0), row.name.casefold(), row.key)

    def ordered(items: Sequence[SpaceRow]) -> list[SpaceRow]:
        if sort == SORT_AZ:
            return sorted(items, key=name_key)
        if sort == SORT_RECENT:
            # Newest day first. A space with no known day sorts last, and zero-usage spaces sort
            # after
            # every space with usage (the stable name pass keeps equal days in A-Z order).
            by_name = sorted(items, key=lambda row: row.name.casefold())
            return sorted(
                by_name,
                key=lambda row: (not row.zero, row.last_used_day or ""),
                reverse=True,
            )
        return sorted(items, key=tokens_key)

    if needle:
        substring = [row for row in rows if needle in row.name.casefold()]
        fuzzy = [
            row for row in rows if row not in substring and _fuzzy(needle, row.name.casefold())
        ]
        return SpaceArrangement(
            pinned=(),
            rest=(),
            results=tuple(ordered(substring)) + tuple(ordered(fuzzy)),
        )
    return SpaceArrangement(pinned=pinned, rest=tuple(ordered(rows)), results=None)


# -- building the screen --------------------------------------------------------------------------


def _cost(usage: s.Usage) -> str:
    """The cost text. A partial selection shows its priced subtotal (flagged separately as Partial);
    an all-unpriced or unknown one is an em dash, never $0."""
    if usage.cost.state in ("unknown", "unpriced"):
        return "—"
    return money(usage.cost.amount_micro_usd)


def _a11y(name: str, usage: s.Usage, partial: bool, unpriced: bool, honest: str | None) -> str:
    if honest is not None:
        return f"{name}, {honest.lower()}"
    if unpriced and not partial:
        cost = "unpriced"
    elif partial:
        cost = f"partly unpriced, {money(usage.cost.amount_micro_usd)}"
    else:
        cost = money(usage.cost.amount_micro_usd)
    return f"{name}, {tokens_words(usage.tokens or 0)}, {cost}"


NOT_LINKED_NOTE = "Sessions RelayDeck could not tie to one space"


def link_line(entry: model.CoverageEntry | None) -> str | None:
    """ "12 of 80 sessions linked · 9 inferred". Only when the server sent both link counts: an
    older
    server's missing field shows nothing, never a zero."""
    if entry is None or entry.linked_sessions is None or entry.inferred_sessions is None:
        return None
    total = entry.linked_sessions + entry.inferred_sessions + entry.unmapped_sessions
    return (
        f"{entry.linked_sessions} of {total} sessions linked · {entry.inferred_sessions} inferred"
    )


def _inferred_spaces(page: UsagePage, flt: s.Filter, today: date) -> frozenset[str]:
    """Spaces with at least one inferred-linked session in the period (and provider), by opaque
    id."""
    first, last = s.window_bounds(flt.period, today)
    found: set[str] = set()
    for item in page.rows:
        if item.scope != "workspace" or not item.workspace_id or not item.inferred_sessions:
            continue
        try:
            day = date.fromisoformat(item.day)
        except ValueError:
            continue
        if first <= day <= last and (flt.provider == s.ALL or item.kind == flt.provider):
            found.add(item.workspace_id)
    return frozenset(found)


def _ranked(
    items: list[s.Bucket],
    names: dict[str, str],
    n: int,
    *,
    roles: dict[str, str] | None = None,
    honest: dict[str, str] | None = None,
    noun: tuple[str, str],
    italic_key: str | None = None,
    links: dict[str, str] | None = None,
    inferred: frozenset[str] = frozenset(),
    notes: dict[str, str] | None = None,
) -> RankedModel:
    ranked = s.rank(items, n)
    rows: list[RowModel] = []
    for bucket in ranked.top:
        usage = bucket.usage
        cost = _cost(usage)
        partial = usage.cost.state == "partial"
        unpriced = usage.cost.state == "unpriced"
        if partial:
            subline = f"{cost} · Partial"
        elif unpriced:
            subline = "Unpriced"
        else:
            subline = cost
        name = names.get(bucket.key, bucket.name)
        has_inferred = bucket.key in inferred
        note = (notes or {}).get(bucket.key)
        accessible = _a11y(name, usage, partial, unpriced, None)
        if has_inferred:
            accessible += ", includes inferred sessions"
        if note:
            accessible += f". {note}"
        rows.append(
            RowModel(
                key=bucket.key,
                name=name,
                value=s.tokens_text(usage.tokens or 0),
                share=s.share(usage.tokens or 0, ranked.peak),
                subline=subline,
                unpriced=unpriced or partial,
                italic=bucket.key == italic_key,
                monogram=s.monogram(name),
                series=(roles or {}).get(bucket.key),
                honest=None,
                a11y=accessible,
                amount=cost,
                partial=partial,
                links=(links or {}).get(bucket.key),
                inferred=has_inferred,
                note=note,
            )
        )
    honest_items = list((honest or {}).items())
    overflow_names = tuple(names.get(key, key) for key, _status in honest_items[n:])
    for key, status in honest_items[:n]:
        name = names.get(key, key)
        rows.append(
            RowModel(
                key=key,
                name=name,
                value=status,
                share=0.0,
                subline=None,
                unpriced=False,
                italic=False,
                monogram=s.monogram(name),
                series=None,
                honest=status,
                a11y=_a11y(name, s.usage_of(()), False, False, status),
            )
        )
    rollup = None
    if ranked.other is not None:
        rollup = RollupModel(
            text=f"Other · {plural(ranked.other.count, noun[0], noun[1])}",
            value=s.tokens_text(ranked.other.usage.tokens or 0),
            count=ranked.other.count,
        )
    return RankedModel(rows=tuple(rows), rollup=rollup, total=len(items), overflow=overflow_names)


def _provider_roles(items: list[s.Bucket]) -> dict[str, str]:
    """Only the top two providers by tokens get a series colour; every other provider is neutral."""
    ordered = sorted(
        items, key=lambda item: (-(item.usage.tokens or 0), item.name.casefold(), item.key)
    )
    roles: dict[str, str] = {}
    for position, item in enumerate(ordered):
        roles[item.key] = ("series_1", "series_2")[position] if position < 2 else "series_other"
    return roles


def _attention(
    page: UsagePage, now_ms: int, flt: s.Filter, compact: bool, tz: str
) -> tuple[AttentionModel, AttentionModel]:
    """(the shown list, capped on a phone; every item for the temporary "+k more" list)."""
    shown = s.attention(page, now_ms, compact=compact, flt=flt)
    everything = s.attention(page, now_ms, compact=False, flt=flt)
    limits = {view.kind: view for view in s.limits(page, now_ms, flt)}
    names = {kind.kind: kind.display_name or kind.kind for kind in page.kinds}

    def items_of(found: s.Attention) -> tuple[AttentionItem, ...]:
        out = []
        for entry in found.items:
            reason, action, label, tone = _reason(entry.status, limits.get(entry.kind), now_ms, tz)
            name = names.get(entry.kind, entry.kind)
            out.append(
                AttentionItem(
                    key=entry.kind,
                    name=name,
                    monogram=s.monogram(name),
                    reason=reason,
                    tone=tone,
                    action=action,
                    action_label=label,
                )
            )
        return tuple(out)

    return (
        AttentionModel(items=items_of(shown), more=shown.more),
        AttentionModel(items=items_of(everything), more=0),
    )


def _setup_text(view: s.ProviderLimits | None, default: str) -> str:
    """Why the hook needs attention, in one line, with the command to run on the Mac."""
    state = view.setup_state if view is not None else None
    name = view.name if view is not None else "The CLI"
    if state == "statusline_missing":
        return (
            f"{name}'s status line no longer runs RelayDeck's hook — repair on your Mac: "
            "relaydeck usage repair"
        )
    if state == "script_outdated":
        return (
            f"RelayDeck was updated and {name}'s hook needs refreshing — repair on your Mac: "
            "relaydeck usage repair"
        )
    return default


def _reason(
    status: str, view: s.ProviderLimits | None, now_ms: int, tz: str
) -> tuple[str, str | None, str | None, str]:
    """(reason text, action, action label, tone) for one attention status."""
    if status == s.STATUS_SETUP_NEEDED:
        return _setup_text(view, "Limits hook not installed"), "setup", "Set up", TONE_NEUTRAL
    if status == s.STATUS_CONFLICT:
        return "Another status line is configured", "setup", "Set up", TONE_NEUTRAL
    if status == s.STATUS_NO_READING:
        return "Installed · no reading yet", "refresh", "Refresh", TONE_WARN
    if status == s.STATUS_READING_UNAVAILABLE:
        return "Reading unavailable", "refresh", "Refresh", TONE_WARN
    if status == s.STATUS_STALE:
        read = _relative(now_ms, view.observed_ms, tz) if view and view.observed_ms else "earlier"
        return f"Reading stale · read {read}", "refresh", "Refresh", TONE_WARN
    if status == s.STATUS_WINDOW_ENDED:
        return "A limit window ended with no new reading", "refresh", "Refresh", TONE_WARN
    if status == s.STATUS_NEAR_LIMIT and view is not None:
        for window in view.windows:
            if window.status == "fresh" and window.used_percent >= s.NEAR_LIMIT_PERCENT:
                resets = (
                    ""
                    if window.resets_at_ms is None
                    else f" · resets {_reset(window.resets_at_ms, tz)}"
                )
                return (
                    f"{window.label} at {round(window.used_percent)}%{resets}",
                    None,
                    None,
                    TONE_WARN,
                )
    return "Needs a look", None, None, TONE_WARN


def _series_roles(count: int) -> tuple[str, ...]:
    if count == 1:
        return ("series_1",)
    return ("series_1", "series_2", "series_other")[:count]


def _axis_label(grain: str, start: str, label: str, compact: bool) -> str:
    """The x-axis text for one bucket: one unbroken token. Hours are clock times. A non-breaking
    space
    joins the weekday and the day, and a long day chart (more than 14 buckets) shows the day
    alone."""
    if grain == "hour":
        return f"{int(label):02d}:00"
    day = date.fromisoformat(start)
    if grain == "week":
        return f"{day:%b}\u00a0{day.day}"
    return str(day.day) if compact else f"{_weekday(day)}\u00a0{day.day}"


def _labelled(grain: str, index: int, count: int, label: str, hour_step: int) -> bool:
    """Which buckets get an axis label. Hours: every `hour_step` hours. A long day chart (30 days on
    a
    phone) labels every fifth bucket, counting back from the latest day. Other charts label all."""
    if grain == "hour":
        return int(label) % hour_step == 0
    if grain != "day" or count <= 14:
        return True
    return (count - 1 - index) % 5 == 0


def _hour_range(label: str) -> str:
    hour = int(label)
    return f"{hour:02d}:00–{hour + 1:02d}:00"


def _peak_label(grain: str, start: str, label: str) -> str:
    if grain == "hour":
        return _hour_range(label)
    day = date.fromisoformat(start)
    if grain == "week":
        return f"week of {_short_day(day)}"
    return _weekday(day)


def _chart(
    chart: s.Chart,
    flt: s.Filter,
    *,
    wide: bool,
    now_hour: int | None,
    credits: bool = False,
) -> ChartModel:
    """The bars. `credits` draws a credit tool's credits per day instead of tokens: the same
    shape, with the values formatted as credits (thousandths in, one decimal out)."""
    fmt = s.credits_chart_text if credits else s.tokens_chart_text
    unit = "credits" if credits else "tokens"
    if not chart.available:
        return ChartModel(False, "", "", (), (), "0", "0", False)
    totals = [sum(bucket.tokens) for bucket in chart.buckets]
    peak_total = max(totals) if totals else 0
    peak_index = totals.index(peak_total) if peak_total > 0 else -1
    roles = _series_roles(len(chart.series))
    names = chart.series_names
    count = len(chart.buckets)
    hour_step = 3 if wide else 6
    now_index = now_hour if chart.grain == "hour" else None
    bars = []
    for index, bucket in enumerate(chart.buckets):
        total = totals[index]
        future = now_index is not None and index > now_index
        segments = (
            ()
            if future
            else tuple(
                SegmentModel(
                    role=roles[position], fraction=(value / peak_total) if peak_total else 0.0
                )
                for position, value in enumerate(bucket.tokens)
            )
        )
        if chart.grain == "hour":
            when = _hour_range(bucket.label)
        else:
            when = _short_day(date.fromisoformat(bucket.start))
        if future:
            tooltip = f"{when} · not yet"
        else:
            parts = [f"{when} · {fmt(total)} {unit}"]
            for position, value in enumerate(bucket.tokens):
                if value:
                    parts.append(f"{names[position]} {fmt(value)}")
            tooltip = " · ".join(parts)
        # The peak always gets a label, and the latest bucket too (not for hours: that is "now"'s
        # job).
        labelled = (
            index == peak_index
            or (chart.grain != "hour" and index == count - 1)
            or _labelled(chart.grain, index, count, bucket.label, hour_step)
        )
        bars.append(
            BarModel(
                label=_axis_label(chart.grain, bucket.start, bucket.label, count > 14)
                if labelled
                else "",
                value="" if future or total == 0 else fmt(total),
                segments=segments,
                stub=not future and total == 0,
                peak=index == peak_index,
                tooltip=tooltip,
                future=future,
                now=index == now_index,
            )
        )
    prefix = {"hour": "Hourly", "week": "Weekly"}.get(chart.grain, "Daily")
    if peak_index >= 0:
        peak_bucket = chart.buckets[peak_index]
        where = _peak_label(chart.grain, peak_bucket.start, peak_bucket.label)
        meta = f"Peak {fmt(peak_total)} · {where}"
        summary = f"{prefix} {unit}. Peak {fmt(peak_total)} on {where}."
    else:
        meta = "No usage in this period"
        summary = f"{prefix} {unit}. No usage in this period."
    legend = tuple(zip(roles, names, strict=True)) if len(roles) > 1 else ()
    return ChartModel(
        available=True,
        meta=meta,
        summary=summary,
        bars=tuple(bars),
        legend=legend,
        axis_max=fmt(peak_total),
        axis_mid=fmt(peak_total // 2),
        hourly_missing=chart.grain == "hour" and not chart.hourly_available,
        grain=chart.grain,
        title=f"{prefix} credits" if credits else "",
    )


def _busiest(chart: s.Chart) -> tuple[str, str]:
    if not chart.available or not chart.buckets:
        return "—", "No usage in this period"
    totals = [sum(bucket.tokens) for bucket in chart.buckets]
    peak = max(totals)
    if peak == 0:
        return "—", "No usage in this period"
    overall = sum(totals)
    bucket = chart.buckets[totals.index(peak)]
    pct = round(100 * peak / overall)
    if chart.grain == "hour":
        when = f"{bucket.label}:00"
    elif chart.grain == "week":
        when = f"week of {_short_day(date.fromisoformat(bucket.start))}"
    else:
        day = date.fromisoformat(bucket.start)
        when = f"{_weekday(day)}, {_short_day(day)}"
    return s.tokens_chart_text(peak), f"{when} · {pct}% of the period"


def _amount_text(value: float) -> str:
    """0.15 -> "0.15", 50.0 -> "50", 1234.5 -> "1,234.5" (at most two decimals)."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def amount_line(window: s.LimitWindow) -> str:
    """ "0.15 of 50 credits" for a window that reports amounts, "12 credits used" when the plan
    has no limit amount (0 or missing: no bar, no percentage, never a division), and "" for a
    percent-only window."""
    if window.used_amount is None:
        return ""
    unit = (window.unit or "").strip()
    noun = f"{unit}s" if unit.casefold() == "credit" else unit
    used = _amount_text(window.used_amount)
    if window.limit_amount is None or window.limit_amount <= 0:
        one = unit if window.used_amount == 1 else noun
        return f"{used} {one} used" if one else f"{used} used"
    text = f"{used} of {_amount_text(window.limit_amount)}"
    return f"{text} {noun}" if noun else text


def _has_limit(window: s.LimitWindow) -> bool:
    return window.limit_amount is not None and window.limit_amount > 0


def _reset_when(resets_at_ms: int, now_ms: int, tz: str) -> str:
    """A reset more than six days away is a date (`Nov 1`); nearer ones keep `Sun 06:58`."""
    if resets_at_ms - now_ms > 6 * 86_400_000:
        when = datetime.fromtimestamp(resets_at_ms / 1000, ZoneInfo(tz))
        return f"{when:%b} {when.day}"
    return _reset(resets_at_ms, tz)


def _refresh_model(
    view: s.ProviderLimits, local: RefreshLocal | None, now_ms: int
) -> RefreshModel | None:
    info = view.refresh
    if info is None or not info.available:
        return None
    cooldown = max(info.cooldown_until_ms or 0, (local.until_ms or 0) if local else 0)
    cooling = cooldown > now_ms
    attempt = info.last_attempt
    running = info.in_progress or (local is not None and local.phase == REFRESH_RUNNING)
    notes: list[str] = []
    if running:
        waited = local is not None and now_ms - local.at_ms >= REFRESH_WINDOW_MS
        notes.append("still checking…" if waited else "reading…")
    elif local is not None and local.phase == REFRESH_FAILED:
        notes.append(_REFRESH_ERRORS.get(local.error or "", _REFRESH_ERROR_FALLBACK))
    elif (
        local is not None
        and local.phase == REFRESH_DONE
        and now_ms - local.at_ms < REFRESH_DONE_SHOWN_MS
    ):
        notes.append("checked just now")
    elif info.last_error:
        notes.append(_REFRESH_ERRORS.get(info.last_error, _REFRESH_ERROR_FALLBACK))
    left = -(-(cooldown - now_ms) // 60_000) if cooling and not running else None
    if left is not None:
        if (
            attempt is not None
            and attempt.result == "ok"
            and attempt.finished_ms is not None
            and not notes
            and not (local is not None and local.phase == REFRESH_COOLDOWN)
        ):
            # Say a read just happened, so a paused button is not mistaken for a broken one.
            age = max(0, (now_ms - attempt.finished_ms) // 60_000)
            notes.append("read just now" if age < 1 else f"read {age} min ago")
        notes.append(f"next refresh in {max(1, left)} min")
    return RefreshModel(
        kind=view.kind,
        label=f"Refresh {view.name} limits",
        busy=running,
        enabled=not running and not cooling,
        note=" · ".join(notes),
        cooldown_minutes=(0 if (cooldown - now_ms) < 60_000 else left)
        if left is not None
        else None,
    )


def _limit_rows(
    page: UsagePage,
    now_ms: int,
    flt: s.Filter,
    tz: str,
    local: Mapping[str, RefreshLocal] | None = None,
) -> tuple[LimitRowModel, ...]:
    rows = []
    for view in s.limits(page, now_ms, flt):
        windows = []
        for window in view.windows:
            if window.status == "expired":
                resets = (
                    f"Window ended · resets {_reset(window.resets_at_ms, tz)}"
                    if window.resets_at_ms
                    else "Window ended"
                )
                windows.append(WindowModel(window.label, "—", 0.0, "neutral", resets, True))
                continue
            pct = round(window.used_percent)
            over = window.over_allowance
            role = (
                "danger"
                if pct >= 100 or over
                else "warn"
                if pct >= s.NEAR_LIMIT_PERCENT
                else "series_1"
            )
            amount = amount_line(window)
            # No limit amount: the amount alone, with no bar and no percentage.
            bar = not amount or _has_limit(window)
            if amount:
                # A credit window dates its reset (`Nov 1`) and names the plan beside it.
                parts = (
                    [f"Resets {_reset_when(window.resets_at_ms, now_ms, tz)}"]
                    if (window.resets_at_ms)
                    else []
                )
                if view.plan_label:
                    parts.append(view.plan_label)
                resets = " · ".join(parts)
            else:
                resets = f"Resets {_reset(window.resets_at_ms, tz)}" if window.resets_at_ms else ""
            if window.idle and window.status == "fresh":
                role, resets = "neutral", "Idle · no use yet"
            if window.status == "stale":
                # An old reading keeps its own percentage, marked stale, never shown as current.
                role = "neutral"
                resets = f"Reading stale · {resets}" if resets else "Reading stale"
            windows.append(
                WindowModel(
                    label=window.label,
                    percent=(
                        "over plan"
                        if over and bar
                        else ("<1%" if pct == 0 and window.used_percent > 0 else f"{pct}%")
                        if bar
                        else ""
                    ),
                    fraction=1.0
                    if over and bar
                    else min(1.0, max(0.0, window.used_percent / 100))
                    if bar
                    else 0.0,
                    role=role if bar else "neutral",
                    resets=resets,
                    ended=False,
                    amount=amount,
                    bar=bar,
                )
            )
        rows.append(
            LimitRowModel(
                key=view.kind,
                name=view.name,
                monogram=s.monogram(view.name),
                subline=_limit_subline(view, now_ms, tz),
                windows=tuple(windows),
                status=view.status,
                refresh=_refresh_model(view, (local or {}).get(view.kind), now_ms),
            )
        )
    return tuple(rows)


def _limit_subline(view: s.ProviderLimits, now_ms: int, tz: str) -> str:
    """The setup state and the reading state, reported separately: a
    missing
    hook or a conflict keeps the last reading's age beside it. "No reading" only when none exists,
    and
    "No limits hook installed" only when the setup evidence says so (it is never inferred)."""
    age = _relative(now_ms, view.observed_ms, tz) if view.observed_ms else None
    info = view.refresh
    # A tool that can be read on demand says "Checked", and one with no Herdr space yet says how
    # to get a reading.
    on_demand = info is not None and info.available
    hint = f" · {NO_SPACE_HINT}" if info is not None and info.reason == "no_space" else ""
    if view.status in (s.STATUS_SETUP_NEEDED, s.STATUS_CONFLICT):
        label = (
            _setup_text(view, "Setup needed")
            if view.status == s.STATUS_SETUP_NEEDED
            else "Conflict"
        )
        return f"{label} · last reading {age}" if age else f"{label} · no reading yet"
    if view.status in (s.STATUS_OK, s.STATUS_NEAR_LIMIT):
        idle = next((w for w in view.windows if w.idle and w.status == "fresh"), None)
        if idle is not None:
            return f"{idle.label} window idle"
        if age:
            quiet = " · no use since" if view.quiet else ""
            return (f"Checked {age}" if on_demand else f"Read {age}") + quiet
        return "No reading yet"
    if view.status == s.STATUS_STALE:
        return f"Reading stale · read {age}" if age else "Reading stale"
    if view.status == s.STATUS_WINDOW_ENDED:
        return f"A limit window ended · read {age}" if age else "A limit window ended"
    if view.status == s.STATUS_READING_UNAVAILABLE:
        return "Reading unavailable"
    if view.status == s.STATUS_NOT_REPORTED:
        return "This CLI does not report limits"
    if view.status == s.STATUS_NO_READING:
        return f"Installed · no reading yet{hint}"
    if view.status == s.STATUS_UNMANAGED:
        return f"Read {age}" if age else f"Limits not connected yet{hint}"
    return f"No reading yet{hint}"


_CAVEAT_NO_TOKENS = "reports no token counts"
_CAVEAT_CREDITS = "reports credits, not tokens"
_CAVEAT_UNVERIFIED = "token counts not verified yet"
_CAVEAT_UNAVAILABLE = "no reading right now"


PARTLY_VERIFIED = "partly_verified"
PARTLY_NOTE = "some sessions not verified"


def effective_coverage(page: model.UsagePage) -> dict[str, str]:
    """Coverage by kind for display. A kind whose coverage is `unverified` but that has verified
    tokens in the page is `partly_verified`: its tokens and cost are shown like any provider's, with
    a small note. Only a kind with no verified tokens stays status-only."""
    with_tokens = {row.kind for row in page.rows if row.counters.tokens > 0}
    out: dict[str, str] = {}
    for entry in page.coverage:
        value = entry.coverage
        if value == "unverified" and entry.kind in with_tokens:
            value = PARTLY_VERIFIED
        out[entry.kind] = value
    return out


def token_caveat(page: model.UsagePage, coverage: dict[str, str] | None = None) -> str:
    """One line naming each tool the token totals leave out, built only from the page's kinds and
    coverage. Nothing is hard-coded to a tool name. "" when every tool is included.

    Excluded when the tool reports no tokens, or its token counts are unverified, or it has no
    reading right now. A tool's own display name is used, never a guessed one."""
    coverage_by_kind = coverage if coverage is not None else effective_coverage(page)
    excluded: list[str] = []
    for entry in page.kinds:
        state = coverage_by_kind.get(entry.kind)
        if entry.tokens_reported and state == PARTLY_VERIFIED:
            continue  # its provider row carries the note; it is no reason to doubt the totals
        if not entry.tokens_reported and entry.credits_reported:
            reason = _CAVEAT_CREDITS
        elif not entry.tokens_reported:
            reason = _CAVEAT_NO_TOKENS
        elif state == "unverified":
            reason = _CAVEAT_UNVERIFIED
        elif state == "unavailable":
            reason = _CAVEAT_UNAVAILABLE
        else:
            continue
        excluded.append(f"{entry.display_name or entry.kind} ({reason})")
    if not excluded:
        return ""
    return "Token totals do not include: " + " · ".join(excluded) + "."


def _footnote(rows: Sequence[model.Row]) -> str:
    priced = [
        row for row in rows if row.cost_micro_usd is not None and (row.priced_tokens or 0) > 0
    ]
    snapshots = sorted({row.price_snapshot for row in priced if row.price_snapshot})
    if not snapshots:
        if priced:
            return f"{_ESTIMATE} · price snapshot unavailable"
        return f"{_ESTIMATE} · no priced usage in this view"
    if len(snapshots) == 1:
        return f"{_ESTIMATE} · price snapshot {snapshots[0]}"
    return f"{_ESTIMATE} · price snapshots {snapshots[0]} to {snapshots[-1]}"


def _command_lines(page: UsagePage) -> tuple[CommandModel, ...]:
    """The exact offline command per configured source that still needs its limits hook. Built only
    from the validated `kinds[].sources[]` kind and format, shell-quoted; every command works as
    typed against the default companion config. An outdated hook collapses to one repair line,
    which covers every source of the kind. A kind whose server did not report its sources gets
    no command: the format is needed, so the screen points at the Mac's doctor instead."""
    out = []
    for kind in page.kinds:
        lines: list[str] = []
        outdated = False
        for source in kind.sources:
            if source.setup is None or source.setup.state == "installed":
                continue
            if not all(_SAFE_NAME.fullmatch(part) for part in (kind.kind, source.format)):
                continue
            if source.setup.state == "script_outdated":
                outdated = True
                continue
            argument = f"{kind.kind}/{source.format}"
            lines.append(
                f"relaydeck usage install --source={shlex.quote(argument)} --install-limits-hook"
            )
        if outdated:
            lines.insert(0, "relaydeck usage repair")
        if lines:
            out.append(CommandModel(kind=kind.kind, lines=tuple(lines), outdated=outdated))
    return tuple(out)


def _message(state: str) -> str:
    return {
        STATE_CAPACITY: "Too much data for this period — choose a shorter period or one space.",
        STATE_UNAVAILABLE: "Usage is unavailable right now.",
        STATE_DISABLED: "Usage is turned off for this host.",
        STATE_NO_PROVIDERS: "Connect a CLI to start tracking usage.",
    }.get(state, "")


def build(
    page: UsagePage | None,
    *,
    state: str,
    flt: s.Filter,
    now_ms: int,
    width: str,
    updating: bool = False,
    inventory: UsagePage | None = None,
    timezone: str | None = None,
    refresh: Mapping[str, RefreshLocal] | None = None,
) -> ScreenModel:
    """The whole screen for one state. `page` is None unless the state carries data. An over-
    capacity
    state carries its `inventory` instead: the spaces and providers to choose from, with unknown
    values."""
    # The host's zone is the page's, or the one the caller remembers from an earlier page of the
    # same host (`timezone`). It is never guessed: with neither, the clock part is left out and
    # the date label uses the device calendar (a label only; no host data is read with it).
    zone_name = page.timezone if page is not None else timezone
    tz = zone_name or ""
    today = (
        model.host_day(now_ms, zone_name)
        if zone_name
        else datetime.fromtimestamp(now_ms / 1000).date()
    )
    range_text = s.date_range(*s.window_bounds(flt.period, today), today)
    now_local = datetime.fromtimestamp(now_ms / 1000, ZoneInfo(zone_name)) if zone_name else None
    if flt.period == "today":
        # Today is the host's day so far, said in the host's own clock.
        range_text = f"{range_text} · since midnight"
        if now_local is not None and zone_name:
            range_text += f" · now {now_local:%H:%M} {zone_label(zone_name)}"
    period_options = tuple((key, label, key == flt.period) for key, label in PERIODS)
    bare = ScreenModel(
        state=state,
        message=_message(state),
        meta=f"{range_text} · loading…" if state == STATE_LOADING else "",
        meta_tone=TONE_NORMAL,
        period=flt.period,
        period_options=period_options,
        range_text=range_text,
        space_value="All",
        space_active=flt.space != s.ALL,
        space_label="All spaces",
        provider_value="All",
        provider_active=flt.provider != s.ALL,
        summary=None,
        attention=AttentionModel((), 0),
        attention_all=AttentionModel((), 0),
        chart=None,
        plan=(),
        spaces=None,
        space_total=0,
        spaces_more=False,
        providers=None,
        provider_zero=None,
        empty=False,
        footnote="",
        space_options=(),
        provider_options=(),
        commands=(),
    )
    if state == STATE_CAPACITY and inventory is not None:
        return _capacity_model(inventory, bare, flt)
    if page is None or state not in (STATE_READY, STATE_OFFLINE):
        return bare
    if not page.kinds:
        return replace(bare, state=STATE_NO_PROVIDERS, message=_message(STATE_NO_PROVIDERS))

    compact = width == WIDTH_COMPACT
    n = top_n(width)
    names = {kind.kind: kind.display_name or kind.kind for kind in page.kinds}
    space_names = dict(page.workspaces)
    coverage = effective_coverage(page)
    credits = s.credits_by_kind(page, flt, today)
    total = s.totals(page, flt, today)
    counts = s.counts(page, flt, today)
    kinds_by_id = {entry.kind: entry for entry in page.kinds}
    credit_chart = (
        flt.provider != s.ALL
        and flt.provider in kinds_by_id
        and _credit_only(kinds_by_id[flt.provider])
    )
    chart_data = s.chart(
        s.credit_chart_page(page) if credit_chart else page,
        flt,
        today,
        now_hour=now_local.hour if flt.period == "today" and now_local is not None else None,
    )
    relative = _relative(now_ms, page.generated_ms, tz)
    if state == STATE_OFFLINE:
        meta, meta_tone = f"{range_text} · offline · last updated {relative}", TONE_WARN
    elif updating:
        meta, meta_tone = f"{range_text} · updating…", TONE_NORMAL
    else:
        meta, meta_tone = f"{range_text} · updated {relative}", TONE_NORMAL
    as_of = _as_of(page)
    if as_of is not None:
        suffix = " · offline" if state == STATE_OFFLINE else " · updating…" if updating else ""
        meta = f"{range_text} · {as_of}{suffix}"

    filtered = s.filtered_rows(page, flt, today)
    cost_text = _cost(total)
    partial = total.cost.state == "partial"
    if total.cost.state == "unpriced":
        cost_caption: str | None = "No pricing available for this selection"
    elif total.cost.state == "unknown":
        cost_caption = "Price data unavailable"
    else:
        cost_caption = None
    partial_note = None
    if partial:
        kinds = {row.kind for row in filtered if (row.unpriced_tokens or 0) > 0}
        partial_note = (
            f"{plural(len(kinds), 'provider', 'providers')} "
            f"({s.tokens_text(total.cost.unpriced_tokens or 0)} tokens) "
            f"{'has' if len(kinds) == 1 else 'have'} no price yet, so the real total is higher."
        )
    busiest_value, busiest_caption = _busiest(chart_data)
    if flt.space != s.ALL:
        context = f"in {_space_label(flt, space_names)}"
    else:
        context = (
            f"{plural(counts.active_providers or 0, 'provider', 'providers')} · "
            f"{plural(counts.active_spaces or 0, 'active space', 'active spaces')}"
        )
    summary = SummaryModel(
        tokens=s.tokens_text(total.tokens or 0) if total.tokens else "0",
        context=context,
        cost=cost_text,
        cost_caption=cost_caption,
        partial=partial,
        partial_note=partial_note,
        busiest_value=busiest_value,
        busiest_caption=busiest_caption,
        credits_line=_credits_line(credits, names),
    )

    shown_attention, all_attention = _attention(page, now_ms, flt, compact, tz)
    spaces_model = None
    spaces_listed = 0
    coverage_entries = {entry.kind: entry for entry in page.coverage}
    provider_links = {
        kind_id: line
        for kind_id, line in ((k, link_line(v)) for k, v in coverage_entries.items())
        if line is not None
    }
    inferred_spaces = _inferred_spaces(page, flt, today)
    if flt.space == s.ALL:
        buckets = s.space_buckets(page, flt, today)
        items = [
            s.Bucket(key=key, name=space_names.get(key) or key, usage=usage)
            for key, usage in buckets.items()
            if key != s.UNLINKED
        ]
        if s.UNLINKED in buckets:
            items.append(s.Bucket(key=s.UNLINKED, name=_NOT_LINKED, usage=buckets[s.UNLINKED]))
        spaces_listed = len(items)  # Not linked is one of these rows, so it is in the count
        spaces_model = _ranked(
            items,
            {**space_names, s.UNLINKED: _NOT_LINKED},
            n,
            noun=("space", "spaces"),
            italic_key=s.UNLINKED,
            inferred=inferred_spaces,
            notes=_space_notes(page, flt, today),
        )

    providers_model = None
    provider_zero = None
    if len(page.kinds) > 1 and flt.provider == s.ALL:
        by_kind = {entry.kind: entry for entry in page.kinds}
        provider_items: list[s.Bucket] = []
        honest: dict[str, str] = {}
        zero = 0
        for entry in s.providers(page, now_ms, replace(flt, provider=s.ALL)):
            usage = entry.usage or s.usage_of(())
            if _credit_only(by_kind[entry.kind]):
                continue  # shown by its credits below, never as zero tokens
            status = _honest(by_kind[entry.kind], coverage)
            if status is not None:
                honest[entry.kind] = status
            elif (usage.tokens or 0) == 0:
                zero += 1
            else:
                provider_items.append(s.Bucket(key=entry.kind, name=entry.name, usage=usage))
        providers_model = _ranked(
            provider_items,
            names,
            n,
            roles=_provider_roles(provider_items),
            honest=honest,
            noun=("provider", "providers"),
            links=provider_links,
            notes={
                **{
                    kind_id: PARTLY_NOTE
                    for kind_id, value in coverage.items()
                    if value == PARTLY_VERIFIED
                },
                **{
                    kind_id: f"{s.credits_text(milli)} credits"
                    for kind_id, milli in credits.items()
                    if kind_id in {item.key for item in provider_items}
                },
            },
        )
        credit_models = tuple(
            _credit_row_model(kind_id, names.get(kind_id, kind_id), milli)
            for kind_id, milli in sorted(
                credits.items(), key=lambda pair: (-pair[1], names.get(pair[0], pair[0]).casefold())
            )
            if milli > 0 and _credit_only(by_kind[kind_id])
        )
        if credit_models:
            providers_model = replace(
                providers_model,
                rows=providers_model.rows + credit_models,
                total=providers_model.total + len(credit_models),
            )
        if zero:
            provider_zero = f"{zero} other{'s' if zero != 1 else ''} had no usage this period"

    all_buckets = s.space_buckets(page, replace(flt, space=s.ALL), today)
    last_days = dict(page.workspace_days)
    option_rows = [
        (key, label, (all_buckets.get(key) or s.usage_of(())).tokens or 0)
        for key, label in page.workspaces
    ]
    # Not linked is always offered, even with no usage, under the screen's local id (never a server
    # id).
    option_rows.append(
        (
            NOT_LINKED_ID,
            _NOT_LINKED,
            all_buckets[s.UNLINKED].tokens or 0 if s.UNLINKED in all_buckets else 0,
        )
    )
    option_rows.sort(key=lambda row: (-row[2], row[1].casefold(), row[0]))
    space_rows = tuple(
        SpaceRow(
            key=key,
            name=label,
            tokens=value,
            tokens_text=s.tokens_text(value),
            unlinked=key == NOT_LINKED_ID,
            zero=value == 0,
            last_used_day=last_days.get(key),
        )
        for key, label, value in option_rows
    )
    plan_all = _limit_rows(page, now_ms, replace(flt, provider=s.ALL), tz, refresh)
    connected = _connected(page, plan_all, names, coverage, provider_links)
    space_options = tuple((key, label, s.tokens_text(value)) for key, label, value in option_rows)
    provider_options = _provider_options(page, flt, today, names, coverage)
    return ScreenModel(
        state=state,
        message="",
        meta=meta,
        meta_tone=meta_tone,
        period=flt.period,
        period_options=period_options,
        range_text=range_text,
        space_value=_space_value(flt, space_names, counts),
        space_active=flt.space != s.ALL,
        space_label=_space_label(flt, space_names),
        provider_value=_provider_value(flt, names, counts),
        provider_active=flt.provider != s.ALL,
        summary=summary,
        attention=shown_attention,
        attention_all=all_attention,
        chart=_legend_status(
            _chart(
                chart_data,
                flt,
                wide=width == WIDTH_WIDE,
                now_hour=now_local.hour
                if flt.period == "today" and now_local is not None
                else None,
                credits=credit_chart,
            ),
            chart_data.series,
            {entry.kind: entry for entry in page.kinds} if page is not None else {},
            coverage,
        ),
        plan=_limit_rows(page, now_ms, flt, tz, refresh),
        spaces=spaces_model,
        space_total=spaces_listed,
        spaces_more=spaces_model is not None and spaces_listed > len(spaces_model.rows),
        providers=providers_model,
        provider_zero=provider_zero,
        empty=total.tokens == 0 and not credits,
        footnote=_footnote(filtered),
        caveat=token_caveat(page, coverage) if page is not None else "",
        space_options=space_options,
        provider_options=provider_options,
        commands=_command_lines(page),
        space_rows=space_rows,
        connected=connected,
    )


def _provider_options(
    page: UsagePage,
    flt: s.Filter,
    today: date,
    names: dict[str, str],
    coverage: dict[str, str],
) -> tuple[tuple[str, str, str], ...]:
    """Provider choices with their period totals. A provider whose tokens are not reported or not
    verified shows "—" (never zero). Genuinely zero usage shows "0" and sorts last; the rest sort by
    tokens, largest first."""
    by_kind = {entry.kind: entry for entry in page.kinds}
    ranked: list[tuple[int, int, str, str]] = []  # (group, -tokens, name, id) with the value apart
    values: dict[str, str] = {}
    credit_totals = s.credits_by_kind(page, flt, today)
    for entry in page.kinds:
        name = names[entry.kind]
        if _credit_only(by_kind[entry.kind]):
            milli = credit_totals.get(entry.kind, 0)
            ranked.append((1, 0, name.casefold(), entry.kind))
            values[entry.kind] = f"{s.credits_text(milli)} credits" if milli else "—"
            continue
        if _honest(by_kind[entry.kind], coverage) is not None:
            ranked.append((1, 0, name.casefold(), entry.kind))
            values[entry.kind] = "—"
            continue
        tokens = s.totals(page, replace(flt, provider=entry.kind), today).tokens or 0
        if tokens == 0:
            ranked.append((2, 0, name.casefold(), entry.kind))
            values[entry.kind] = "0"
        else:
            ranked.append((0, -tokens, name.casefold(), entry.kind))
            values[entry.kind] = s.tokens_text(tokens)
    ranked.sort()
    return tuple((kind, names[kind], values[kind]) for _group, _neg, _folded, kind in ranked)


def _legend_status(
    chart: ChartModel,
    series: tuple[str, ...],
    by_kind: dict[str, model.Kind],
    coverage: dict[str, str],
) -> ChartModel:
    """The legend names a provider with the same honest status the By provider list shows, so one
    provider has one status everywhere (tokens not verified)."""
    if not chart.legend:
        return chart
    legend = []
    for (role, name), kind_id in zip(chart.legend, series, strict=False):
        kind = by_kind.get(kind_id)
        status = _honest(kind, coverage) if kind is not None else None
        legend.append((role, f"{name} · {status}" if status else name))
    return replace(chart, legend=tuple(legend))


def _connected(
    page: UsagePage,
    plan: tuple[LimitRowModel, ...],
    names: dict[str, str],
    coverage: dict[str, str],
    links: dict[str, str],
) -> tuple[ProviderLine, ...]:
    """One status line per connected CLI. A setup or limit problem wins and reads as the Plan limits
    row does; otherwise the honest token status; otherwise the plan line itself."""
    by_plan = {row.key: row for row in plan}
    overall_credits: dict[str, int] = {}
    for credit in page.credits:
        if credit.scope == "overall":
            overall_credits[credit.kind] = (
                overall_credits.get(credit.kind, 0) + credit.credits_milli
            )
    out = []
    for kind in page.kinds:
        row = by_plan.get(kind.kind)
        honest = _honest(kind, coverage)
        if row is not None and row.status in _LIMIT_PROBLEMS:
            status = row.subline
            warn = row.status != s.STATUS_NO_READING
            if row.status in (s.STATUS_SETUP_NEEDED, s.STATUS_CONFLICT):
                action = "setup"
            else:
                action = "refresh"
        elif _credit_only(kind):
            used = overall_credits.get(kind.kind, 0) > 0
            status = (
                "Credits used · remaining not reported"
                if used
                else "Credits · no use yet · remaining not reported"
            )
            warn, action = False, None
        elif honest is not None:
            status, warn, action = honest, False, None
        elif row is not None:
            status, warn = row.subline, row.status == s.STATUS_STALE
            action = "refresh" if row.status == s.STATUS_UNMANAGED else None
        else:
            status, warn, action = "Limits not connected yet", False, None
        name = names.get(kind.kind, kind.kind)
        out.append(
            ProviderLine(
                kind=kind.kind,
                name=name,
                monogram=s.monogram(name),
                status=status,
                warn=warn,
                action=action,
                links=links.get(kind.kind),
            )
        )
    return tuple(out)


def _space_notes(page: UsagePage, flt: s.Filter, today: date) -> dict[str, str]:
    """Each space's note: Not linked's explanation, and any credits the space used, beside its
    tokens ("42.0 credits")."""
    notes: dict[str, str] = {s.UNLINKED: NOT_LINKED_NOTE}
    for key, milli in s.credit_space_buckets(page, flt, today).items():
        if milli <= 0:
            continue
        text = f"{s.credits_text(milli)} credits"
        notes[key] = f"{notes[key]} · {text}" if key in notes else text
    return notes


def _credit_only(kind: model.Kind) -> bool:
    """A tool metered in credits that reports no tokens."""
    return kind.credits_reported and not kind.tokens_reported


def _credit_row_model(kind_id: str, name: str, milli: int) -> RowModel:
    text = f"{s.credits_text(milli)} credits"
    return RowModel(
        key=kind_id,
        name=name,
        value=text,
        share=0.0,
        subline="Credits, not tokens",
        unpriced=False,
        italic=False,
        monogram=s.monogram(name),
        series=None,
        honest=None,
        a11y=f"{name}, {text}, credits not tokens",
    )


def _credits_line(credits: dict[str, int], names: dict[str, str]) -> str:
    """`+ 162.2 credits (Tool-a)`: one part per tool with credits, apart from tokens and cost."""
    parts = [
        f"+ {s.credits_text(milli)} credits ({names.get(kind, kind)})"
        for kind, milli in sorted(
            credits.items(), key=lambda pair: (-pair[1], names.get(pair[0], pair[0]).casefold())
        )
        if milli > 0
    ]
    return " · ".join(parts)


def _honest(kind: model.Kind, coverage: dict[str, str]) -> str | None:
    """The honest text for a provider whose number is not a usable total."""
    if not kind.tokens_reported:
        # A credit-metered tool reports credits instead of tokens: it has a number to show.
        return None if kind.credits_reported else "Tokens not reported"
    value = coverage.get(kind.kind)
    if value == "unverified":
        return "Tokens not verified"
    if value == "unavailable":
        return "Unavailable"
    return None


def _capacity_model(inventory: UsagePage, bare: ScreenModel, flt: s.Filter) -> ScreenModel:
    """The capacity state: the same filter labels, the real spaces and providers to choose from, and
    unknown values ("—") in every dialog, never zero."""
    names = {kind.kind: kind.display_name or kind.kind for kind in inventory.kinds}
    space_names = dict(inventory.workspaces)
    space_options = tuple(
        [(key, label, "—") for key, label in inventory.workspaces]
        + [(NOT_LINKED_ID, _NOT_LINKED, "—")]
    )
    provider_options = tuple((kind.kind, names[kind.kind], "—") for kind in inventory.kinds)
    # Over capacity, every value is unknown ("—"); the rows are still the real spaces to choose
    # from.
    space_rows = tuple(
        SpaceRow(key=key, name=label, tokens=None, tokens_text="—", unlinked=False, zero=False)
        for key, label in inventory.workspaces
    ) + (
        SpaceRow(
            key=NOT_LINKED_ID,
            name=_NOT_LINKED,
            tokens=None,
            tokens_text="—",
            unlinked=True,
            zero=False,
        ),
    )
    connected = tuple(
        ProviderLine(
            kind=kind.kind,
            name=names[kind.kind],
            monogram=s.monogram(names[kind.kind]),
            status="Usage is over capacity, so this CLI's status is not shown.",
            warn=False,
            action=None,
        )
        for kind in inventory.kinds
    )
    if flt.provider == s.ALL:
        provider_value = next(iter(names.values())) if len(names) == 1 else "All"
    else:
        provider_value = names.get(flt.provider, flt.provider)
    return replace(
        bare,
        space_value=_space_value(flt, space_names, None),
        space_label=_space_label(flt, space_names),
        provider_value=provider_value,
        space_total=len(space_names),
        spaces_more=False,
        space_options=space_options,
        provider_options=provider_options,
        space_rows=space_rows,
        connected=connected,
    )


def _space_value(flt: s.Filter, names: dict[str, str], counts: s.Counts | None) -> str:
    if flt.space == s.ALL:
        total = counts.total_spaces if counts is not None else len(names)
        return f"All · {total}"
    return _space_label(flt, names)


def _space_label(flt: s.Filter, names: dict[str, str]) -> str:
    if flt.space == s.ALL:
        return "All spaces"
    if flt.space == s.UNLINKED:
        return _NOT_LINKED
    return names.get(flt.space) or flt.space


def _provider_value(flt: s.Filter, names: dict[str, str], counts: s.Counts) -> str:
    if flt.provider != s.ALL:
        return names.get(flt.provider, flt.provider)
    if len(names) == 1:
        return next(iter(names.values()))  # one provider: the picker shows its name
    if counts.active_providers is None:
        return "All"
    return f"All · {counts.active_providers} active"


CONSOLE_TWO_FIT = 38  # characters: two tools fit one phone line when the text is this short


def console_line2(card: ConsoleCardModel, *, wide: bool = False) -> str:
    """The strip's second line. Generic: tools come from the page, none is named here.

    - several tools with a fresh limit reading: the top one, and the next when it fits (wide, or a
      short text), each tool once with its highest window, then "+N" for the tools left out;
    - one tool: its window and reset;
    - no tool has a limit reading: the most used tool today, or "" (the line is hidden)."""
    if not card.available:
        return ""
    if not card.picks:
        return card.pick or card.fallback
    if len(card.picks) == 1:
        return card.pick
    two = f"{card.picks[0]} · {card.picks[1]}"
    if wide or len(two) <= CONSOLE_TWO_FIT:
        text, left = two, len(card.picks) - 2
    else:
        text, left = card.picks[0], len(card.picks) - 1
        if card.first_reset:
            text = f"{text} · {card.first_reset}"
    return f"{text} · +{left}" if left > 0 else text


def console_card(page: UsagePage | None, *, state: str, now_ms: int) -> ConsoleCardModel:
    """The Console's Today card. Unavailable and capacity are never shown as 'No usage today'."""
    if page is None or state in (STATE_CAPACITY, STATE_UNAVAILABLE, STATE_DISABLED):
        return ConsoleCardModel(
            available=False,
            capacity=state == STATE_CAPACITY,
            headline=(
                "Too much data today · open Usage"
                if state == STATE_CAPACITY
                else "Usage is unavailable right now."
            ),
            pick="",
            age="",
            stale_day=False,
        )
    card = model.console_card(page, now_ms)
    if not card.available:
        return ConsoleCardModel(False, False, "Usage is unavailable right now.", "", "", False)
    partial = False
    if card.stale_day:
        headline = "Last reading is from an earlier day; open Usage to refresh"
    elif card.has_today:
        if card.priced_micro_usd is None:
            cost = "Unpriced"
        else:
            cost = money(card.priced_micro_usd)
        partial = card.priced_micro_usd is not None and bool(card.unpriced_tokens)
        headline = f"Today {cost} · {s.tokens_text(card.tokens)} tokens"
    else:
        headline = "No usage today"
    short: list[str] = []
    for item in card.picks:
        window = s.window_label(item.window_minutes).lower()
        short.append(f"{item.display_name} {round(item.used_percent)}% {window}")
    first_reset = ""
    if card.pick is not None and card.pick.resets_at_ms is not None:
        first_reset = f"resets {_reset(card.pick.resets_at_ms, page.timezone)}"
    fallback = ""
    if card.top_tool is not None and not short:
        fallback = f"Most used: {card.top_tool[0]} {s.tokens_chart_text(card.top_tool[1])}"
    pick = (short[0] + (f" · {first_reset}" if first_reset else "")) if short else fallback
    return ConsoleCardModel(
        available=True,
        capacity=False,
        headline=headline,
        pick=pick,
        age=_as_of(page) or f"snapshot {_relative(now_ms, page.generated_ms, page.timezone)}",
        stale_day=card.stale_day,
        partial=partial,
        picks=tuple(short),
        first_reset=first_reset,
        fallback=fallback,
    )
