"""Shared components: property-level equivalence with today's hand-built code.

The reference trees below are transcribed from the hand-built code they replaced:
composer/components.py::_framed, connect.py::_panel_button, the settings
header Column in settings/components.py, and the shell "Connecting…" Text.
"""

from __future__ import annotations

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO
from relaydeck.client.components.design_system.primitives import (
    ActionButton,
    SectionHeader,
    text_style,
)
from relaydeck.client.components.design_system.surfaces import (
    MutedText,
    SurfaceFrame,
    frame_border,
)
from relaydeck.client.theme import SURFACE_RADIUS, palette

TOKENS = palette(True)


def _border(tokens: dict[str, str]) -> ft.Border:
    side = ft.BorderSide(width=1, color=tokens["line"])
    return ft.Border(left=side, top=side, right=side, bottom=side)


def test_frame_border_matches_hand_built() -> None:
    assert frame_border(TOKENS) == _border(TOKENS)


def test_surface_frame_matches_hand_built() -> None:
    content = ft.Text("body")
    frame = SurfaceFrame("frame-key", content, tokens=TOKENS)
    reference = ft.Container(
        key="frame-key",
        bgcolor=TOKENS["surface"],
        border=_border(TOKENS),
        border_radius=SURFACE_RADIUS,
        padding=12,
        content=content,
    )
    assert isinstance(frame, ft.Container)
    assert frame.key == reference.key
    assert frame.bgcolor == reference.bgcolor
    assert frame.border == reference.border
    assert frame.border_radius == reference.border_radius == 8
    assert frame.padding == reference.padding
    assert frame.content is content
    # Composer/common never passed disabled; the shell passed False explicitly.
    # SurfaceFrame always passes it, so compare the resolved attribute.
    assert frame.disabled == reference.disabled is False
    assert SurfaceFrame("k", content, tokens=TOKENS, disabled=True).disabled is True


def test_muted_text_matches_hand_built() -> None:
    text = MutedText("Connecting…", key="shell-connecting", tokens=TOKENS)
    reference = ft.Text("Connecting…", key="shell-connecting", color=TOKENS["muted"], size=13)
    assert isinstance(text, ft.Text)
    assert text.value == reference.value
    assert text.key == reference.key
    assert text.color == reference.color
    assert text.size == reference.size
    assert MutedText("x", tokens=TOKENS).key is None


def test_section_header_matches_settings_header() -> None:
    header = SectionHeader("Saved on this device", "Settings")
    reference = ft.Column(
        key="settings-header",
        spacing=0,
        controls=[
            ft.Text(
                "Saved on this device",
                style=text_style(FAMILY_MONO, T.TYPE_EYEBROW, T.TEXT_MUTED, 0.06),
            ),
            ft.Text(
                "Settings",
                key="destination-title",
                style=text_style(FAMILY_INTERFACE, T.TYPE_TITLE, T.TEXT, -0.02),
            ),
        ],
    )
    assert isinstance(header, ft.Column)
    assert header.key == reference.key == "settings-header"
    assert header.spacing == 0
    assert len(header.controls) == 2
    eyebrow, title = header.controls
    ref_eyebrow, ref_title = reference.controls
    assert eyebrow.value == ref_eyebrow.value
    assert eyebrow.style == ref_eyebrow.style
    assert title.value == ref_title.value
    assert title.key == ref_title.key == "destination-title"
    assert title.style == ref_title.style
    assert eyebrow.key is None


def _expected_button_style(primary: bool) -> ft.ButtonStyle:
    return ft.ButtonStyle(
        padding=0,
        shape=ft.RoundedRectangleBorder(radius=26),
        bgcolor=T.PRIMARY_FILL if primary else ft.Colors.TRANSPARENT,
        overlay_color={
            ft.ControlState.DEFAULT: ft.Colors.TRANSPARENT,
            ft.ControlState.FOCUSED: T.FOCUS_WASH,
        },
        side={
            ft.ControlState.DEFAULT: (
                ft.BorderSide(1, T.PRIMARY_TEXT)
                if not primary
                else ft.BorderSide(3, ft.Colors.TRANSPARENT)
            ),
            ft.ControlState.FOCUSED: ft.BorderSide(3, T.TEXT),
        },
    )


def test_action_button_matches_panel_button() -> None:
    for primary in (True, False):
        for disabled in (False, True):
            pressed: list[int] = []

            def record_press(pressed: list[int] = pressed) -> None:
                pressed.append(1)

            button = ActionButton(
                "Begin pairing",
                key="connect-begin",
                on_press=record_press,
                primary=primary,
                disabled=disabled,
            )
            assert isinstance(button, ft.TextButton)
            assert button.key == "connect-begin"
            assert button.disabled is disabled
            assert button.style == _expected_button_style(primary)
            content = button.content
            assert isinstance(content, ft.Container)
            assert content.height == 52
            assert content.alignment == ft.Alignment.CENTER
            label = content.content
            assert isinstance(label, ft.Text)
            assert label.value == "Begin pairing"
            assert label.style == text_style(
                FAMILY_INTERFACE,
                {"size": 16, "weight": 700, "height": 1.2},
                T.ON_PRIMARY if primary else T.PRIMARY_TEXT_LIGHT,
            )
            # Same lambda shape as today's _panel_button: event in, on_press() out.
            button.on_click(object())
            assert pressed == [1]
