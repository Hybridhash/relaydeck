"""Paired Flet 1.0.3 themes and a lifecycle-safe appearance bridge."""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, page_fonts
from relaydeck.client.theme import FONT_SIZE, MIN_TOUCH, TEXT_FONT_FALLBACK, palette


class AppearanceBridge:
    """Mirror page brightness without remounting the connected application."""

    def __init__(self, dark: bool) -> None:
        self.dark = dark
        self._listeners: list[Callable[[bool], None]] = []

    def set_dark(self, dark: bool) -> None:
        if self.dark == dark:
            return
        self.dark = dark
        for listener in tuple(self._listeners):
            listener(dark)

    def subscribe(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe


def chrome_style(dark: bool, *, primary: bool = False) -> ft.ButtonStyle:
    """Semantic controls with paired states and a visible keyboard focus edge."""
    t = palette(dark)
    return ft.ButtonStyle(
        color={
            ft.ControlState.DEFAULT: t["on_accent"] if primary else t["text"],
            ft.ControlState.DISABLED: t["disabled_text"],
        },
        bgcolor={
            ft.ControlState.DEFAULT: t["accent"] if primary else t["raised"],
            ft.ControlState.HOVERED: t["accent"] if primary else t["active_row_bg"],
            ft.ControlState.FOCUSED: t["accent"] if primary else t["active_row_bg"],
            ft.ControlState.DISABLED: t["raised"],
        },
        overlay_color=ft.Colors.TRANSPARENT,
        side={
            ft.ControlState.DEFAULT: ft.BorderSide(1, t["outline"]),
            ft.ControlState.FOCUSED: ft.BorderSide(
                2, t["on_accent"] if primary else t["accent_text"]
            ),
        },
        elevation=0,
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        shape=ft.RoundedRectangleBorder(radius=8),
        text_style=ft.TextStyle(
            size=FONT_SIZE["base"],
            weight=ft.FontWeight.W_500,
            font_family_fallback=list(TEXT_FONT_FALLBACK),
        ),
        icon_size=20,
    )


def build_theme(dark: bool) -> ft.Theme:
    """Explicit roles prevent a light label inheriting onto a dark panel."""
    t = palette(dark)
    text = ft.TextStyle(size=14, color=t["text"], font_family_fallback=list(TEXT_FONT_FALLBACK))
    meta = ft.TextStyle(size=12, color=t["muted"], font_family_fallback=list(TEXT_FONT_FALLBACK))
    return ft.Theme(
        font_family=FAMILY_INTERFACE,
        use_material3=True,
        color_scheme=ft.ColorScheme(
            primary=t["accent"],
            on_primary=t["on_accent"],
            primary_container=t["active_row_bg"],
            on_primary_container=t["text"],
            secondary=t["accent"],
            on_secondary=t["on_accent"],
            secondary_container=t["active_row_bg"],
            on_secondary_container=t["text"],
            tertiary=t["amber"],
            on_tertiary=t["surface"],
            surface=t["surface"],
            on_surface=t["text"],
            on_surface_variant=t["muted"],
            surface_container=t["surface"],
            surface_container_low=t["surface"],
            surface_container_high=t["raised"],
            surface_container_highest=t["raised"],
            surface_container_lowest=t["bg"],
            surface_dim=t["bg"],
            surface_bright=t["raised"],
            surface_tint=ft.Colors.TRANSPARENT,
            outline=t["outline"],
            outline_variant=t["line"],
            error=t["danger"],
            on_error=t["surface"],
            error_container=t["error_bg"],
            on_error_container=t["danger"],
        ),
        text_theme=ft.TextTheme(
            body_large=ft.TextStyle(
                size=16,
                color=t["text"],
                font_family_fallback=list(TEXT_FONT_FALLBACK),
            ),
            body_medium=text,
            body_small=meta,
            title_large=ft.TextStyle(
                size=22,
                color=t["text"],
                weight=ft.FontWeight.W_600,
                font_family_fallback=list(TEXT_FONT_FALLBACK),
            ),
            title_medium=ft.TextStyle(
                size=15,
                color=t["text"],
                weight=ft.FontWeight.W_600,
                font_family_fallback=list(TEXT_FONT_FALLBACK),
            ),
            title_small=text,
            label_large=text,
            label_medium=meta,
            label_small=meta,
        ),
        button_theme=ft.ButtonTheme(style=chrome_style(dark)),
        outlined_button_theme=ft.OutlinedButtonTheme(style=chrome_style(dark)),
        text_button_theme=ft.TextButtonTheme(style=chrome_style(dark)),
        filled_button_theme=ft.FilledButtonTheme(style=chrome_style(dark, primary=True)),
        icon_button_theme=ft.IconButtonTheme(style=chrome_style(dark)),
        icon_theme=ft.IconTheme(color=t["muted"], size=20),
        navigation_rail_theme=ft.NavigationRailTheme(
            bgcolor=t["surface"],
            elevation=0,
            label_type=ft.NavigationRailLabelType.ALL,
            indicator_color=t["active_row_bg"],
            indicator_shape=ft.RoundedRectangleBorder(radius=8),
            selected_label_text_style=ft.TextStyle(
                size=11,
                color=t["accent_text"],
                font_family_fallback=list(TEXT_FONT_FALLBACK),
            ),
            unselected_label_text_style=ft.TextStyle(
                size=11,
                color=t["muted"],
                font_family_fallback=list(TEXT_FONT_FALLBACK),
            ),
        ),
        navigation_bar_theme=ft.NavigationBarTheme(
            bgcolor=t["surface"],
            elevation=0,
            height=64,
            indicator_color=t["active_row_bg"],
            indicator_shape=ft.RoundedRectangleBorder(radius=8),
            label_text_style={
                ft.ControlState.DEFAULT: ft.TextStyle(
                    size=11,
                    color=t["muted"],
                    font_family_fallback=list(TEXT_FONT_FALLBACK),
                ),
                ft.ControlState.SELECTED: ft.TextStyle(
                    size=11,
                    color=t["accent_text"],
                    font_family_fallback=list(TEXT_FONT_FALLBACK),
                ),
            },
        ),
        list_tile_theme=ft.ListTileTheme(
            text_color=t["text"],
            icon_color=t["muted"],
            selected_color=t["text"],
            selected_tile_color=t["active_row_bg"],
            min_height=MIN_TOUCH,
        ),
        scaffold_bgcolor=t["bg"],
        canvas_color=t["bg"],
        divider_color=t["line"],
        hint_color=t["muted"],
        disabled_color=t["disabled_text"],
        focus_color=t["active_row_bg"],
        hover_color=t["raised"],
    )


def configure_page_theme(
    page: ft.Page, *, mode: ft.ThemeMode = ft.ThemeMode.DARK
) -> AppearanceBridge:
    """Ship the approved purple design, with explicit light/system opt-in.

    OS brightness must not silently select the legacy presentation on first
    launch. System-mode consumers still receive lifecycle-safe live updates.
    """
    page.theme_mode = mode
    page.fonts = {**(page.fonts or {}), **page_fonts()}
    page.theme = build_theme(False)
    page.dark_theme = build_theme(True)
    page.padding = 0
    page.spacing = 0
    page.bgcolor = ft.Colors.SURFACE_CONTAINER_LOWEST
    bridge = AppearanceBridge(
        mode == ft.ThemeMode.DARK
        or (mode == ft.ThemeMode.SYSTEM and page.platform_brightness == ft.Brightness.DARK)
    )

    def on_brightness(event: ft.Event[ft.Page]) -> None:
        if page.theme_mode != ft.ThemeMode.SYSTEM:
            return
        # Flet1.0.3 declares a typed payload, but its carrier sends a scalar
        # after updating platform_brightness. Scalar dispatch yields Event.
        for value in (getattr(event, "brightness", None), event.data, page.platform_brightness):
            try:
                brightness = ft.Brightness(value)
            except (TypeError, ValueError):
                continue
            bridge.set_dark(brightness == ft.Brightness.DARK)
            return

    page.on_platform_brightness_change = on_brightness
    return bridge
