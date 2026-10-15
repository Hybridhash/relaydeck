"""the `[notifications]` table, in a temporary home only."""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.companion.config import NotificationsConfig, load_config, notifications_from
from tests.support.companion.setup_sources import setup_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    return fake


def test_the_defaults_are_web_push_on_generic_lock_screen_and_a_placeholder_subject(
    home: Path,
) -> None:
    config, _report = setup_home(home)
    loaded = load_config(config)
    assert loaded.notifications == NotificationsConfig()
    assert loaded.notifications.web_push is True
    assert loaded.notifications.lock_screen_detail is False
    assert loaded.notifications.vapid_subject == "https://example.com/relaydeck"


def test_an_explicit_section_is_read(home: Path) -> None:
    config, _report = setup_home(home)
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[notifications]\nweb_push = false\nlock_screen_detail = true\n"
        + 'vapid_subject = "https://ops.example.com/contact"\n',
        encoding="utf-8",
    )
    loaded = load_config(config)
    assert loaded.notifications == NotificationsConfig(
        web_push=False, lock_screen_detail=True, vapid_subject="https://ops.example.com/contact"
    )


@pytest.mark.parametrize(
    "value",
    [
        {"web_push": "yes"},
        {"lock_screen_detail": 1},
        {"vapid_subject": "https://example.invalid"},
        {"vapid_subject": "mailto:no-at-sign"},
        {"vapid_subject": "https://has space.example.invalid"},
        {"unknown_key": True},
    ],
)
def test_bad_notification_settings_fail_closed(value: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        notifications_from(value)


def test_a_non_table_section_is_refused() -> None:
    with pytest.raises(ValueError):
        notifications_from("on")
