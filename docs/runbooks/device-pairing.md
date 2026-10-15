# Local device pairing

A device pairs by requesting access and the Mac owner approving a matching
code on the Mac. The browser, the iPhone/iPad web app and Android all use this
flow through the Connect panel. There is no separate account provider; the
pairing approval is the sign-in. Existing OIDC code remains available but is
not used by this pairing path.

The foreground Mac companion configures `state_dir/pairing.json` automatically.
The standalone gateway can opt in with `--pairing-store <private-path>` and its
configured HTTPS origin. No pairing window opens automatically. The gateway
still binds loopback; clients reach the approved private HTTPS/WSS address.
This work installs no service or private-network route.

## Local owner ceremony

First enroll the **connector**, using the existing public-key-bound local
operator ceremony in [connector.md](connector.md). This grants the connector's
host tunnel token; it does not authorize any client device. The Mac must have
an enrolled host with an owner, public key and matching fingerprint.

From the installed repository environment, use the protected companion config:

```sh
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml pairing open --ttl-seconds 120
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml pairing list
.venv/bin/python -m relaydeck.companion.cli --config ~/.config/relaydeck/companion.toml pairing approve --request <request-id> --code <XXX-XXX>
```

Compare the request ID and matching code on the client and Mac. Inspect the
private address, host fingerprint and scope. Labels are untrusted and escaped
in CLI output. Replace `approve` with `deny` to reject a pending or unused
approved request. `pairing close` discards every unused request/approval; it does
not revoke already issued credentials. Revoke those with the existing device
revocation contract. There is no HTTP approval endpoint.

## Gateway protocol

- `POST /v1/pairing/requests`: body `origin`, `host_id`, `device_label`,
  `code_challenge`, `code_challenge_method: "S256"`. Keep the randomly generated
  43–128 character verifier on the client. The response includes request ID,
  matching code, fresh device ID, host fingerprint, exact capabilities, state,
  expiry and polling interval. Account/grants/device identity cannot be supplied.
- `POST /v1/pairing/{request_id}/status`: body `origin`, `host_id`, `verifier`.
  Returns pending/approved/denied; poll no faster than 2 seconds. Verifiers never
  belong in URLs or logs. Invalid/expired/consumed requests return `not_found`.
- `POST /v1/pairing/{request_id}/redeem`: same proof body. Only an approved,
  unexpired request can atomically produce the existing token response.
  An uncertain redemption must not be replayed automatically; begin a new
  explicit pairing attempt if no credentials were received.

Requests are capped at 2048 bytes, 20 per window, 5 creations/10 seconds and
60 total network operations/10 seconds; each request allows 60 polls and five
failed verifier attempts. Windows last at most 300 seconds; requests at most
120 seconds. Closing/reopening a window invalidates unused approvals. Both
browser Origin headers (when present) and body origin must match configuration.

Grants permit the seven Console/terminal capabilities for **one selected host**;
they omit `hosts.enroll`. Paired sessions never inherit other owned hosts,
including during refresh or notification eligibility. Host enrollment rotation
invalidates their binding; normal host revocation also severs paired sessions.

Paired device management is self-only: `/v1/devices` reports only the issued
session device ID, and registration/rotation/revocation must target that ID.
Foreign device requests return `not_found` without changing credentials or
push subscriptions. Refresh preserves this restriction. Logout removes the
issued device's subscription and ignores extra install refs; notification/link
resolution also requires that device and its selected host. Clients register
FCM against the server-issued `/v1/me.device_id`, not a separate caller-chosen
install ID. Existing unpaired owner sessions retain their account management.

## Restart and evidence boundaries

Serving startup resets the pairing epoch and all approvals. Whether a paired
device stays signed in across a companion restart depends on durable sign-ins
(see [companion operations](companion.md)): when they are enabled and the key
store is available, sign-ins are restored; otherwise they are memory-only and
the device must pair again. No approval or terminal write is restored.
Owner-only state (0700 directory, 0600 files), symlink/hardlink rejection and
file locking protect the CLI/server boundary. No verifiers or tokens are stored
in the pairing file.

Tests use temporary synthetic enrollment records, in-process ASGI and explicit
server doubles. Actual browser/device pairing, secure storage on Android,
private-route connectivity and terminal fidelity remain unverified. The owner
controls foreground runtime reloads.
