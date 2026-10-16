# Mac companion operations

RelayDeck runs on the Herdr Mac
for gateway, authenticated local connector and dynamic Flet web. Cloud/public
web is deferred; Android terminal, auth, revocation, native push and release
gates remain required.

## Configure the private profile

Use Python 3.13, locked gateway/web dependencies and Flet 1.0.3. The existing
Herdr baseline is protocol 22/version 0.9.1. The companion checks compatibility
before opening its connector link; it does not start Herdr.

Copy [the template](../../examples/companion.toml) to
`~/.config/relaydeck/companion.toml` with owner-only permissions (0600).
`advertised_origin` must be the Mac's exact private Tailscale HTTPS DNS origin.
`bind_host` accepts only `127.0.0.1` or `::1`. The connector and server-side web
client use that separate HTTP loopback address; LAN plaintext is refused.
Provide separate cookie/cursor secret files (0600, each at least 32 bytes)
through an approved secure setup channel. Start never generates production
credentials or falls back to environment bearer/secret values.

The private state directory defaults to `~/Library/Application Support/RelayDeck`
(0700). It holds command journals, push data, enrollment invitations, host-token
hashes, revocation requests and lifecycle files. Unsafe existing permissions or
symlinks are refused, not silently repaired. Prompt/notification data requires
private backups; keep this directory outside source control/shared folders.

## Choose bridge only or bridge plus web app

The companion is always the bridge between your devices and Herdr: gateway API
plus the authenticated Herdr connector. The browser UI is an install choice:

- `web_ui = true` (default): the Mac also serves the RelayDeck web app at the
  private address, for use from a browser on your tailnet.
- `web_ui = false`: bridge only, for Android-only use. Only the API the Android
  app pairs with and uses is served; no browser UI, web sessions or Flet web
  runtime start. `status` reports `web_ready: false` and `api_ready`.

Either way, the Android app talks to the gateway API directly and never needs
the web app.

## Foreground start, status and stop

The installed entry point is `relaydeck-companion`. The equivalent module path
works without reinstalling the editable package:

```sh
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml doctor
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml start
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml status
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml stop
```

`start` stays in the foreground with one gateway/Flet serving instance.
Without `--connect-herdr`, it serves setup diagnostics without attaching Herdr.
After approved enrollment, `start --connect-herdr` authorizes this run's local
connector resources. `--fixture` explicitly selects a synthetic source, never
production proof. No launch agent is installed. Do not run historical standalone
gateway/web runners on the companion's port/state directory at the same time.

`status` separates `web_ready`, `connector_ready` and `identity_ready` and shows
the private URL/local endpoint. `pairing_available` reports local pairing
capability; `identity_ready` describes the optional account/provider adapter.
Anonymous terminal access stays locked. A reachable page or health response
does not prove authenticated readiness.

An exclusive state lock rejects duplicate starts. Stop requests carry the
current instance nonce; stop never selects or signals a PID, process name or
port. Startup and cleanup are bounded. An unresponsive owner yields diagnostics,
without a kill fallback. Ctrl+C stops the foreground owner. Herdr, agents and
unrelated listeners survive; only owned attachment clients/subscriptions detach.

## Enrollment, authentication and restart

Follow [connector enrollment](connector.md) with the owner's verified public
key. Operator invitations use `--store <state-dir>/enrollments.json` and
`--origin <advertised-origin>`; the companion reads that same invitation file.
`connector_credentials` points to the resulting private enrollment file. Host ID
and private origin must match. No token is minted to bypass pairing. Restart
explicitly after pairing.

Host-token hashes persist enrollment, rotation and revocation across restart.
Install/connect uses approved Mac-local device pairing; separate account/provider
login remains deferred. Tailscale headers are not application authorization.
Terminal epochs, leases, input and output remain ephemeral. Restart never restores
control, resizes a pane or replays input/uncertain commands.

## Optional durable sign-ins

Default remains in-memory. Proposed operator entry point is implemented as explicit
startup flags, avoiding changes to the configuration/CLI contract. The owner must
first complete the native Keychain acceptance check.
For approved one-time setup and cookie-signing migration:

```sh
RELAYDECK_DURABLE_SIGNINS=1 RELAYDECK_PERSISTENCE_SETUP=1 RELAYDECK_MIGRATE_COOKIE_SECRET=1 \
  .venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml start
```

This imports the exact signing bytes the current configured `cookie_secret_file`
parser uses, including its existing whitespace handling. Import is idempotent;
a mismatch fails closed. Existing cookies keep their signatures, but earlier
in-memory sessions have no durable rows: pair once after first enabling storage.
The plaintext
file is NEVER automatically deleted or replaced. No tokens/secrets go into flags,
logs or command arguments. After setup, omit both one-time flags:

```sh
RELAYDECK_DURABLE_SIGNINS=1 \
  .venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml start
```

