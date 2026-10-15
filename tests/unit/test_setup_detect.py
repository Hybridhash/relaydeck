"""`relaydeck setup --detect` is read-only and reports four separate states per CLI.

Everything runs against a fake HOME, a fake PATH with fake executables that never run (each one
writes a
marker if it ever did), a fake `herdr integration status` text and a fake companion configuration.
The
standard bin directories are replaced too, so the real machine cannot change a result.
"""

from __future__ import annotations

import builtins
import os
import tomllib
from pathlib import Path
from typing import Any

import pytest

from relaydeck.cli import detect_lines
from relaydeck.companion import detect
from relaydeck.companion.detect import (
    CatalogEntry,
    CatalogError,
    load_catalog,
    run_detect,
)
from relaydeck.usage.readers import READERS
from tests.support.companion.setup_sources import (
    CUM_CLI,
    CUMULATIVE_READER,
    MESSAGE_READER,
    MSG_CLI,
    UNSUPPORTED_CLI,
    under_home,
)

MARKER = "RAN-MARKER"


def _hook_line(entry: CatalogEntry, state: str, suffix: str = "") -> str:
    return f"{entry.herdr_integration}{suffix}: {state} (/home/x/hooks/herdr-hook.sh)\n"


_WITH_HOOK = [e for e in load_catalog().entries if e.herdr_integration]
_USED = {MSG_CLI.id, CUM_CLI.id, UNSUPPORTED_CLI.id}
_SPARE = [e for e in _WITH_HOOK if e.id not in _USED]
# One entry whose Herdr integration name differs from its catalog id, one listed as experimental
# and one that will be reported outdated.
ALIASED = next(e for e in _SPARE if e.herdr_integration != e.id)
EXPERIMENTAL = next(e for e in _SPARE if e is not ALIASED)
OUTDATED = next(e for e in _SPARE if e not in (ALIASED, EXPERIMENTAL))
HERDR_TEXT = (
    _hook_line(UNSUPPORTED_CLI, "not installed")
    + _hook_line(MSG_CLI, "current (v10)")
    + _hook_line(CUM_CLI, "current (v8)")
    + _hook_line(ALIASED, "not installed")
    + _hook_line(EXPERIMENTAL, "not installed", " (experimental)")
)
_NO_HOOK = [e for e in load_catalog().entries if e.herdr_detects and not e.herdr_integration]
# A catalog entry that is none of the above and has a log location, for "not installed" cases.
ABSENT = next(
    e
    for e in load_catalog().entries
    if e.id not in _USED and e.reader_format is None and e.binaries and e.default_roots
)


