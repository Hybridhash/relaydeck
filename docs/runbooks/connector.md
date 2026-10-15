# Connector runbook

The selected primary release is the [Mac companion](companion.md), which owns
the local connector with gateway/Flet in one foreground workflow. Existing
enrollment and protocol rules below still apply. Standalone `run --local-gateway
http://127.0.0.1:8125` explicitly selects same-Mac transport while retaining the
enrolled private HTTPS origin. Never use this flag for a LAN endpoint or run a
second connector alongside the companion. Persistent installation is not part
of this release; cloud/public web is deferred.

Install, enroll, operate, upgrade, and recover the home-Mac connector
(the process that journals gateway intents and executes them against the
local Herdr daemon).

Consequential steps are labeled throughout, no exceptions:

- `PREPARATION` -- safe to perform while planning: reading state,
  staging files, rehearsing against synthetic data. No credentials are
  created, changed, or destroyed; no service is installed or started.
- `REQUIRES-OWNER-APPROVAL` -- the owner explicitly authorizes each
  execution: anything that mints, rotates, or wipes keys, enrolls or
  revokes the host, opens network behavior, or installs/persists a
  service. Persistent macOS service installation is ALWAYS in this
  class.

Every command, flag, and path below was verified against this tree (see
"Verification" at the end). System tools (`launchctl`, `cp`, `chmod`,
`ls`, `grep`) are marked `(sys)`; everything else is a repo entry
point. Placeholders look like `<angle-brackets>`.

## 0. Layout (verified paths)

