"""Focus-order audit over real rendered trees.

`iter_controls` reproduces declaration order headlessly (component
bodies, containers, tiles, banners). Handler presence establishes an
action surface, not platform focusability or Tab/Enter/Space support.
Integration tests separately require keyboard-capable control classes;
actual focus traversal still needs browser/device proof. `FOCUS_ORDER` is the declared
requirement per screen: exact keys, or a `prefix*` glob for dynamic
list rows. Strict: unexpected interactive controls fail the check, so
new actions must join the declared order explicitly.

New-screen checklist: a screen that is rendered anywhere in the app
but has no `FOCUS_ORDER` entry (and no case in
`test_focus_orders_match_declared_sequences`) is silently unaudited --
adding the entry + case is part of adding the screen.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import flet as ft

_HANDLER_ATTRS = ("on_click", "on_change", "on_select", "on_submit")

_CHILD_ATTRS = (
    "leading",
    "title",
    "subtitle",
    "content",
    "controls",
    "destinations",
    "actions",
    "trailing",
)


def _is_node(item: Any) -> bool:
    # Controls and declarative Component wrappers (Component is not a
    # Control subclass but owns the rendered body in `_b`).
    return isinstance(item, ft.Control) or hasattr(item, "_b")


def _children(control: Any) -> list[Any]:
    found: list[Any] = []
    body = getattr(control, "_b", None)
    if body is not None:
        found.extend(body if isinstance(body, list) else [body])
    for attr in _CHILD_ATTRS:
        try:
            value = getattr(control, attr, None)
        except Exception:  # noqa: BLE001 - tolerant tree walk
            continue
        if value is None:
            continue
        items = list(value) if isinstance(value, list | tuple) else [value]
        found.extend(item for item in items if _is_node(item))
    return found


def iter_controls(roots: list[Any], *, active_only: bool = False) -> Iterator[Any]:
    """Yield controls in traversal (declaration) order, cycle-safe."""
    seen: set[int] = set()
    stack: list[Any] = list(reversed(roots))
    while stack:
        control = stack.pop()
        if control is None or id(control) in seen:
            continue
        seen.add(id(control))
        if active_only and (
            getattr(control, "visible", True) is False
            or getattr(control, "disabled", False) is True
        ):
            continue
        yield control
        kids = _children(control)
        stack.extend(reversed(kids))


def _page_roots(page: Any) -> list[Any]:
    roots: list[Any] = [page.views[0].controls]
    overlay = getattr(page, "_dialogs", None)
    if overlay is not None:
        roots.extend(overlay.controls or [])
    return roots


def is_interactive(control: Any) -> bool:
    """Actionable: an input handler, or an editable field.

    This predicate does not certify keyboard focusability.

    Disabled or invisible controls are skipped by platform traversal, so
    they never appear in the focus order.
    """
    if getattr(control, "visible", True) is False:
        return False
    if getattr(control, "disabled", False) is True:
        return False
    if isinstance(control, ft.TextField | ft.PopupMenuButton):
        # A menu button opens its items on activation; the items join
        # traversal only inside the open menu.
        return True
    return any(getattr(control, attr, None) is not None for attr in _HANDLER_ATTRS)


def interactive_controls(page: Any) -> tuple[tuple[str, Any], ...]:
    """(key, control) pairs in traversal order. Every interactive control
    must carry a key; unkeyed ones raise (they are unauditable)."""
    pairs: list[tuple[str, Any]] = []
    for control in iter_controls(_page_roots(page), active_only=True):
        if not is_interactive(control):
            continue
        key = getattr(control, "key", None)
        if not key or not isinstance(key, str):
            raise AssertionError(f"interactive {type(control).__name__} has no key")
        pairs.append((key, control))
    return tuple(pairs)


def focus_order(page: Any) -> tuple[str, ...]:
    """Interactive keys in declaration order; actual Tab order is unverified."""
    return tuple(key for key, _ in interactive_controls(page))


def check_focus_order(actual: tuple[str, ...], expected: tuple[str, ...]) -> tuple[str, ...]:
    """Strict match of `actual` against `expected` (`prefix*` globs allowed
    for dynamic rows). Returns error strings; empty means exact match."""
    errors: list[str] = []
    pos = 0
    for want in expected:
        if want.endswith("*"):
            prefix = want[:-1]
            while pos < len(actual) and actual[pos].startswith(prefix):
                pos += 1
            continue
        if pos >= len(actual):
            errors.append(f"missing {want!r}: order ends at {actual!r}")
            continue
        if actual[pos] != want:
            errors.append(f"at #{pos}: want {want!r}, have {actual[pos]!r}")
        pos += 1
    if pos < len(actual):
        errors.append(f"unexpected trailing controls: {actual[pos:]!r}")
    return tuple(errors)


DOCK_KEY_ORDER = (
    "tools-key-escape",
    "tools-key-tab",
    "tools-key-shift-tab",
    "tools-key-shift-enter",
    "tools-key-home",
    "tools-key-end",
    "tools-key-insert",
    "tools-key-delete",
    "tools-key-page-up",
    "tools-key-page-down",
    "tools-key-f1",
    "tools-key-f2",
    "tools-key-f3",
    "tools-key-f4",
    "tools-key-f5",
    "tools-key-f6",
    "tools-key-f7",
    "tools-key-f8",
    "tools-key-f9",
    "tools-key-f10",
    "tools-key-f11",
    "tools-key-f12",
    "tools-key-left",
    "tools-key-up",
    "tools-key-down",
    "tools-key-right",
    "tools-key-enter",
    "tools-key-backspace",
    "tools-mod-ctrl",
    "tools-mod-alt",
    "tools-mod-shift",
    "tools-keys-dismiss",
)

RAIL_ORDER = tuple(f"console-rail-{name}" for name in ("console", "terminals", "usage", "settings"))
PHONE_ORDER = tuple(f"console-nav-{name}" for name in ("console", "terminals", "usage", "settings"))

FOCUS_ORDER: dict[str, tuple[str, ...]] = {
    # Hosts is a Settings section: the rail, then the Settings controls, then the host rows.
    "inventory-hosts-layout": (*RAIL_ORDER, "settings-*", "host-*"),
    # Inventory shell (nav, banner alert, search, create, rows, detail).
    "inventory-agents": (
        *RAIL_ORDER,
        "banner-action*",
        "console-search",
        "console-new-agent",
        "console-collapse-toggle",
        "host-chip-*",
        "agent-*",
        "inventory-refresh*",
        "inventory-more*",
        "detail-open*",
        "detail-back*",
    ),
    "inventory-agents-filtered": (
        *RAIL_ORDER,
        "banner-action*",
        "console-search",
        "console-new-agent",
        "console-collapse-toggle",
        "host-chip-*",
        "sidebar-narrow",
        "sidebar-widen",
        "inventory-host",
        "inventory-status",
        "inventory-project",
        "inventory-query",
        "agent-*",
        "inventory-refresh*",
        "inventory-more*",
        "detail-open*",
        "detail-back*",
    ),
    "inventory-terminals": (
        *RAIL_ORDER,
        "banner-action*",
        # Light theme keeps the sidebar controls and plain filter field.
        "sidebar-*",
        "inventory-query*",
        "new-terminal-open",
        "shells-collapse*",
        "shells-search*",
        "shell-*",
        "detail-open*",
        "detail-back*",
    ),
    "inventory-hosts": (
        *RAIL_ORDER,
        # Settings › Hosts and Devices: the Settings controls, the host rows, then management.
        "settings-*",
        "host-*",
        "management-refresh",
        "managed-host-revoke*",
        "enroll-host-open",
        "enroll-host-label*",
        "enroll-host-key*",
        "enroll-fingerprint*",
        "enroll-host-create*",
        "management-revoke-confirm*",
        "management-revoke-cancel*",
        "managed-device-revoke-*",
    ),
    "inventory-settings": (
        *RAIL_ORDER,
        "banner-action*",
        # Appearance, terminal input, notifications, then device/about.
        "settings-font-size-*",
        "settings-open-*",
        "settings-auto-takeover*",
        "settings-confirm-takeover*",
        "settings-show-key-row*",
        "settings-haptics*",
        "notifications-enable*",
        "settings-logout",
        "fixture-selector*",
        "management-refresh*",
        "managed-device-revoke-*",
        "management-revoke-confirm*",
        "management-revoke-cancel*",
        "settings-acknowledgements-toggle",
        "host-*",  # the Settings › Hosts section
    ),
    # Creation sheets (fields top-to-bottom, Cancel before Submit).
    "start-agent-sheet": (
        "agent-host",
        "agent-workspace",
        "agent-workspace-mode",
        "agent-name",
        "agent-kind",
        "agent-argv",
        "sheet-cancel",
        "sheet-submit",
    ),
    "new-terminal-sheet": (
        "shell-host",
        "shell-workspace",
        "shell-workspace-mode",
        "shell-label",
        "sheet-cancel",
        "sheet-submit",
    ),
    "start-agent-new-workspace": (
        "agent-host",
        "agent-workspace-mode",
        "agent-workspace-directory",
        "agent-workspace-label",
        "agent-name",
        "agent-kind",
        "agent-argv",
        "sheet-cancel",
        "sheet-submit",
    ),
    "new-terminal-new-workspace": (
        "shell-host",
        "shell-workspace-mode",
        "shell-workspace-directory",
        "shell-workspace-label",
        "shell-label",
        "sheet-cancel",
        "sheet-submit",
    ),
    # Composer (draft, validation/dismiss, review, Send last).
    "composer": ("composer-draft", "composer-error-dismiss*", "composer-send"),
    "composer-blocked-review": (
        "composer-draft",
        "composer-blocked-cancel",
        "composer-blocked-confirm",
        # Send is disabled while a review is open, so traversal skips it.
        "composer-send*",
    ),
    # Paste review (Cancel before Paste) and Select Text (field, Copy, Done).
    "paste-review": ("paste-review-cancel", "paste-review-confirm"),
    "select-text": ("select-text-field", "select-text-copy", "select-text-close"),
    # Tools dock (key rows, modifiers, Text dismiss).
    "tools-dock": DOCK_KEY_ORDER,
    # Terminal window: header (Back on narrow layouts,
    # Disconnect, More), key row (keys, More keys, Select text, Paste),
    # then the bottom area (take-over / notices / input row).
    "shell-live": (
        "shell-back*",
        "shell-disconnect*",
        "shell-more",
        "shortcut-key-*",
        "shell-mode-keys*",
        "shell-mode-text*",
        "terminal-select-text",
        "shell-paste",
    ),
    "shell-conflict": ("shell-back*", "shell-more", "terminal-select-text", "shell-takeover"),
    "shell-close-confirm": (
        "shell-back*",
        "shell-disconnect*",
        "shell-more",
        "shortcut-key-*",
        "shell-mode-keys*",
        "shell-mode-text*",
        "terminal-select-text",
        "shell-paste",
        "shell-close-cancel",
        "shell-close-confirm",
    ),
    # Agent window: header, key row, take-over, workflow switch, composer.
    "agent-composer": (
        "terminal-back*",
        "agent-disconnect*",
        "agent-more*",
        "shortcut-key-*",
        "agent-keyboard-keys*",
        "agent-keyboard-text*",
        "terminal-select-text",
        "agent-paste*",
        "agent-takeover*",
        "agent-workflow-composer*",
        "agent-workflow-direct*",
        "composer-draft",
        "composer-error-dismiss*",
        "composer-send",
    ),
    # Narrow phone layout: content first, navigation bar last.
    "inventory-agents-narrow": (
        "banner-action*",
        "console-search",
        "console-new-agent",
        "host-chip-*",
        "agent-*",
        "inventory-refresh*",
        "inventory-more*",
        *PHONE_ORDER,
    ),
    "inventory-agents-narrow-detail": (
        "banner-action*",
        "detail-back",
        "detail-open",
    ),
    # Login (single primary action, optional re-open).
    "login": ("auth-sign-in", "auth-reopen*"),
    "login-error": ("auth-retry*", "auth-cancel"),
    "shell-chooser": ("shell-choice-*",),
}

__all__ = [
    "DOCK_KEY_ORDER",
    "FOCUS_ORDER",
    "check_focus_order",
    "focus_order",
    "interactive_controls",
    "is_interactive",
    "iter_controls",
]
