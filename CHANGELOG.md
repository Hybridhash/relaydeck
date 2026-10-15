# Changelog

User-visible changes by release. Versions follow the package version
(`relaydeck --version`).

## 0.2.0

- **Install and setup**: `relaydeck setup` creates the private config and checks Herdr and
  Tailscale; `relaydeck setup --check` prints a read-only readiness report with the next step
  for each check. `relaydeck --version`, `relaydeck-companion --version` and Settings show the
  same version.
- **Pairing**: devices pair by approving a matching code on the Mac
  (`relaydeck pairing open`, `list`, `approve`). Sign-ins last 30 days after last use and
  survive a companion restart when durable sign-ins are enabled.
- **Faster terminals**: terminals you switch back to keep their size and content while they
  settle, and recently used terminals stay warm.
- **Usage**: per-tool and per-space tokens, estimated cost and plan limits, with hourly,
  7-, 30- and 90-day charts; setup offers each supported tool it finds as a source.
- **Notifications**: Web Push for blocked and done agents, with per-device choices.
- **Settings**: Acknowledgements lists the open-source software and fonts the app ships with.

## 0.1.1

- Terminal reconnect and recovery: a dropped link resumes the same terminal without replaying
  unsent input, and takeover is disclosed before it happens.
- Unsent composer drafts survive switching between agents, shells and destinations.
- Safer paste: multi-line paste is reviewed before it reaches the terminal.

## 0.1.0

- First release: the Console with every Herdr space, agent and shell and live status.
- Real terminals with selection, copy, control keys and a keyboard toolbar for phones.
- Three input modes: agent composer, direct input and ordinary shell terminal.
- Web app (installable on iPhone and iPad) over a private Tailscale address; native Android and iOS apps in progress.
