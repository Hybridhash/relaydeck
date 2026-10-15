"""Screens reference the key and pane radii by name; changing either is a visible decision."""

from relaydeck.client.components.design_system import tokens as T


def test_key_and_pane_radii_keep_their_values():
    assert T.RADIUS_KEY == 12
    assert T.RADIUS_PANE == 24