One private encrypted store serves gateway verifiers/grants and web bearer pairs.
A verified cookie can restore its identity until the session's current deadline;
restoring never extends it. Sign-in lifetime is sliding: a web session expires after
30 days without use, and each verified live use (the access-token rotation, written in
the same transaction, at most once per hour) moves the deadline to 30 days from then.
At most 90 days after the original pairing a re-pair is required, and the session never
outlives the gateway refresh grant it holds (also 30 days, sliding). The cookie
`Max-Age` is re-issued as the deadline moves. Pending pairing/browser identities
still expire in ten minutes and are not restored. Gateway/web refresh replacements
commit together. Logout/unpair invalidates durable authority and web rows before
success. Locked storage or a failed logout commit returns 503, denies current
access and closes local writers; it keeps the cookie and pending revocation
identity for retry. Unlock/repair storage, then retry logout: the revocation-only
path never grants access and returns success only after deletion commits or a
durable read proves the credential is absent. A 503 is not a completed logout.
Consumed refresh pairs are deleted without permanent history tombstones;
backup-generation invalidation still prevents restoring old credentials.

Unavailable/locked storage does not crash startup: status reports in-memory sign-ins
and the fixed reason. That run uses a fresh volatile cookie signer, so it cannot
bypass Keychain by using the retained plaintext signer or restore old cookies.
Fresh pairing still requires local approval. Do not treat this as durable success.
An active durable instance also denies reads when storage locks; it does not switch
its authority to memory silently. `status` reports `durable_signins`/`signin_storage`.

Backup recovery is an explicit owner operation with all old instances stopped.
Before serving a restored DB, invalidate its authentication generation:

```sh
RELAYDECK_DURABLE_SIGNINS=1 RELAYDECK_INVALIDATE_RESTORED_SIGNINS=1 \
  .venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml start
```

Remove this one-time flag afterward. The Keychain-held generation advances without
rollback and invalidates restored sessions. Do not restore an old host registry
to undo revocation. Draft-only page aliases are identity-scoped; duplicate live
claims fork their namespace. They never restore Flet ownership or control. Draft
storage plumbing defaults to disabled until draft editing is connected. Native/device restart,
refresh/logout/unpair and backup drills remain acceptance gates; this is not a
production-ready release.

## Network and prerequisite diagnostics

[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) shares a
local service within the tailnet and requires HTTPS/access-policy setup. Its
documented foreground command `tailscale serve 8125` displays the private HTTPS
URL. Setup is an explicit owner operation, never performed by the companion.
Funnel/public exposure and persistent network setup remain outside this scope.

`doctor --probe-private` explicitly contacts the configured HTTPS `/healthz`
with certificate validation, no redirects or environment proxy fallback.
Failure points to Mac power/network, Tailscale login, HTTPS Serve and device
access policy. Mac-side success still needs browser and phone proof from mobile
data. A browser without a permitted private route cannot connect.

Herdr offline/incompatible leaves setup diagnostics available. An occupied port
fails startup and cleans owned resources; inspect/change configuration rather
than stopping another listener. Sleep/network changes require resync before
input. Push still needs native/provider integration and an online Mac; no offline
or internet-independent delivery promise is made.

## Verification limits

```sh
.venv/bin/python scripts/verify_deployment_config.py --offline --companion-config ~/.config/relaydeck/companion.toml
.venv/bin/python -m pytest tests/integration/companion/test_companion_lifecycle.py tests/ui/pairing/test_setup_component.py -q
```

Local tests use real process ownership/files and combined ASGI handlers with
explicit server/Herdr doubles. No live daemon, production enrollment, network
setup or persistent service is touched. TCP/private browser/phone access,
sleep/resume and native push remain external acceptance; sandbox limits block
local socket proof. Independent review is required before verified status.

## Install with uv

The companion runs in an isolated uv tool environment, not in system Python or pip.