def _executable(path: Path) -> None:
    """A fake CLI: if it were ever run, it would leave a marker file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch '{path.parent / MARKER}'\n", encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setattr(detect, "STANDARD_BIN_DIRS", ())
    return {"home": home, "bin": bin_dir, "environ": {"PATH": str(bin_dir)}}


def _run(
    fixture: dict[str, Any],
    *,
    config_path: Path | None = None,
    herdr: str | None = HERDR_TEXT,
    catalog: detect.Catalog | None = None,
) -> detect.DetectReport:
    return run_detect(
        catalog or load_catalog(),
        home=str(fixture["home"]),
        environ=fixture["environ"],
        config_path=config_path,
        herdr_reader=lambda: herdr,
    )


def _row(report: detect.DetectReport, cli_id: str) -> detect.DetectRow:
    return next(row for row in report.rows if row.id == cli_id)


def _config(tmp_path: Path, *, kinds: list[str], usage_kind: str | None = None) -> Path:
    lines = [
        'advertised_origin = "https://mac.example-tailnet.ts.net"',
        f'cookie_secret_file = "{tmp_path / "cookie.secret"}"',
        f'cursor_secret_file = "{tmp_path / "cursor.secret"}"',
        f'state_dir = "{tmp_path / "state"}"',
        f'connector_credentials = "{tmp_path / "connector.json"}"',
        f'herdr_socket = "{tmp_path / "herdr.sock"}"',
        f"agent_kinds = {kinds!r}".replace("'", '"'),
    ]
    if usage_kind is not None:
        root = tmp_path / "usage-root"
        root.mkdir(exist_ok=True)
        lines += [
            "[usage]",
            'timezone = "Etc/UTC"',
            "[[usage.sources]]",
            f'kind = "{usage_kind}"',
            f'format = "{MESSAGE_READER}"',
            f'root = "{root}"',
            f'display_name = "{usage_kind}"',
        ]
    path = tmp_path / "companion.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


# -- the four states -----------------------------------------------------------------------------


def test_an_installed_supported_configured_cli_shows_all_four_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    binary = fixture["bin"] / MSG_CLI.binaries[0]
    _executable(binary)
    under_home(fixture["home"], MSG_CLI).mkdir(parents=True)
    config = _config(tmp_path, kinds=[MSG_CLI.id])
    row = _row(_run(fixture, config_path=config), MSG_CLI.id)
    assert row.herdr_detects is True
    assert row.herdr_hook == "current v10"
    assert row.binary == str(binary) and os.path.isabs(row.binary)
    assert row.adapter == MESSAGE_READER
    assert row.configured is True
    assert row.next_step == "already configured"
    assert row.roots_present == (str(under_home(fixture["home"], MSG_CLI)),)


def test_an_installed_cli_with_no_adapter_needs_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _executable(fixture["bin"] / UNSUPPORTED_CLI.binaries[0])
    row = _row(_run(fixture), UNSUPPORTED_CLI.id)
    assert row.binary is not None
    assert row.adapter == "not yet"
    assert row.configured is False
    assert row.next_step == "needs an adapter (not supported yet)"


def test_an_installed_cli_with_an_adapter_that_is_not_configured_can_be_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _executable(fixture["bin"] / CUM_CLI.binaries[0])
    row = _row(_run(fixture), CUM_CLI.id)
    assert row.adapter == CUMULATIVE_READER
    assert row.configured is False
    assert row.next_step == "can be added"


def test_a_cli_that_is_not_installed_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    row = _row(_run(fixture), ABSENT.id)
    assert row.binary is None
    assert row.next_step == "not installed"


def test_a_root_present_without_its_binary_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    under_home(fixture["home"], ABSENT).mkdir(parents=True)
    row = _row(_run(fixture), ABSENT.id)
    assert row.binary is None
    assert row.roots_present == (str(under_home(fixture["home"], ABSENT)),)
    assert row.next_step == "not installed"


def test_an_environment_override_root_counts_when_it_is_an_absolute_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    custom = tmp_path / "custom-config"
    (custom / MSG_CLI.env_subdir).mkdir(parents=True)
    fixture["environ"][MSG_CLI.env_override] = str(custom)
    row = _row(_run(fixture), MSG_CLI.id)
    assert str(custom / MSG_CLI.env_subdir) in row.roots_present


def test_a_relative_environment_override_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    fixture["environ"][MSG_CLI.env_override] = MSG_CLI.env_subdir
    row = _row(_run(fixture), MSG_CLI.id)
    assert not any(
        MSG_CLI.env_subdir == Path(root).name and not root.startswith("/")
        for root in row.roots_present
    )


def test_herdr_missing_is_unknown_and_never_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _executable(fixture["bin"] / MSG_CLI.binaries[0])
    report = _run(fixture, herdr=None)
    assert report.herdr_status == "unknown"
    assert all(row.herdr_hook == "unknown" for row in report.rows)
    assert _row(report, MSG_CLI.id).herdr_detects is True  # the catalog's list needs no Herdr
    assert _row(report, MSG_CLI.id).binary is not None


# -- safety: nothing is run, nothing inside a root is opened, nothing is written
# ----------------------


def test_binaries_are_never_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    for entry in (MSG_CLI, CUM_CLI, UNSUPPORTED_CLI):
        _executable(fixture["bin"] / entry.binaries[0])
    _run(fixture)
    assert not (fixture["bin"] / MARKER).exists()


def test_no_file_inside_a_root_is_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    root = under_home(fixture["home"], MSG_CLI)
    root.mkdir(parents=True)
    (root / "session.jsonl").write_text("{}\n", encoding="utf-8")
    opened: list[str] = []
    real_open = builtins.open

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    _run(fixture)
    assert not [name for name in opened if name.startswith(str(root))]


def test_detection_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _executable(fixture["bin"] / CUM_CLI.binaries[0])
    under_home(fixture["home"], MSG_CLI).mkdir(parents=True)

    def snapshot() -> set[tuple[str, float]]:
        return {(str(path), path.stat().st_mtime) for path in tmp_path.rglob("*")}

    config = _config(tmp_path, kinds=[CUM_CLI.id])
    before = snapshot()
    _run(fixture, config_path=config)
    assert snapshot() == before


def test_a_missing_configuration_is_a_status_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    report = _run(fixture, config_path=tmp_path / "absent.toml")
    assert report.config_status == "missing"
    assert all(row.configured is False for row in report.rows)


def test_an_invalid_configuration_is_a_status_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    bad = tmp_path / "companion.toml"
    bad.write_text('unknown_field = "x"\n', encoding="utf-8")
    bad.chmod(0o600)
    report = _run(fixture, config_path=bad)
    assert report.config_status == "invalid"


def test_a_usage_source_configures_its_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    # A usage source's kind must also be in agent_kinds (the config rule), so both are set here.
    config = _config(tmp_path, kinds=[CUM_CLI.id], usage_kind=CUM_CLI.id)
    assert _row(_run(fixture, config_path=config), CUM_CLI.id).configured is True


# -- the catalog ---------------------------------------------------------------------------------


def test_the_shipped_catalog_keeps_readers_to_tier_a_and_ships_each_reader() -> None:
    catalog = load_catalog()
    assert catalog.version == 1
    assert {entry.tier for entry in catalog.entries} <= {"A", "B", "C", "D", "E"}
    for entry in catalog.entries:
        assert (entry.reader_format is not None) == (entry.tier == "A"), entry.id
        if entry.reader_format:
            assert entry.reader_format in READERS, entry.id


def test_every_log_root_of_a_multi_root_cli_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    multi = next(entry for entry in load_catalog().entries if len(entry.default_roots) > 1)
    expected = tuple(str(under_home(fixture["home"], multi, n)) for n in range(2))
    for root in expected:
        Path(root).mkdir(parents=True)
    assert _row(_run(fixture), multi.id).roots_present == expected


def _catalog_text(body: str) -> str:
    return f"catalog_version = 1\n{body}"


_ONE = """
[[cli]]
id = "one"
display_name = "One"
binaries = ["one"]
default_roots = ["~/one"]
tier = "B"
evidence = "test"
"""


def test_a_valid_small_catalog_loads() -> None:
    assert load_catalog(_catalog_text(_ONE)).entries[0].id == "one"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_ONE + _ONE, "duplicate id"),
        (_ONE.replace('tier = "B"', 'tier = "Z"'), "unknown tier"),
        (
            _ONE.replace('tier = "B"', 'tier = "B"\nreader_format = "jsonl-message-usage-v1"'),
            "only allowed on tier A",
        ),
        (
            _ONE.replace('tier = "B"', 'tier = "A"\nreader_format = "made-up-v9"'),
            "unknown reader_format",
        ),
        (
            _ONE.replace('default_roots = ["~/one"]', 'default_roots = ["relative/one"]'),
            "not absolute",
        ),
        (_ONE.replace('binaries = ["one"]', "binaries = []"), "non-empty"),
        (_ONE.replace('id = "one"', 'id = "One Bad"'), "invalid id"),
    ],
)
def test_catalog_validation_refuses_each_bad_shape(body: str, message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        load_catalog(_catalog_text(body))


def test_a_catalog_with_the_wrong_version_is_refused() -> None:
    with pytest.raises(CatalogError, match="catalog_version"):
        load_catalog("catalog_version = 2\n" + _ONE)


# -- the printed table ---------------------------------------------------------------------------


def test_the_printed_table_names_the_four_states_and_a_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _executable(fixture["bin"] / UNSUPPORTED_CLI.binaries[0])
    lines = detect_lines(_run(fixture))
    assert lines[0].startswith("CLI") and "Detects" in lines[0] and "Hook" in lines[0]
    assert "Installed" in lines[0]
    assert "Adapter" in lines[0] and "Configured" in lines[0]
    index = next(i for i, line in enumerate(lines) if line.startswith(f"{UNSUPPORTED_CLI.id} "))
    assert "needs an adapter (not supported yet)" in lines[index + 1]
    assert any(line.startswith("Summary:") for line in lines)
    assert lines[-1].startswith("Nothing was written")


def test_the_catalog_file_is_the_only_one_naming_products() -> None:
    """The names exception is tested in test_no_agent_product_names; here the catalog must parse."""
    text = (Path(detect.__file__).parent / "data" / "cli_catalog.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["catalog_version"] == 1


# -- Herdr detects (the catalog's list) and the Herdr hook (Herdr's integration status)
# ----------------


def test_the_detection_list_is_exactly_the_twenty_two_manifest_ids() -> None:
    """The manifest ids on the Mac; no other CLI is claimed as detected."""
    detected = [entry for entry in load_catalog().entries if entry.herdr_detects]
    assert len(detected) == 22
    assert all(entry.tier in {"A", "B", "C", "D", "E"} for entry in detected)


def test_a_cli_with_no_integration_hook_says_none_even_when_herdr_detects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    assert _NO_HOOK
    for entry in _NO_HOOK:
        row = _row(_run(fixture), entry.id)
        assert row.herdr_detects is True, entry.id
        assert row.herdr_hook == "none", entry.id


def test_an_integration_that_is_not_installed_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    row = _row(_run(fixture), UNSUPPORTED_CLI.id)
    assert row.herdr_detects is True
    assert row.herdr_hook == "not installed"


def test_an_outdated_integration_shows_both_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    text = HERDR_TEXT + _hook_line(OUTDATED, "outdated (v12 < v13)")
    assert _row(_run(fixture, herdr=text), OUTDATED.id).herdr_hook == "outdated v12<v13"


def test_the_table_fits_one_hundred_columns_and_carries_a_legend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    lines = detect_lines(_run(fixture))
    assert all(len(line) <= 100 for line in lines), [line for line in lines if len(line) > 100]
    assert any(line.strip().startswith("Legend:") for line in lines)
    assert any("none = Herdr has no hook" in line for line in lines)
