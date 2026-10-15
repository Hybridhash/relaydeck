"""One bundled asset root for the packaged entry point and private web app."""

from pathlib import Path

CLIENT_ASSETS = Path(__file__).resolve().parent / "assets"
