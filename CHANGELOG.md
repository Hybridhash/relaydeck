# Changelog

User-visible changes by release. Versions follow the package version
(`relaydeck --version`).

## 0.2.4

- **Phone Direct input**: the typing row keeps only the text field and the keyboard button, stays
  put while the keyboard is down, and shows "Agent is working" above its keys. The page no longer
  stays shifted after iOS scrolls it.
- **iPad**: the typing column keeps the keyboard up and has the phone's keyboard button. Its tools
  button replaces Keys and opens every terminal key, the input switch, paste, Select text and attach
  above the keyboard. Select text follows the handles at every width.
- **Reconnect**: an agent you left returns to your control when you reopen it after the app was
  hidden. After a long time away, a gentler "older output not kept" notice has a close button.
- **Paste on desktop browsers**: text copied outside the browser pastes into the composer and the
  terminal, including in Firefox and Safari. Very large pastes are refused with a message.
- **Fonts**: the web app ships Chinese, Japanese and Korean fallback fonts and Noto Sans Symbols,
  with their licences in the app's notices. Fonts are cached until they change.
- **Server health**: Settings shows an owner-only Server health panel with request counts and
  timings, kept in memory only.
- **Usage**: Muse refresh resumes one saved session in an empty, trusted folder, so a reading costs
  no tokens; `relaydeck-companion usage install` helps trust that folder. A usage read waits for a
  new shell's prompt before giving up.
- **Gateway**: routes are grouped by area with one error format, request IDs and one logging setup.
  API docs routes are off unless debugging, secret replies are never cached, and sign-in work runs
  off the event loop.
- **Dependencies**: Starlette 1.8, FastAPI 0.133, Pillow 13, cryptography 50 and the latest certifi
  clear every known advisory. CI fails on any new one, and new releases are used only after seven
  days. The phone and tablet apps no longer bundle cryptography.
- **Quieter**: fewer log lines, no pasted text in debug logs, and the notifications banner checks
  less often.

## 0.2.3

- **Phone agent screen**: the header title opens the space's agents, with a count of working
  ones. A tools button opens every terminal key, the Compose / Direct input switch, paste, Select
  text and attach, floating over the terminal so it never resizes. The text box is a fixed two
  lines, and the typing row shows only in Direct input.
- **Working indicator**: "Agent is working" shimmers in the theme colours on phone and web while
  the agent works, and stops when it finishes.
- **Paste**: the paste review floats over the terminal on every layout instead of shrinking it.
- **Select text on the phone**: the highlight follows the handles exactly, and Copy selection copies
  what is highlighted.
- **Console**: the selected card stands out, spaces show as coloured chips, cards keep their order
  and height when an agent's status changes or it is read, and the loading bar no longer moves the
  list.
- **Renaming**: renaming an agent no longer changes its Herdr agent name.
- **Usage**: Refresh reads the tool again, rows stay visible while they reload, and an idle tool
  keeps its weekly limit.

## 0.2.2

- **Agent names**: an agent keeps its name after its process exits. Rename an agent from its
  card: swipe left, long-press, right-click, double-click the name or press F2. The sheet
  suggests names from the agent's task and offers Undo after saving. Names may contain spaces,
  capitals and emoji.
- **Console**: pin agents to the top from the long-press menu. Unread agents show "Unread", and the
  status counters take less space.
- **Phone keyboard**: the keyboard stays open after Send and while the terminal scrolls. The button
  next to Send hides or shows it.
- **Sending**: Ctrl+Enter (Cmd+Enter on Apple keyboards) sends the draft. A send interrupted by a
  reconnect is checked with the Mac before the draft is kept.
- **Reconnecting**: after a dropped connection, retries wait for the Mac and back off instead of
  looping. Persistent failures show a Reconnect prompt. Busy terminals and fast typing no longer
  close the connection.
- **Opening terminals**: cold opens show the terminal sooner, with fewer redraws behind it.
- **Usage**: long reports finish and stay visible while new usage is recorded, with the time they
  were calculated. A busy Mac answers quickly instead of queueing requests.
- **Companion**: stop and restart complete promptly. `kill -USR1 <pid>` writes thread stacks to the
  companion log for diagnosis.

## 0.2.1

- **Returning to the app**: terminals reconnect on their own when the app comes back to the
  foreground, and the first key or tap after a pause wakes the terminal. A brief switch away and
  back leaves the terminal's size alone.
- **Phone keyboard**: keyboard and composer size changes are coalesced, reducing repeated terminal
  redraws.
- **Scrolling**: agent screens scroll at a steady pace, a flick coasts, and pressing a key stops a
  scroll that is still moving.
- **Pairing**: pairing completes even when you leave the app before approving on the Mac.
- **Sign-out**: a revoked sign-in stays signed out, even while a sync is still running.
- **Usage**:
  - Readings that come from a tool's usage screen refresh on their own after you use the tool.
  - A reading is marked stale only when it may be wrong.
  - A paused refresh shows a countdown and when the reading was taken.
  - A temporary read failure keeps the last reading for a known account. Manual refreshes show
    more specific failure messages.
- **Diagnostics**: the companion log times each terminal open step by step. The timing lines
  never include terminal content.
- **CI**: the Python and Flutter test suites, type check, secret scan and licence inventory run on
  each push. The licence inventory covers every declared Dart dependency.

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
