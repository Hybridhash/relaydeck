"""Flet entry point: `flet run --web src/relaydeck/client/main.py`."""

import flet as ft

from relaydeck.client.app import main
from relaydeck.client.asset_paths import CLIENT_ASSETS

if __name__ == "__main__":
    ft.run(main, assets_dir=str(CLIENT_ASSETS))
