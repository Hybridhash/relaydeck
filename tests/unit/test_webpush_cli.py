"""`relaydeck-companion push web-rotate` needs a typed phrase on a terminal and never
runs while the companion is up. Temporary home only."""

from __future__ import annotations

from pathlib import Path

import pytest

from relaydeck.companion import cli
from relaydeck.gateway import webpush
from relaydeck.gateway.webpush_store import WebPushStore
from tests.support.companion.setup_sources import setup_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    return fake


def _seed(config: Path, state: Path) -> str:
    """A key, one subscription and one queued message, so rotation has something to remove."""
    key = webpush.load_or_create_vapid(state)
    store = WebPushStore(state, clock=lambda: 1_700_000_000_000)
    ua = webpush.b64u(b"\x04" + b"\x09" * 64)
    auth = webpush.b64u(b"\x08" * 16)
    store.put_subscription(
        "acct", "dev", "https://fcm.googleapis.com/x/abc", ua, auth, 1_700_000_000_000
    )
    store.enqueue("acct", "dev", "event-1", "attention", now=1_700_000_000_000)
    store.close()
    return webpush.public_key_b64u(key)


def _tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: value)


def test_the_typed_phrase_rotates_the_key_and_removes_subscriptions_and_queued_work(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _report = setup_home(home)
    state = home / "state"
    before = _seed(config, state)
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda prompt: "rotate web push")
    assert cli.main(["--config", str(config), "push", "web-rotate"]) == 0
    assert "1 subscription(s) removed" in capsys.readouterr().out
    assert webpush.public_key_b64u(webpush.load_or_create_vapid(state)) != before
    store = WebPushStore(state)
    try:
        assert store.subscription("acct", "dev") is None
        assert store.due(1_700_000_000_000 + 1) == []
        assert store.sending_enabled() is True
    finally:
        store.close()


@pytest.mark.parametrize("answer", ["", "y", "yes", "ROTATE WEB PUSH!", "rotate"])
def test_anything_but_the_exact_phrase_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], answer: str
) -> None:
    config, _report = setup_home(home)
    state = home / "state"
    before = _seed(config, state)
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert cli.main(["--config", str(config), "push", "web-rotate"]) == 1
    assert "nothing was changed" in capsys.readouterr().out
    assert webpush.public_key_b64u(webpush.load_or_create_vapid(state)) == before


def test_without_a_terminal_nothing_is_asked_or_changed(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _report = setup_home(home)
    state = home / "state"
    before = _seed(config, state)
    _tty(monkeypatch, False)

    def no_input(prompt: str) -> str:
        raise AssertionError("must not prompt without a terminal")

    monkeypatch.setattr("builtins.input", no_input)
    assert cli.main(["--config", str(config), "push", "web-rotate"]) == 2
    assert "nothing was changed" in capsys.readouterr().out
    assert webpush.public_key_b64u(webpush.load_or_create_vapid(state)) == before


def test_a_running_companion_blocks_rotation(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _report = setup_home(home)
    state = home / "state"
    before = _seed(config, state)
    _tty(monkeypatch, True)
    monkeypatch.setattr(cli, "status", lambda directory: {"state": "running"})

    def no_input(prompt: str) -> str:
        raise AssertionError("must not prompt while the companion runs")

    monkeypatch.setattr("builtins.input", no_input)
    assert cli.main(["--config", str(config), "push", "web-rotate"]) == 1
    assert "Stop the companion first" in capsys.readouterr().out
    assert webpush.public_key_b64u(webpush.load_or_create_vapid(state)) == before


def test_there_is_no_yes_flag_to_skip_the_phrase(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _report = setup_home(home)
    _tty(monkeypatch, True)
    with pytest.raises(SystemExit) as caught:
        cli.main(["--config", str(config), "push", "web-rotate", "--yes"])
    assert caught.value.code == 2
