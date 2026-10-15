"""Shared terminal lifecycle and generation-bound capabilities.

`TerminalSessionBase` orders lifecycle steps; agent and shell subclasses supply
workflow-specific hooks. `RendererBinding`, `LeaseRenewalOwner`,
`SelectionController`, `PasteFlow` and `KeyInput` are composed collaborators
with explicit callbacks and no session back-reference. `hooks` builds those
callbacks; `SessionFacade` delegates without adding lifecycle behavior.

Agent Composer edits stay local, Agent Direct sends through the live writer,
and Shell never owns a composer store. Retired generations cannot replay input.
Renewal failures act only on the driver, lease and controller they captured."""
