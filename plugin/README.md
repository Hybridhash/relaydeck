# RelayDeck Herdr plugin

A thin launcher. It gives Herdr three popup actions, and every bit of logic stays in the Python
package `relaydeck`.

| Action | Pane | Runs |
|---|---|---|
| RelayDeck: Set up | `setup` | `relaydeck setup` (interactive, in the popup) |
| RelayDeck: Pair a device | `pair` | `relaydeck-companion pairing open`, then you approve each device here |
| RelayDeck: Status | `status` | `relaydeck-companion status` and `relaydeck-companion doctor` (read-only) |

The entry point (`bin/relaydeck-plugin.sh`) starts from `/bin/sh` and runs the Python launcher
(`bin/relaydeck-plugin`) only after validating the installed RelayDeck. The launcher finds the installed
`relaydeck` or `relaydeck-companion` in the uv tool bin directory or `~/.local/bin`, refuses an executable
that is not owned by you, is group- or world-writable, or is a symbolic link to an unsafe target, and runs
it with a fixed argument list. It installs nothing, passes no confirmation flags, never approves a pairing
and never reads Herdr's context JSON. A `build` step checks that uv and Python 3.13 exist.

## Install

Install RelayDeck first (see the main README, "Install"), then install this plugin with Herdr:

    herdr plugin install Hybridhash/relaydeck/plugin --ref v0.2.0

or link a local checkout while developing:

    herdr plugin link PATH_TO_CLONE/plugin

## Uninstall

    herdr plugin uninstall relaydeck     (or `herdr plugin unlink relaydeck` for a linked checkout)

and, to remove the companion too: `uv tool uninstall relaydeck`.