1. Install uv (https://docs.astral.sh/uv/getting-started/installation/). Tested baseline: 0.12.15.
2. Install a versioned release wheel with its release constraints:

   ```sh
   uv tool install --managed-python --python 3.13 "relaydeck[companion] @ <versioned-wheel>" -c <release-constraints>
   ```

   The runtime must report Python 3.13 (3.13 up to, not including, 3.14). `relaydeck setup` checks this.
3. Run `relaydeck setup`. It first runs read-only prerequisite checks for Tailscale, Herdr, uv and the
   runtime Python. If any check is not ready it prints the fixed guidance for that check, changes
   nothing and makes no Tailscale Serve call, even with `--yes`. Re-run it when the check passes.
4. Update later with `uv tool upgrade relaydeck`. Changing the pinned versions is a deliberate release
   change: install a new versioned wheel with its new constraints, then re-run `relaydeck setup`.

Source checkout alternative: `uv sync --locked`, then `uv run relaydeck setup` (or `uv run
relaydeck-companion doctor`) from the checkout.

Reproducibility: `uv tool install` ignores `uv.lock`. The wheel now carries the companion's runtime
dependencies (declared, with no path dependency) and the two extension packages. Releases still ship
the lock-derived constraints file and the artifact hashes; install with those constraints so the
environment matches the lock.

## Check readiness

`relaydeck setup --check` prints a read-only readiness report: ten items in order (prerequisites,
configuration file, secrets, connector enrollment, web app, companion running, Tailscale Serve, usage
sources, limits, pairing). Each item shows ✓, ! or ✗, a reason and the next step. It writes nothing,
asks nothing and never prints a secret value. Exit status: 0 when everything passes, 1 when an item
fails, 2 when only warnings remain. "Ready to pair" appears only when items 2 to 7 pass.

Without a terminal, plain `relaydeck setup` writes nothing: it prints the pending decisions and exits 1.
Run it in a terminal to answer its prompts.

A usage source (`[[usage.sources]]`) no longer needs its kind in `agent_kinds`. `agent_kinds` only lists
the agents RelayDeck may start.

## Add usage sources

`relaydeck setup` (interactive, in a terminal) offers each installed CLI that RelayDeck can read and that is
not yet a source. For each one it shows the kind, the format, the folder (with `~`), the history it reads
(up to 90 days) and that only token counts are read. It asks `Add <name>? [y/N]` one CLI at a time; the
default is No. Nothing is written for a CLI you decline, and `--yes` never answers these questions.

The approved entries are appended to `[[usage.sources]]`; your comments and the rest of the file are kept.
Before the write, the file is checked against the copy you were shown, a private backup is kept next to it,
and a receipt without secret values is written under `setup-receipts/`. If the file changed while setup was
asking, nothing is written: run setup again.

Undo: `relaydeck setup --undo-sources` removes only the entries setup added that are still unchanged. An
entry you edited is kept.

Without a terminal, setup writes nothing. It lists the pending choices and exits 1.

## Install from Git

Use this to install a published release tag from the public repository.

```sh
uv tool install --managed-python --python 3.13 "relaydeck[companion] @ git+https://github.com/Hybridhash/relaydeck@v0.2.1"
```

From a local clone of the same tag:

```sh
uv tool install --managed-python --python 3.13 ".[companion]"
```

The `companion` extra carries the server packages (FastAPI, uvicorn, the Flet web mount, QR and image
support). Always install it for the companion; the base package alone is the client-side dependency set.

Then run `relaydeck setup` as in step 3 above. The web app is a separate artifact: the wheel does
not contain the Flutter web build, so install a built bundle with `relaydeck web install <dir>`.

## Install as a Herdr plugin

Install the companion first (the section above). The plugin in `plugin/` is a launcher only: it gives
Herdr three popup actions (RelayDeck: Set up, RelayDeck: Pair a device, RelayDeck: Status) and runs the
installed companion commands. It installs nothing and approves no pairing.

The plugin is not published yet; it will ship later as a separate repository. For a local checkout while developing: `herdr plugin link <path-to-clone>/plugin`. If the companion is
missing, the action prints the two install commands above and stops. The plugin's README lists each
action and the uninstall line.

`relaydeck-companion doctor` prints the same prerequisite results as setup. It is read-only.


## Notifications

Web push is the default route for a paired browser or home-screen app. After you sign in, the page
shows "Get notifications" if the browser supports it and permission has not been decided. The
permission prompt appears only after you tap that button.

- iPhone and iPad: Safari shows the page only as a tab, and web push needs the Home Screen app. Use
  Share → Add to Home Screen, open RelayDeck from the Home Screen, then tap "Get notifications".
- The banner shows only while action is needed, and it is hidden for good once this device is
  subscribed. "×" hides it for 30 days. To turn notifications off, open Settings › Notifications and tap
  "Turn off"; this removes the subscription on the companion for this device.
- Settings: `[notifications]` in `companion.toml`. `web_push = false` turns it off. `vapid_subject` must be
  a routable `https:` URL (a `mailto:` address is also accepted); the default is `https://example.com/relaydeck`. Push
  services reject placeholder domains such as `.invalid` and `localhost`. The default lock
  screen text is "An agent needs attention" or "An agent finished". `lock_screen_detail = true` adds the
  host name.
- Privacy: the browser's push service receives delivery metadata (when, and which subscription) and an
  encrypted message. RelayDeck's message holds only the generic text and an opaque route token. It never
  includes terminal text, paths, commands or prompts.
- Rotate the web push key: stop the companion, then run `relaydeck-companion push web-rotate` in a
  terminal. Type `rotate web push` when asked. This turns sending off, removes every web subscription and
  queued message, and replaces the key. Each device must tap "Get notifications" again. There is no
  `--yes` for this command.
- Failed sends: each failed send logs one WARNING with the provider host, the HTTP status and the
  provider's short reason word (for example `BadJwtToken`). Endpoints, keys and payloads are never
  logged. `relaydeck-companion setup --check` item 10 (Notifications) counts queued and retrying messages.
  Messages are retried for one hour and then dropped.
- Banner missing: the device is not signed in, permission was already decided (check the browser's site
  settings), or push is unsupported. Nothing is sent until a device subscribes.
