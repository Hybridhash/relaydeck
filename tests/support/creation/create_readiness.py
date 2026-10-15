"""Create-readiness message builder."""

from __future__ import annotations

from typing import Any

from tests.support.creation.command_journal import _dispatch_msg


def _message(new: bool = False) -> dict[str, Any]:
    return {
        **_dispatch_msg(
            "ready-agent-command",
            "agent.start",
            {"workspace_mode": "new"} if new else {"workspace_id": "w1"},
            {
                "name": "qa",
                "kind": "agent-a",
                "argv": ["--flag"],
                **({"new_workspace": {"directory": "/synthetic", "label": "qa"}} if new else {}),
            },
        ),
        "host_epoch": "epoch-7",
    }
