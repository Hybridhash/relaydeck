# Install RelayDeck on your Mac

This guide installs the RelayDeck companion on your own Mac so your phone
(a browser on your tailnet) reaches your Herdr sessions. Everything runs
on your Mac; there is no RelayDeck cloud server and nothing is ever public.
For day-to-day operation see [companion operations](companion.md).

## 1. Prerequisites

- **Herdr** installed and running on this Mac (protocol 22; developed against 0.9.1).
  The companion checks compatibility before connecting; it never starts Herdr.
- **Tailscale** installed on this Mac, logged in and connected
  (`tailscale status --json` shows `"BackendState": "Running").
- **MagicDNS and HTTPS enabled** for your tailnet (Tailscale admin console →
  DNS settings). The companion needs your Mac's `*.ts.net` name.
- **Python 3.13** with [`uv`](https://docs.astral.sh/uv/) and a RelayDeck
  checkout with locked dependencies (`uv sync --locked --group dev`;
  Flet is pinned to 1.0.3).

## 2. Install the companion

Inside your RelayDeck checkout:

```sh
uv sync --locked --group dev
uv run python scripts/verify_versions.py
```

The installed entry point is `relaydeck-companion`. Until the package is
installed, the equivalent module path works from the checkout:

```sh
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml doctor
```

## 3. Create the private configuration

Create the owner-only config directory and copy the template:

```sh
mkdir -p ~/.config/relaydeck
chmod 700 ~/.config/relaydeck
cp examples/companion.toml ~/.config/relaydeck/companion.toml
chmod 600 ~/.config/relaydeck/companion.toml
```

Edit `~/.config/relaydeck/companion.toml` (every field below is real; unknown
fields are rejected — see `src/relaydeck/companion/config.py`). Pick one of
the two complete examples — bridge plus web app, or bridge only:

```toml
# Bridge + web app: the Mac also serves the RelayDeck web UI at your
# private address, for a browser on your tailnet.
# Automatic tailnet-only route (recommended): omit advertised_origin and let
# the companion derive https://<your-mac>.<your-tailnet>.ts.net:8445 itself.
tailscale_auto = true
tailscale_https_port = 8445

bind_host = "127.0.0.1"   # loopback only; LAN addresses are refused
port = 8125
host_id = "home-mac"
state_dir = "~/Library/Application Support/RelayDeck"
cookie_secret_file = "~/.config/relaydeck/cookie.secret"
cursor_secret_file = "~/.config/relaydeck/cursor.secret"
connector_credentials = "~/.config/relaydeck/connector.json"
herdr_socket = "~/.config/herdr/herdr.sock"
web_ui = true
```

```toml
# Bridge only, for Android-only users: only the gateway API the Android
# app pairs with is served — no browser UI. `status` reports
# `web_ui`-driven `web_ready: false`.
tailscale_auto = true
tailscale_https_port = 8445

bind_host = "127.0.0.1"
port = 8125
host_id = "home-mac"
state_dir = "~/Library/Application Support/RelayDeck"
cookie_secret_file = "~/.config/relaydeck/cookie.secret"
cursor_secret_file = "~/.config/relaydeck/cursor.secret"
connector_credentials = "~/.config/relaydeck/connector.json"
herdr_socket = "~/.config/herdr/herdr.sock"
web_ui = false
```

Provide the two secret files through a secure channel (0600, each at least
32 bytes, and different from each other). The companion never generates
production credentials and never falls back to environment secrets.

## 4. Choose bridge only or bridge plus web app

- `web_ui = true` (default): the Mac also serves the RelayDeck web app at
  your private address, for a browser on your tailnet.
- `web_ui = false`: bridge only, for Android-only use. Only the gateway API
  the Android app pairs with is served — no browser UI. `status` reports
  `web_ready: false`.

The native Android app (in progress, not yet released) will talk to the gateway API directly and
never needs the web app; until it ships, keep `web_ui = true`.

## 5. Private route (Tailscale)

With `tailscale_auto = true` the companion derives its own
`https://<your-mac>.<your-tailnet>.ts.net:<port>` origin from the local
Tailscale client and creates a **tailnet-only** `tailscale serve` mapping
from that HTTPS port to its loopback backend. The mapping is removed again
on stop, and only a mapping this run created is ever removed
(see `src/relaydeck/companion/tailscale.py`).

Refusals are deliberate, never silent takeovers:

- An HTTPS port with **Funnel** (public) enabled is refused — RelayDeck
  stays tailnet-only. Turn Funnel off for that port first.
- An HTTPS port that already serves something else is refused — pick another
  `tailscale_https_port`; the existing mapping is left untouched.

Prefer port 8445 (the default) unless it is taken. If `tailscale_auto` is
off, set `advertised_origin` to the exact HTTPS address of a Tailscale Serve
mapping you configured yourself.

## 6. Enroll the connector (keygen → issue-enrollment → enroll)

First provision the connector identity on the Mac (copied from
[connector operations](connector.md); the seed never leaves this host):

```sh
uv run relaydeck-connector keygen
uv run relaydeck-connector fingerprint
```

Expected: `keys at <path>`, `fingerprint <SHA256:...>`,
`pubkey <ssh-ed25519 ...>`. Hand the PUBLIC line (never the seed) to the
gateway operator. Never run `keygen --force` unless rotation plus
re-enrollment was explicitly approved — it orphans the current enrollment.

The operator mints the invitation (exact flags from
`relaydeck-gateway issue-enrollment --help`):

```sh
uv run relaydeck-gateway issue-enrollment --host-label "<label>" \
    --pubkey "<ssh-ed25519 line from step 1>" --account owner \
    --origin https://<your-mac>.<your-tailnet>.ts.net:8445 --ttl-seconds 600 \
    --store <state-dir>/enrollments.json
```

Then redeem it from the Mac (both forms verified via `enroll --help`;
`--host` is always required):

```sh
uv run relaydeck-connector enroll --qr "<qr payload>" --host <host-id>
uv run relaydeck-connector enroll --gateway https://<your-mac>.<your-tailnet>.ts.net:8445 \
    --id <enr_...> --code XXXX-XXXX-XXXX --host <host-id>
```

`connector_credentials` in the companion config points at the resulting
private enrollment file. Full key lifecycle is in
[connector operations](connector.md).

## 7. Start, check, stop

```sh
# Diagnostics first (safe: provisions nothing)
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml doctor
# Private HTTPS health probe (certificate-validated, no redirects)
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml doctor --probe-private

# Foreground start; stays attached, Ctrl+C stops it (no service is installed)
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml start --connect-herdr
# In another terminal:
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml status
```

Without `--connect-herdr`, `start` serves setup diagnostics without
attaching Herdr. `--fixture` selects an explicit synthetic source and is
never production proof. `stop` asks the running instance to stop (bounded
`--timeout`, default 12s, max 60s); it never signals a PID. An exclusive
state lock rejects duplicate starts.

## 8. Pair a device

Device pairing is authorized explicitly on the Mac (exact subcommands from
`relaydeck-companion pairing --help`):

```sh
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml pairing open --ttl-seconds 120
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml pairing list
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml pairing approve --request <id> --code <XXX-XXX>
# or: ... pairing deny --request <id> --code <XXX-XXX>
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml pairing close
```

Compare the matching code shown on the device before approving. A companion restart
resets the pairing epoch and any unused approvals. Paired sign-ins survive a
restart only when durable sign-ins are enabled (`RELAYDECK_DURABLE_SIGNINS=1`,
see [companion operations](companion.md)) and the Mac key store is available;
otherwise they are memory-only and the device must pair again. See
[device pairing](device-pairing.md).

## 9. Security notes

- **Tailnet-only, always.** No Funnel, no public URL, no inbound Mac ports;
  the backend binds loopback and Tailscale terminates HTTPS.
- **Secrets stay in `~/.config/relaydeck`** (0700 directory, 0600 files):
  config, cookie/cursor secrets, connector credentials. Never commit them,
  never paste them into chat — use a secure setup channel.
- **Application login is separate** from Mac pairing and Tailscale login;
  Tailscale identity headers are not application auth.
- Uncertain commands and input are never blindly replayed after restart;
  terminal output and leases are ephemeral.

## 10. Production install

One `relaydeck` command installs and runs the companion. Nothing is installed
as a service, and nothing is loaded into launchd, until you say so with `--yes`.

Install the tool once from your checkout (or from a built wheel). The
`companion` extra carries the gateway and web dependencies:

```sh
uv tool install --managed-python --python 3.13 ".[companion]"
# or, from a tagged release:
uv tool install --managed-python --python 3.13 \
  "relaydeck[companion] @ git+https://github.com/Hybridhash/relaydeck@v0.2.0"
# to update a tag-pinned install, repeat the command above with --force and the new tag
relaydeck --version           # prints "relaydeck <version>"
```

Then, in order:

```sh
relaydeck setup               # creates the owner-only config if missing, checks the Herdr
                              # socket, prints the exact tailscale serve command, applies it
                              # only when you confirm, then runs doctor --probe-private
relaydeck web install <dir>   # copies a built web bundle to
                              # ~/Library/Application Support/RelayDeck/web/<build-id>/ after
                              # its repair markers verify on the copy; refuses otherwise
relaydeck web list            # installed bundles; the newest verified one is "current"
relaydeck start --connect-herdr   # foreground; uses the current web bundle unless
                              # FLET_WEB_PATH is set (an explicit value always wins)
```

`relaydeck setup --yes` applies the serve mapping without asking; `--no-probe`
skips the HTTPS probe. Setup never prints secrets and never enrolls the
connector: enrollment stays the one-time [connector runbook](connector.md).

Optional: run the companion at login with launchd (per user, no root, no inbound
ports, loopback bind unchanged):

```sh
relaydeck service install     # prints the LaunchAgent and the launchctl command; writes nothing
relaydeck service install --yes                # writes ~/Library/LaunchAgents/dev.relaydeck.companion.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.relaydeck.companion.plist   # loads it
relaydeck service status      # reads the plist file; never calls launchctl
relaydeck service uninstall --yes --unload    # bootout, then removes the file
```

The agent runs `relaydeck start --connect-herdr` with `RunAtLoad`, `KeepAlive`
and a 30 s throttle. Logs go to `~/Library/Logs/RelayDeck/companion.out.log` and
`companion.err.log`. `service install --load --yes` is the same as the
bootstrap line above.

Pair a device with the short command (the Connect screen shows it too):

```sh
relaydeck pairing open --ttl-seconds 120
relaydeck pairing list
relaydeck pairing approve --code <XXX-XXX>    # approves only when exactly one pending request has this code; otherwise it lists the candidates and approves nothing
relaydeck pairing deny --request <id>
relaydeck pairing close
```

Later: a Homebrew formula or a signed installer package is planned. It is not
implemented; `uv tool install` is the supported path until then.
