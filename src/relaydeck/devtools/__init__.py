"""Development-only fixtures (not part of the packaged app).

The fake gateway/host/terminal adapters and the named UI scenarios live here,
outside `relaydeck.client`, so the native packager (which stages only the
client, contracts and domain layers) never ships them. Tests, the UI scenario
script and the dev web server import them from this package; production client
code must not (`tests/architecture` enforces it).
"""
