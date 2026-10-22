"""Development-only fixtures (not part of the packaged app).

The dev web server lives here, outside `relaydeck.client`, so the native
packager (which stages only the client, contracts and domain layers) never
ships it. Production client code must not import it (`tests/architecture`
enforces it).
"""
