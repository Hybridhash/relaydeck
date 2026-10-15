"""the owner's agent_kinds config is shape-checked, deduplicated and capped.

Synthetic kinds and temporary files only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.companion.config import agent_kinds_from, load_config
from tests.support.companion.asgi_companion import config_file


def test_valid_kinds_are_kept_in_order_and_deduplicated() -> None:
    assert agent_kinds_from(["agent-a", "agent-b", "agent-a", "tool_c"]) == (
        "agent-a",
        "agent-b",
        "tool_c",
    )


def test_invalid_entry_is_refused_naming_its_index_not_its_value() -> None:
    with pytest.raises(ValueError, match="entry 1 is not a valid kind name") as refused:
        agent_kinds_from(["agent-a", "Not Valid!", "agent-b"])
    assert "Not Valid" not in str(refused.value)


@pytest.mark.parametrize("value", ["agent-a", 3, None, [3]])
def test_non_list_or_non_string_values_are_refused(value: object) -> None:
    with pytest.raises(ValueError):
        agent_kinds_from(value)


def test_at_most_32_kinds() -> None:
    assert len(agent_kinds_from([f"agent-{n}" for n in range(32)])) == 32
    with pytest.raises(ValueError, match="at most 32"):
        agent_kinds_from([f"agent-{n}" for n in range(33)])


def test_config_loads_the_configured_kinds(tmp_path: Path) -> None:
    path = config_file(tmp_path, advertised_origin="https://mac.example.ts.net")
    path.write_text(path.read_text() + '\nagent_kinds = ["agent-a", "agent-b"]\n')
    assert load_config(path).agent_kinds == ("agent-a", "agent-b")


def test_config_default_is_empty(tmp_path: Path) -> None:
    path = config_file(tmp_path, advertised_origin="https://mac.example.ts.net")
    assert load_config(path).agent_kinds == ()