| Path | Content | Mode |
| --- | --- | --- |
| `~/.config/relaydeck/connector_key.json` | Ed25519 seed (`seed_b64`) + OpenSSH public line | 0600, atomic write (`DEFAULT_KEY`, `_write_private`) |
| `~/.config/relaydeck/connector.json` | `gateway_url`, `host_id`, `host_token` | 0600, atomic write (`DEFAULT_CREDS`) |
| `~/.config/herdr/herdr.sock` | Local Herdr daemon socket (Herdr's file, not ours) | daemon-owned |
| Connector journal file | Gateway-intent journal (`CommandJournal`, same sqlite core as the gateway) | 0600, WAL (`src/relaydeck/connector/journal.py`, default `~/.config/relaydeck/connector-journal.db`) |

Entry point: `uv run relaydeck-connector --help` (verified
subcommands: `keygen`, `fingerprint`, `enroll`, `status`, `revoke`,
`run`). There are no other connector flags: anything else you remember
from another tool does not exist here.

## 1. OS account

- `PREPARATION` Run the connector as a dedicated macOS user (or at
  minimum a dedicated login session) whose `~/.config/relaydeck/` no
  other user can read. Keys are file-based 0600, NOT Keychain items:
  whoever can read the account's home can copy the seed, so full-disk
  encryption and a locked screen are part of the posture, stated
  plainly.
- `PREPARATION` Confirm the account umask cannot leak new files
  (`umask` (sys) should print `077` or stricter for this user; the app
  forces 0600 itself, but operator copies such as backups must not be
  lax -- always `chmod 600` (sys) after copying credential files).
- `REQUIRES-OWNER-APPROVAL` Creating the account, granting it login or
  `sudo` rights, or moving existing keys to it. Do not assume a locked
  or rebooted Mac can unlock anything unattended: file keys are
  available at boot to whoever holds the disk credentials, which is
  exactly why the account and the disk encryption are owner decisions.

## 2. Install and Herdr compatibility

- `PREPARATION` Install the tree and the locked dependencies, then prove
  the pinned versions:

```sh
uv sync --locked
uv run python scripts/verify_versions.py
uv run relaydeck-connector --help
```

- `PREPARATION` Confirm the local Herdr daemon the connector will
  drive. The connector speaks Herdr protocol 22 only
  (`EXPECTED_PROTOCOL` in `src/relaydeck/connector/herdr.py`) over the
  Unix socket `~/.config/herdr/herdr.sock` (`DEFAULT_SOCKET`), one
  request per connection, from a fixed allowlist of 18 verified method
  names (`ALLOWED_METHODS` -- `ping`, `session.snapshot`, `agent.*`,
  `pane.*`, `tab.create/close`, `workspace.create/close`,
  `events.subscribe`). Anything else is refused. Rehearsed check
  (imports verified by execution):

```sh
ls -l ~/.config/herdr/herdr.sock   # (sys) socket present, daemon-owned
uv run python -c "from relaydeck.connector.herdr import EXPECTED_PROTOCOL, ALLOWED_METHODS, resolve_socket;
print(EXPECTED_PROTOCOL, len(ALLOWED_METHODS), resolve_socket())"
```

Expected: `22 18 ~/.config/herdr/herdr.sock`. The pinned
baseline is Herdr 0.9.1 / protocol 22; any
other daemon version reopens the compatibility ledger before this connector is trusted
against it -- check the daemon's own version through Herdr's external
tooling (`herdr api schema --json`, cited by the compatibility ledger;
Herdr's CLI, not this repo's) and compare against the ledger.

## 3. Key provisioning

- `REQUIRES-OWNER-APPROVAL` Provision the connector identity (creates
  the seed; the seed never leaves this host):

```sh
uv run relaydeck-connector keygen
```

Expected output: `keys at <path>`, `fingerprint <SHA256:...>`, `pubkey
<ssh-ed25519 ...>`. The public line is what the owner binds the
invitation to (section 4).

- `PREPARATION` Re-print the fingerprint at any time (read-only):

```sh
uv run relaydeck-connector fingerprint
```

- `REQUIRES-OWNER-APPROVAL` `keygen --force` REPLACES the identity. On
  an enrolled host this orphans the current enrollment (the gateway
  still binds the old public key) and requires re-enrollment from
  scratch. Never run with `--force` unless the owner has approved both
  the rotation and the re-enrollment window.

## 4. Enrollment (owner ceremony)

Enrollment binds one invitation to one account, one connector public
key, and one proof of seed possession. The invitation code is single-use
with a 600 s default TTL and 5 code-guess attempts before burn
(`EnrollmentStore`, `src/relaydeck/gateway/identity.py`).

1. `PREPARATION` Read the connector fingerprint (section 3) and hand
   the PUBLIC line (never the seed) to the gateway operator over an
   authenticated channel.
2. `REQUIRES-OWNER-APPROVAL` The gateway operator mints the invitation
   into the shared store the serving gateway was given (flags verified
   via `--help`; `--store` must be the same file as the server's
   `--enrollment-store`.
   Without `--store` the invitation prints but can never redeem: the
   command warns `single-process demo store` on stderr instead of
   persisting):

```sh
uv run relaydeck-gateway issue-enrollment --host-label "<label>" \
    --pubkey "<ssh-ed25519 line from step 1>" --account owner \
    --origin https://<approved-host> --ttl-seconds 600 \
    --store /var/lib/relaydeck/enrollments.json
```

Expected: `id <enr_...>`, `code XXXX-XXXX-XXXX`, `fingerprint
<SHA256:...>`, `qr <payload>` on stdout (plus an ASCII QR when the
gateway group is installed) and `persisted to <file> (0600 shared
store)` on stderr. The store file is 0600 JSON holding the invitation
(`version`, `items` keyed by invitation id).

3. `PREPARATION` Compare the printed fingerprint against the
   connector's `fingerprint` output. On ANY mismatch: stop, do not
   enroll, treat the invitation as suspect (drill 3).
4. `REQUIRES-OWNER-APPROVAL` Redeem from the Mac (both forms
   verified via `enroll --help`; `--host` is always required):

```sh
uv run relaydeck-connector enroll --qr "<qr payload>" --host <host-id>
uv run relaydeck-connector enroll --gateway https://<approved-host> \
    --id <enr_...> --code XXXX-XXXX-XXXX --host <host-id>
```

Expected: `enrolled <host-id> [<fingerprint>]`. The connector aborts
unless the gateway echoes the local fingerprint back
(`claim_enrollment` mutual verification) -- a different echo fails the
ceremony without writing credentials.

5. `PREPARATION` Confirm enrollment state (read-only; exit 1 when not
   enrolled):

```sh
uv run relaydeck-connector status
```

Expected: `host=<id> gateway=<url> enrolled=yes fp=<SHA256:...>`.

## 5. Gateway pinning and TLS

- The enrolled `gateway_url` is pinned in `connector.json` at redeem
  time. There is no flag to override it per-run (verified: `run` takes
  `--herdr-socket`, `--journal-path`, `--fixture`, `--live-herdr`,
  `--once`, `--once-timeout`, `--max-retries`, `--log-config`, and the
  first-run redeem helpers `--qr`/`--gateway`/`--host`/`--id`/`--code`
  -- no gateway override), so a connector cannot be silently redirected
  to a lookalike gateway by a stray argument. The `--gateway` flag is
  honored ONLY when no credentials exist yet (first-run redeem); an
  enrolled connector ignores it.
- TLS to the gateway uses the HTTP client's default verification
  (verified: the enroll path builds `httpx.AsyncClient` with no custom
  trust and no `--insecure`-style flag exists in the CLI). Never
  downgrade this; a certificate error is a stop-and-investigate event,
  not a flag to add.
- `REQUIRES-OWNER-APPROVAL` Changing gateway (new URL, new account, or
  new host id) = `revoke` locally (section 8) + fresh invitation +
  re-enroll (section 4). Re-enrolling an existing host id rotates its
  token and kills the old one immediately (gateway behavior; rehearsed
  in `test_host_rotation_kills_old_token`).

## 6. Outbound network requirements

- `PREPARATION` The connector initiates everything outbound: HTTPS to
  `gateway_url` for enrollment, then one outbound WebSocket to
  `wss://<approved-host>/v1/connectors` for the tunnel (single live
  tunnel per host; a new tunnel replaces a stale one). REQUIRED egress:
  TCP 443 to the approved gateway host only.
- The connector listens on NOTHING: no inbound ports, no local HTTP
  surface, no SSH server. Home firewalls need no inbound rule, and
  adding one is out of scope for this product.
- On sleep/wake or network change the tunnel drops; queued gateway
  commands wait out their TTL and then settle (accepted-never-dispatched
  prove non-execution and expire; see drill 4). The connector never
  replays on wake -- dispatch markers and UUID dedupe forbid it.

## 7. Service start, stop, and the persistent-service gate

Wiring state (read before operating). `relaydeck-connector run` is a
real link command in this tree: it opens the authenticated
`/v1/connectors` tunnel (header auth), pushes a snapshot plus seed
events, receives `command.dispatch` intents, verifies the digest,
journals via `ConnectorDispatch`, and reports `command.ack` /
`command.result` (`src/relaydeck/connector/link.py`). With a live
source it additionally streams post-bootstrap upstream events and PTY
bytes every poll tick (see "Live relay" below). Reconnect uses seeded
exponential backoff; a rejected credential exits instead of retrying;
repeated initial-push transport failures trip a circuit breaker
(`push-failed` exit, 5 consecutive by default); SIGINT/SIGTERM shuts
down gracefully (`bye`, socket close, journal close). Exit codes
(verified live): 0 clean shutdown or `--once` done, 1 runtime failure
(not enrolled, credential dead, retries exhausted, source failure,
push-failed, snapshot-too-large), 2 usage error. There are no
launchd/plist/unit files in the tree (verified by search).

Source selection (safety-critical). The link runs against the
`LinkSource` port with exactly one source flag: `--fixture` (scripted
panes/agents, `src/relaydeck/connector/fixture.py`, never contacts
Herdr) or `--live-herdr` (production `HerdrSource` against the daemon
at `--herdr-socket`). `run` with neither or both refuses with exit 2.
`--live-herdr` additionally requires owner authorization in the
environment (`RELAYDECK_ALLOW_LIVE_HERDR=1`); without it the CLI
refuses with exit 2 and never opens the Herdr socket. The live path
bootstraps (protocol-22 gate, subscribe, snapshot, per-pane status
subscriptions) before the first tunnel session; a bootstrap failure
exits 1 without contacting the gateway. Every behavior below is proven
against a scripted Unix-socket daemon plus loopback gateway
(`tests/integration/test_herdr_live_relay.py`); the tests never touch
the live user daemon.

Live relay (live source only). Each poll tick the link drains
translated upstream events (`event` tunnel messages) and pumps
attached PTY bytes (`terminal.output` frames, 32 KiB raw chunks so the
Base64 envelope stays below the 64 KiB text-frame cap) into the
tunnel. PTY routing follows the gateway epoch table: the link consumes
`terminal.epochs` tunnel messages and the optional hello `epochs`
field, auto-attaches newly mapped terminals (shell without takeover,
agent with explicit takeover), and holds unmapped bytes in the PTY
until an epoch arrives (never guessed). The gateway now distributes a
complete account-bound table in hello and on attachment changes. Missing
entries detach local taps; changed epochs replace them. A dead tap requests
an explicit gateway reset, invalidates old downstream leases and recovers
under the new table without replaying input. Upstream death
mid-session drops the tunnel and rebootstraps (new epoch, fresh
subscriptions, re-push); the rebootstrap also sweeps attach sessions
loudly (epochs kept) and the next gateway epochs respawn each mapped
terminal -- output resumes, terminal input is never replayed. Dead
attach clients found mid-session are reaped the same way (loud drop,
epoch kept, respawn on next epochs). Gateway reconnects resync content
under the same epoch. Per-pane `agent_status_changed` subscriptions
open post-snapshot and track live pane creation/closure; a pane-sub
failure degrades status freshness with a bounded 30 s + up-to-5 s
jitter backoff (honored even with no subscription held, so a sick
daemon sees no hot spin), never the link.

Snapshot size guard (M1). The snapshot frame is measured BEFORE
sending against the 64 KiB tunnel bound. Over-bound snapshots go
chunked (`snapshot.begin`/`snapshot.chunk`/`snapshot.end`,
sha256-verified) ONLY when the gateway hello advertised
`snapshot_chunks` intake; otherwise the run exits `snapshot-too-large`
(exit 1) with the byte counts in the log -- fail loudly, never churn.
Gateway intake is implemented: one transfer per tunnel, up to 1 MiB/128
ordered chunks, exact byte count and SHA-256, with a 10-second deadline.
Content is validated against the frozen snapshot schema and host identity
before replacing metadata. Same-epoch updates preserve leases; an upstream
continuity change resets affected stream generations. Over-bound or corrupt
transfers fail closed.

- `PREPARATION` Prove the plumbing and argument shape (exact argv):

```sh
uv run relaydeck-connector run --help
HOME=/tmp/rd-smoke-home uv run relaydeck-connector run --fixture --once
echo "exit=$? (expect 1: isolated HOME is not enrolled)"
```

- `PREPARATION` Loopback smoke of the full enroll/tunnel/dispatch
  path (synthetic fixtures only; boots real loopback servers):

```sh
uv run pytest tests/integration/test_connector_link.py tests/integration/test_gateway_loopback.py tests/integration/test_herdr_live_relay.py -q
```

  Expect all tests to pass (8 link + 6 loopback + 18 live relay). The
  suites enroll with real crypto over loopback HTTP, push fixture and
  translated-Herdr snapshots/events over the real tunnel, stream a
  post-bootstrap upstream event and live PTY bytes into the gateway,
  serve a dispatch, and exercise reconnect-after-restart, rebootstrap,
  dead-attach reap/respawn with zero input replay, pane-sub backoff,
  and the snapshot/breaker guards.

- `PREPARATION` Single-session run against an enrolled gateway
  (push state, serve one dispatch, exit 0):

```sh
uv run relaydeck-connector run --fixture --once --journal-path ~/.config/relaydeck/connector-journal.db
```

- `REQUIRES-OWNER-APPROVAL` Single-session LIVE run against the real
  daemon (owner authorizes each execution: this contacts Herdr and
  executes gateway intents against it):

```sh
RELAYDECK_ALLOW_LIVE_HERDR=1 uv run relaydeck-connector run --live-herdr --once \
    --journal-path ~/.config/relaydeck/connector-journal.db
```

  Without the environment variable the command refuses (exit 2) and
  never opens the Herdr socket. A bootstrap failure (wrong protocol,
  dead socket) exits 1 before any gateway contact.

- `PREPARATION` Long-running link with logging config (`.json` =
  dictConfig, anything else = stdlib fileConfig):

```sh
uv run relaydeck-connector run --fixture --log-config /etc/relaydeck/connector-logging.json
```

- `REQUIRES-OWNER-APPROVAL` Installing the connector as a persistent
  macOS service (owner-authored `launchd` agent plist + `launchctl
  bootstrap` (sys)) is a HUMAN-APPROVED deployment step,
  executed only after the owner approves the exact unit file, log
  paths, and restart policy. Preparation (writing and reviewing the
  plist, staging log directories, rehearsing `launchctl print` (sys)
  queries) never implies approval to `bootstrap` it. The unit must run
  the connector as the section-1 account, keep stdout/stderr in
  approved log files with query-token redaction applied at the
  gateway, and `KeepAlive` only after wake-from-sleep tunnel
  re-establishment is accepted. The unit
  must pass `--fixture` until live Herdr contact is authorized; the
  authorized live unit passes `--live-herdr` AND sets
  `RELAYDECK_ALLOW_LIVE_HERDR=1` in its environment (the binary refuses
  live runs without both).
- `REQUIRES-OWNER-APPROVAL` Starting, stopping, or restarting the
  approved service once installed (`launchctl kickstart -k` /
  `launchctl bootout` (sys) against the approved target). Stopping
  mid-dispatch is safe by construction: the connector journals every
  intent before touching Herdr, so a kill reconciles to `unknown`,
  never to a blind retry (`src/relaydeck/connector/journal.py`).

## 8. Safe upgrade and credential rotation

- `PREPARATION` Stage the upgrade: fetch the new tree, then back up
  the two credential files (sys; `chmod 600` after every copy):

```sh
cp -p ~/.config/relaydeck/connector_key.json ~/relaydeck-backup/connector_key-<utc-stamp>.json   # (sys)
cp -p ~/.config/relaydeck/connector.json ~/relaydeck-backup/connector-<utc-stamp>.json           # (sys)
chmod 600 ~/relaydeck-backup/*.json                                                             # (sys)
```

- `REQUIRES-OWNER-APPROVAL` Stop the service (section 7), `uv sync
  --locked`, `uv run python scripts/verify_versions.py`, then confirm
  the identity survived the upgrade byte-identical:

```sh
uv run relaydeck-connector fingerprint   # must equal the pre-upgrade value
uv run relaydeck-connector status        # host/gateway/enrolled unchanged
```

- `REQUIRES-OWNER-APPROVAL` Start the service and watch the first
  tunnel establishment (gateway logs show the tunnel hello; a `1008
  unauthenticated` close means the credential, not the network -- see
  drill 7). Any fingerprint change, enrollment loss, or protocol-22
  mismatch is a rollback: stop, restore the section-8 backups, re-run
  `status`, and report.
- `REQUIRES-OWNER-APPROVAL` Local credential wipe (compromise or
  decommission; this deletes `connector.json` only -- keys survive):

```sh
uv run relaydeck-connector revoke
```

Expected: `local credentials wiped (also revoke at the gateway)` (or
`not enrolled`, exit 0 either way). Local wipe NEVER stands alone:
pair it with the gateway-side revoke in drill 3/7, otherwise the host
token stays live server-side.
- `REQUIRES-OWNER-APPROVAL` Full identity rotation: `revoke`, then
  `keygen --force`, then a fresh section-4 ceremony. The old public key
  is dead the moment the new invitation binds the new one; keep the old
  gateway enrollment revoked (drill 3), not merely abandoned.

## 9. Connector journal purge (m1)

The connector journal (`~/.config/relaydeck/connector-journal.db`,
`CommandJournal`, same sqlite core as the gateway) grows one row per
dispatched intent plus frame receipts. No CLI or scheduler invokes the
purge in the tree (verified by search), so run it from owner-approved
cron against the live path (imports verified by execution, same form
as the gateway journal line):

```sh
uv run python -c "from relaydeck.journal import CommandJournal;
j = CommandJournal('$HOME/.config/relaydeck/connector-journal.db');
print(j.purge()); j.close()"
```

Retention is shared policy (`relaydeck/journal.py`: terminal records
tombstone after 24 h, tombstones purge after 7 d, frame receipts drop
after 1 h). `PREPARATION` to rehearse against a copy; the live-path
cron entry itself is `REQUIRES-OWNER-APPROVAL` like any persistent
scheduling on the Mac.

## Verification (how this file was checked)

- Subcommands and flags: `relaydeck-connector --help` plus `enroll
  --help`, `run --help`; `relaydeck-gateway issue-enrollment --help`
  plus a `--store` round-trip to a scratch file (stdout
  id/code/fingerprint/qr + ASCII QR, stderr `persisted to <file> (0600
  shared store)`, 0600 JSON with the invitation; without `--store` the
  same stdout plus the `single-process demo store` warning and nothing
  redeemable); `run` exit codes observed live (no source flag -> 2, both source
  flags -> 2, `--live-herdr` without `RELAYDECK_ALLOW_LIVE_HERDR=1`
  -> 2, isolated HOME not enrolled -> 1, dead daemon socket with auth
  -> 1, dead credential against a loopback server -> `auth-failed`
  exit 1); `serve`/`issue-enrollment` flag sets cross-checked against
  `src/relaydeck/*/cli.py`.
- Paths, modes, and atomic writes: `connector/enrollment.py`
  (`DEFAULT_KEY`, `DEFAULT_CREDS`, `_write_private` 0600);
  `connector/herdr.py` (`DEFAULT_SOCKET`, `EXPECTED_PROTOCOL = 22`,
  18-method `ALLOWED_METHODS` counted live); `connector/journal.py`
  (tunnel message kinds, `GATEWAY_KINDS`, write-ahead dispatch);
  `connector/link.py` + `connector/source.py` (tunnel, backoff,
  snapshot/event/output push, bootstrap, translation, relay, live
  loop, epoch intake, pane subscriptions, dead-attach reap/respawn,
  pane-sub backoff, size guard, push breaker, chunked send).
- Enrollment semantics: `claim_enrollment` mutual fingerprint check,
  `EnrollmentStore` TTL/attempt burn re-read; `status`/`revoke`
  outputs quoted from `connector/cli.py`.
- No-service-files claim: tree search for
  `launchd|launchctl|plist` (zero hits outside docs/runbooks/),
  re-verified at write time.
- `launchctl` presence verified on this Mac (`/bin/launchctl`); it is a
  macOS system tool, marked (sys) everywhere it appears.
- Secret-free: no key, seed, token, or fingerprint literals in this
  file (release scanner run at delivery).

### Typed terminal delivery and local verification

`terminal.input` and `terminal.resize` are RelayDeck connector-tunnel
envelopes, not Herdr RPCs. Example (synthetic identities):

```json
{"kind":"terminal.input","host_id":"home-mac","terminal_id":"term-1","epoch":"e1","lease_id":"lease-1","generation":1,"input_seq":1,"input_kind":"text","data_b64":"aGVsbG8="}
{"kind":"terminal.resize","host_id":"home-mac","terminal_id":"term-1","epoch":"e1","lease_id":"lease-1","generation":1,"input_seq":2,"cols":120,"rows":40}
```

A fresh control-lease frame is forwarded once to the currently enrolled
account's live tunnel. Exact text/control bytes go to the existing attach
PTY; resize changes that PTY's geometry. Legacy named-key frames use Herdr's
existing `pane.send_keys` encoder only when metadata supplies one unambiguous
pane; combo names are never inserted as literal text. Phone/web tools already
use exact encoded bytes through the text frame.

Strict shared parsing rejects foreign hosts, stale epochs, boolean integer
coercion, invalid key combos and oversized input. Connector high-water marks
are consumed before a write; missing/partial/failed delivery is never retried.
`terminal.input_failed` returns only host/lease/generation/sequence identity;
the gateway emits `gap/input_uncertain` to the owning viewer. A dead output
tap sends `terminal.reset_required` with terminal/epoch identity; the gateway
rotates only that authorized stream and publishes the new table. Neither
message contains terminal text or authorizes a command.

Socket send, lock wait, close and PTY write have five-second bounds; the
terminal relay buffers at most 1 MiB and input dedupe at most 1024 identities.
The deployed gateway/connector must both include typed terminal delivery. A synthetic recorder
(`FakeBackend`) is not evidence of delivery to Herdr.

Run `.venv/bin/python -m pytest tests/integration/sync/test_serving_and_connector_transport.py -q`
for real in-process gateway/link/source integration with explicit PTY and
subscription doubles. Tests exercise input/output bytes, resize, failure,
epoch recovery, snapshot intake, account fences and lifecycle cleanup without
TCP, providers, live Herdr or service setup. This is not browser/phone or
real-daemon acceptance; those gates remain open.

### Command outcomes

`command.dispatch` is a project tunnel envelope, not an upstream Herdr method.
It includes the submitting account, host and host epoch, canonical digest,
revision assertion and deadline; the link validates it before acknowledgement.
The connector journals before execution, checks source continuity and obtains a
fresh agent revision through the existing `agent.list` method before a prompt.
This preflight cannot make the subsequent upstream RPC atomic.

The app observes the returned command ID via authenticated status queries and
`command.updated` metadata. Same-ID redelivery never executes twice; changed
account/body is a conflict. Disconnect, timeout or crash after dispatch stays
unknown. Reconcile the recorded outcome; do not resend uncertain input.
Blocked Composer insertion sends only reviewed UTF-8, without Enter or paste
framing. `gap/input_uncertain` carries the failed generation/input sequence;
the matching draft is preserved without replay and unrelated drafts remain intact.
