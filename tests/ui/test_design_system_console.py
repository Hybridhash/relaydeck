"""Real Flet dispatch through ConsoleShell.

Covers the three layout branches (390 phone / 768 rail-single / 1440
split), selection/filter/nav callbacks, status ordering, collapsed
strip fidelity, list states, overflow handling and 48px targets.
Detail lifecycle is asserted honestly: the builder re-runs across
breakpoint/collapse changes (documented remount); nothing here claims
retention from object identity — the application keeps the live terminal in the
existing stable retained owner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import build_theme
from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.cards import AgentCard, CollapsedAgentTile
from relaydeck.client.components.design_system.navigation import (
    DEST_CONSOLE,
    ConsoleBottomBar,
    ConsoleRail,
)
from relaydeck.client.components.design_system.primitives import HostChip, RoundIconButton
from relaydeck.client.components.design_system.shell import (
    AgentListItem,
    ConsoleShell,
)
from relaydeck.client.components.design_system.tokens import MIN_TOUCH
from relaydeck.client.features.a11y.focus import iter_controls
from tests.support.host_harness import HostApp, render_component, run

FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "design_system" / "sample_console.json"
)


def _sample() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _items(doc: dict[str, Any]) -> tuple[AgentListItem, ...]:
    return tuple(AgentListItem(**a) for a in doc["agents"])


def _props(
    doc: dict[str, Any], width: float, calls: dict[str, list], **over: Any
) -> dict[str, Any]:
    builds: list[str] = calls.setdefault("builds", [])  # type: ignore[no-redef]

    def builder() -> Any:
        builds.append("detail")
        return ft.Text("DETAIL", key="console-test-detail")

    props: dict[str, Any] = {
        "width": width,
        "destination": DEST_CONSOLE,
        "on_select_destination": lambda d: calls.setdefault("dest", []).append(d),
        "eyebrow": doc["eyebrow"],
        "title": doc["title"],
        "counts": tuple((s, c) for s, c in doc["counts"]),
        "hosts": tuple((h, label) for h, label in doc["hosts"]),
        "selected_host": "",
        "on_select_host": lambda h: calls.setdefault("host", []).append(h),
        "agents": _items(doc),
        "selected_id": "a-fx",
        "on_select_agent": lambda a: calls.setdefault("agent", []).append(a),
        "detail_builder": builder,
        "show_detail": False,
        "collapsed": False,
        "on_toggle_collapse": lambda: calls.setdefault("collapse", []).append(True),
        "on_search": lambda: calls.setdefault("search", []).append(True),
        "on_new_agent": lambda: calls.setdefault("new", []).append(True),
    }
    props.update(over)
    return props


def _ordered_keys(app: HostApp, prefix: str) -> list[str]:
    roots: list[Any] = [app.page.views[0].controls]
    return [
        c.key.removeprefix(prefix) for c in iter_controls(roots) if (c.key or "").startswith(prefix)
    ]


def _card_ids(app: HostApp) -> list[str]:
    return _ordered_keys(app, "agent-card-")


def test_phone_console_callbacks_and_order() -> None:
    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        app = await render_component(ConsoleShell, **_props(doc, 390, calls))
        await app.settle()
        assert app.find("console-nav-bar") is not None
        try:
            app.find("console-nav-rail")
            raise AssertionError("rail must not mount below 600")
        except KeyError:
            pass
        # Status order enforced with stability inside each group.
        assert _card_ids(app) == ["a-fx", "a-onboard", "a-migrate", "a-flaky", "a-changelog"]
        assert app.find("count-blocked") is not None
        assert app.find("host-chip-mac-studio") is not None
        # Collapse toggle is a >=1100 affordance only.
        try:
            app.find("console-collapse-toggle")
            raise AssertionError("collapse toggle must not mount below 1100")
        except KeyError:
            pass
        await app.set_props_and_fire("host-chip-mac-studio", {}, "click")
        await app.set_props_and_fire("agent-card-a-flaky", {}, "click")
        await app.set_props_and_fire("console-nav-terminals", {}, "click")
        await app.set_props_and_fire("console-search", {}, "click")
        await app.set_props_and_fire("console-new-agent", {}, "click")
        await app.settle()
        assert calls["host"] == ["mac-studio"]
        assert calls["agent"] == ["a-flaky"]
        assert calls["dest"] == ["terminals"]
        assert calls["search"] == [True]
        assert calls["new"] == [True]

    run(scenario())


def test_unknown_sorts_last_and_renders() -> None:
    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        agents = _items(doc) + (
            AgentListItem(
                "a-myst", "mystery-box", "mystery", "No telemetry", "mac-studio / ?", "—"
            ),
        )
        app = await render_component(ConsoleShell, **_props(doc, 390, calls, agents=agents))
        await app.settle()
        ids = _card_ids(app)
        assert ids[-1] == "a-myst"
        assert app.find("agent-card-a-myst") is not None

    run(scenario())


def test_wide_split_collapse_keeps_selection_and_order() -> None:
    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        app = await render_component(ConsoleShell, **_props(doc, 1440, calls))
        await app.settle()
        assert app.find("console-nav-rail") is not None
        assert app.find("console-list") is not None
        assert app.find("console-detail") is not None
        assert app.find("console-test-detail").value == "DETAIL"
        assert len(calls["builds"]) == 1
        # Selected full card carries the 3px outline ring (visuals on content).
        assert app.find("agent-card-a-fx").content.border.left.width == 6  # selected bar
        assert app.find("agent-card-a-onboard").content.border.left.width == 3

        collapsed = await render_component(ConsoleShell, **_props(doc, 1440, calls, collapsed=True))
        await collapsed.settle()
        assert collapsed.find("console-strip") is not None
        assert _ordered_keys(collapsed, "agent-tile-") == [
            "a-fx",
            "a-onboard",
            "a-migrate",
            "a-flaky",
            "a-changelog",
        ]
        # Full-name tooltips survive collapsing; selection ring too.
        assert collapsed.find("agent-tile-a-fx").tooltip == "fx-rounding-fix, blocked"
        assert collapsed.find("agent-tile-a-fx").content.shadow is not None
        await collapsed.set_props_and_fire("agent-tile-a-migrate", {}, "click")
        await collapsed.set_props_and_fire("console-expand-toggle", {}, "click")
        await collapsed.settle()
        assert calls["agent"] == ["a-migrate"]
        assert calls["collapse"] == [True]

    run(scenario())


def test_mid_width_rail_single_screen_and_detail_swap() -> None:
    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        app = await render_component(ConsoleShell, **_props(doc, 768, calls))
        await app.settle()
        assert app.find("console-nav-rail") is not None
        assert app.find("console-list") is not None
        detail = await render_component(ConsoleShell, **_props(doc, 768, calls, show_detail=True))
        await detail.settle()
        assert detail.find("console-test-detail") is not None

    run(scenario())


def test_list_states_and_retry() -> None:
    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        loading = await render_component(ConsoleShell, **_props(doc, 390, calls, loading=True))
        await loading.settle()
        assert loading.find("console-list-loading") is not None
        failed = await render_component(
            ConsoleShell,
            **_props(
                doc,
                390,
                calls,
                error="Sync failed.",
                on_retry=lambda: calls.setdefault("retry", []).append(True),
            ),
        )
        await failed.settle()
        await failed.set_props_and_fire("console-list-retry", {}, "click")
        await failed.settle()
        assert calls["retry"] == [True]
        empty = await render_component(ConsoleShell, **_props(doc, 390, calls, agents=()))
        await empty.settle()
        assert "No agents" in str(empty.find("console-list-empty").controls[0].value)

    run(scenario())


def test_overflow_and_hit_targets() -> None:
    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        long_name = "x" * 120
        agents = (AgentListItem("a-long", long_name, "working", "y" * 120, "h / p", "1m"),)
        hosts = tuple((f"h{i}", f"host-{i:02d}") for i in range(12))
        app = await render_component(
            ConsoleShell, **_props(doc, 390, calls, agents=agents, hosts=hosts)
        )
        await app.settle()
        roots: list[Any] = [app.page.views[0].controls]
        titles = [
            c for c in iter_controls(roots) if isinstance(c, ft.Text) and c.value == long_name
        ]
        assert len(titles) == 1
        assert titles[0].no_wrap is True
        assert titles[0].overflow == ft.TextOverflow.ELLIPSIS
        assert app.find("agent-card-a-long").tooltip == long_name
        assert app.find("console-hosts").scroll == ft.ScrollMode.AUTO
        assert app.find("console-cards").scroll == ft.ScrollMode.AUTO
        # Canonical 48px floors on chips, tiles, round buttons, retry.
        assert app.find("host-chip-h0").height == MIN_TOUCH
        assert app.find("console-search").height == MIN_TOUCH
        assert app.find("console-new-agent").height == MIN_TOUCH
        wide = await render_component(
            ConsoleShell, **_props(doc, 1440, calls, collapsed=True, agents=agents)
        )
        await wide.settle()
        assert wide.find("agent-tile-a-long").height == MIN_TOUCH

    run(scenario())


def test_separate_widths_render_detail_from_builder() -> None:
    """Narrow and wide shells each render the builder output.

    Two separate HostApps: this does NOT prove a responsive
    transition or remount, only that each width renders the detail
    builder's content. Retention/transition behavior belongs to
    The application integration in the existing stable AppShell.
    """

    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        narrow = await render_component(ConsoleShell, **_props(doc, 390, calls, show_detail=True))
        await narrow.settle()
        assert narrow.find("console-test-detail") is not None
        assert len(calls["builds"]) == 1
        wide = await render_component(ConsoleShell, **_props(doc, 1440, calls))
        await wide.settle()
        assert wide.find("console-test-detail") is not None
        assert len(calls["builds"]) == 2

    run(scenario())


def test_actions_are_keyboard_focusable_buttons() -> None:
    """A1: every console action is a real focusable button.

    Asserts the actual control type (TextButton carries Tab focus,
    Enter/Space activation and on_focus/on_blur slots in pinned Flet
    1.0.3), zero-padding style with matching shape plus a FOCUSED
    overlay, preserved keys/geometry, and one real click dispatch.
    Live Tab/Enter behavior needs runtime proof (pending).
    """

    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        app = await render_component(ConsoleShell, **_props(doc, 390, calls))
        await app.settle()
        radii = {
            "agent-card-a-fx": T.RADIUS_CARD,
            "host-chip-mac-studio": 22,
            "console-search": 24,
            "console-new-agent": 24,
            "console-nav-terminals": T.RADIUS_KEY,
        }
        for key, radius in radii.items():
            btn = app.find(key)
            assert isinstance(btn, ft.TextButton), key
            assert btn.on_click is not None, key
            assert hasattr(btn, "on_focus") and hasattr(btn, "focus"), key
            assert btn.style is not None, key
            assert btn.style.padding == 0, key
            assert isinstance(btn.style.shape, ft.RoundedRectangleBorder), key
            assert btn.style.shape.radius == radius, key
            overlay = btn.style.overlay_color
            assert isinstance(overlay, dict) and ft.ControlState.FOCUSED in overlay, key
            assert isinstance(btn.content, ft.Container), key
        # Geometry stayed on the button; visuals stayed on the content.
        assert app.find("host-chip-mac-studio").height == MIN_TOUCH
        assert app.find("host-chip-mac-studio").content.bgcolor == T.CARD
        assert app.find("agent-card-a-fx").content.bgcolor == T.BLOCKED
        collapsed = await render_component(ConsoleShell, **_props(doc, 1440, calls, collapsed=True))
        await collapsed.settle()
        tile = collapsed.find("agent-tile-a-fx")
        assert isinstance(tile, ft.TextButton)
        assert tile.height == MIN_TOUCH
        assert tile.content.bgcolor == T.BLOCKED
        rail = await render_component(ConsoleShell, **_props(doc, 768, calls))
        await rail.settle()
        assert isinstance(rail.find("console-rail-terminals"), ft.TextButton)
        # Activation path intact: one real dispatch.
        await app.set_props_and_fire("agent-card-a-flaky", {}, "click")
        await app.settle()
        assert calls["agent"] == ["a-flaky"]

    run(scenario())


def test_text_scale_structure_grows_and_keeps_names() -> None:
    """A1 scale: no fixed text-bucket heights; ellipsized names keep tooltips.

    Structural only: pills/tiles size from text + padding so 200%
    scale grows them instead of clipping, and every intentional
    ellipsis exposes the full name via tooltip. Rendered 200% proof
    needs runtime (pending).
    """

    async def scenario() -> None:
        doc = _sample()
        calls: dict[str, list] = {}
        app = await render_component(ConsoleShell, **_props(doc, 390, calls))
        await app.settle()
        roots: list[Any] = [app.page.views[0].controls]
        pills = [c for c in iter_controls(roots) if isinstance(c, ft.Container) and c.height == 22]
        assert pills == []
        tile = app.find("count-blocked")
        assert tile.height == 52  # the compact strip is a fixed height
        texts = [c for c in iter_controls(roots) if isinstance(c, ft.Text)]
        subtitles = [t for t in texts if t.max_lines == 2]
        assert subtitles, "card subtitle must wrap to 2 lines before ellipsizing"
        assert all(t.no_wrap is not True for t in subtitles)
        assert app.find("console-nav-terminals").tooltip == "Terminals"
        nav_labels = [
            t
            for t in texts
            if t.value in ("Console", "Terminals", "Usage", "Settings")
            and t.style is not None
            and t.style.size == 12  # nav labels, not the 34px header title
        ]
        assert len(nav_labels) == 4
        assert all(t.no_wrap is True for t in nav_labels)
        assert all(t.overflow == ft.TextOverflow.ELLIPSIS for t in nav_labels)

    run(scenario())


def test_action_buttons_neutralize_chrome_theme() -> None:
    """A1 theme guard: widget style defeats the real chrome button theme.

    Uses the actual build_theme (not defaults): the chrome
    text-button theme sets opaque fills, a 1px outline and padding,
    so every action pins transparent bgcolor/border/hover itself and
    carries a same-width focused ring. Lime actions get a dark ring
    (white is unreadable on lime); all others get the light ring.
    """
    for dark in (True, False):
        chrome = build_theme(dark).text_button_theme.style
        assert chrome is not None
        assert chrome.bgcolor is not None  # the conflict is real
        assert chrome.side is not None
    bar = ConsoleBottomBar("console", lambda d: None)
    actions = {
        "card": AgentCard("a", "n", "working", "s", "l", "r", False, lambda i: None),
        "tile": CollapsedAgentTile("a", "n", "blocked", False, lambda i: None),
        "chip": HostChip("h", "H", False, lambda i: None),
        "ghost": RoundIconButton(ft.Icons.ADD, "x", lambda: None),
        "lime": RoundIconButton(ft.Icons.ADD, "x", lambda: None, tone="lime"),
        "nav": bar.content.controls[0],
    }
    for name, btn in actions.items():
        assert isinstance(btn, ft.TextButton), name
        style = btn.style
        assert style is not None, name
        assert style.bgcolor == ft.Colors.TRANSPARENT, name
        assert style.overlay_color[ft.ControlState.DEFAULT] == ft.Colors.TRANSPARENT, name
        assert ft.ControlState.FOCUSED in style.overlay_color, name
        default_side = style.side[ft.ControlState.DEFAULT]
        focused_side = style.side[ft.ControlState.FOCUSED]
        assert default_side.color == ft.Colors.TRANSPARENT, name
        assert default_side.width == focused_side.width == 3, name
    rings = {name: btn.style.side[ft.ControlState.FOCUSED].color for name, btn in actions.items()}
    assert rings["lime"] == T.DONE_TEXT
    assert all(ring == T.TEXT for name, ring in rings.items() if name != "lime"), rings


def _line_height(text: ft.Text) -> float:
    style = text.style
    assert style is not None and style.size is not None and style.height is not None
    return style.size * style.height


def test_variable_actions_keep_structural_48px_floor() -> None:
    """A1 floor guard: variable-height buttons are >=48px from content bounds.

    Pinned Flet 1.0.3 offers no min-height declaration for these
    controls (no Container constraints, no ButtonStyle minimum,
    no tap-target-size API), so the floor is structural: worst-case
    empty-string content still sums above 48px from real tree
    padding/spacing/indicator/style values, and every text bucket
    grows with scale. Assumes empty Text occupies one line height;
    measured-size proof pending runtime.
    """
    card = AgentCard("a", "", "working", "", "", "", False, lambda i: None)
    content = card.content
    assert isinstance(content, ft.Container) and content.padding is not None
    column = content.content
    total = content.padding.top + content.padding.bottom
    total += column.spacing * (len(column.controls) - 1)
    title_row, subtitle_row = column.controls
    pill_text = title_row.controls[1].content
    total += max(_line_height(title_row.controls[0]), _line_height(pill_text))
    badge = subtitle_row.controls[0]
    assert badge.key == "agent-badge-a"
    subtitle_texts = subtitle_row.controls[1:]
    assert len(subtitle_texts) <= 1
    total += max([badge.height] + [_line_height(t) for t in subtitle_texts])
    assert total >= MIN_TOUCH
    for bar in (
        ConsoleBottomBar("console", lambda d: None),
        ConsoleRail("console", lambda d: None),
    ):
        cell = bar.content.controls[0].content
        assert isinstance(cell, ft.Container) and cell.padding is not None
        stack = cell.content
        indicator, label = stack.controls
        total = cell.padding.top
        total += stack.spacing * (len(stack.controls) - 1)
        total += indicator.height
        total += _line_height(label)
        assert total >= MIN_TOUCH
