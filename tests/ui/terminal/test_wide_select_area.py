"""Wide native selection copies the highlighted frozen snapshot without PTY input."""

from __future__ import annotations

import flet as ft
import pytest

from relaydeck.client.features.shell.screen import mount_shell_screen
from relaydeck.client.features.terminal.surfaces import mount_agent_screen
from tests.support.host_harness import render_component, run
from tests.support.terminal.selection import _mark_ready, _open_with_snapshot
from tests.support.terminal.sessions import _agent_session, _session
from tests.support.ui.tree_path import _path_to, _signature


@pytest.mark.parametrize("shell", [False, True])
@pytest.mark.parametrize("width", [1032, 1376])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_wide_selection_area_copies_native_highlight_exactly(
    shell: bool, width: int, newline: str
) -> None:
    async def scenario() -> None:
        if shell:
            session, _ops, factory = _session()
            await session.open("term-1")
            component = mount_shell_screen
        else:
            session, _ops, factory, _submitted = _agent_session()
            session.open()
            await session.confirm_takeover()
            session.on_draft_change("unsent draft")
            component = mount_agent_screen
        extra = (
            {"on_back": lambda: None, "on_new_terminal": lambda: None}
            if shell
            else {"viewport_width": width}
        )
        app = await render_component(component, session, width_tier="expanded", touch=True, **extra)
        app.session.apply_patch(app.page._i, {"width": width, "height": 1032})
        try:
            term, sent = _mark_ready(session)
            body = newline.join(("prefix", "😀 selected", "second line", "suffix"))
            ancestry = _signature(_path_to(app, "terminal-surface"))
            await _open_with_snapshot(app, term, sent, body)
            assert isinstance(app.find("select-text-area"), ft.SelectionArea)
            field = app.find("select-text-field")
            assert isinstance(field, ft.Text) and not field.selectable
            assert field.value == body and field.style.font_family == "JetBrains Mono"
            assert app.find("terminal-live-body").disabled
            assert app.find("terminal-selection-frame") is not None
            before = list(factory.sockets[-1].sent_bytes)
            selected = "😀 selected\nsecond line"
            await app.set_props_and_fire("select-text-area", {}, "change", selected)
            await app.set_props_and_fire("select-text-copy", {}, "click")
            expected = selected.replace("\n", newline)
            assert session._clipboard.writes == [expected]
            assert app.find("select-text-range").value.startswith("Chars ")
            await app.set_props_and_fire("select-text-area", {}, "change", "")
            await app.set_props_and_fire("select-text-copy", {}, "click")
            assert session._clipboard.writes == [expected, expected]
            await app.set_props_and_fire("select-text-close", {}, "click")
            assert not session.term_state.selection.open
            assert session.current_term is term
            assert _signature(_path_to(app, "terminal-surface")) == ancestry
            assert factory.sockets[-1].sent_bytes == before
            if not shell:
                assert session.store.draft == "unsent draft"
        finally:
            await session.dispose()

    run(scenario())
