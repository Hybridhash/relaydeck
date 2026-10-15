"""Custom hooks and builders of the client `Root` component (Q6b).

`Root` in `client/app.py` used to hold about 1,600 lines of interleaved state, effects and
closures. The code is now split into plain functions that `Root` calls at the position the
inline code occupied, so React-style hook order (Flet follows the same rule) and the control
tree, keys and ancestry are unchanged. Hooks are never called conditionally.

Hooks (each owns one cohesive state/effect cluster and returns what `Root` still reads):

- `appearance.use_appearance`, `preferences.use_preferences`, `typing_focus.use_typing_focus`,
  `typing_focus.use_typing_split_log` (content-free decision log)
- `navigation.use_route_state`, `use_navigation` (route bridge subscription), `use_route_effects`
- `sync.use_sync_state`, `use_sync_subscription`, `use_host_effects`
- `inventory.use_inventory`, `sheets.use_creation_sheets`
- `retained.use_retained_sessions`, `use_terminal_effects`

Builders (no hook calls; they return closures or controls built from Root state):
`actions.build_open_terminal`, `actions.build_navigation_actions`,
`creation.build_creation_actions`, `creation.build_shell_origin_actions`,
`views.build_retained_views`, `views.build_inventory_shell`, `views.build_creation_sheets`,
`views.build_status_overlays`. `support` holds the plain helpers they share; `client/app.py`
re-exports the public ones.

Rule for new code: add state to the hook that owns the cluster; give a hook only the inputs
it reads (keyword-only); never reorder hook calls inside `Root`.
"""
