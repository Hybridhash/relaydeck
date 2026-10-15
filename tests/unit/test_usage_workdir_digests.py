"""keyed working-folder digests. Synthetic paths only; keys live in tmp."""

from __future__ import annotations

import os
from pathlib import Path

from relaydeck.usage.link_digests import (
    cwd_digest,
    load_or_create_link_key,
    normalize_cwd,
    repo_fingerprint,
)


def test_key_is_created_once_with_private_permissions(tmp_path: Path) -> None:
    first = load_or_create_link_key(tmp_path)
    assert first is not None and len(first) == 32
    assert load_or_create_link_key(tmp_path) == first
    assert (tmp_path / "usage-link.key").stat().st_mode & 0o777 == 0o600


def test_unreadable_or_uncreatable_key_means_no_key(tmp_path: Path) -> None:
    assert load_or_create_link_key(tmp_path / "absent-dir") is None
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    assert load_or_create_link_key(occupied / "child" / "deeper") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "usage-link.key").write_bytes(b"")
    assert load_or_create_link_key(empty) is None
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "usage-link.key").mkdir()
    assert load_or_create_link_key(directory) is None


def test_normalization_strips_slash_resolves_links_and_nfc(tmp_path: Path) -> None:
    assert normalize_cwd("/synthetic/work/alpha/") == normalize_cwd("/synthetic/work/alpha")
    assert normalize_cwd("/synthetic/é") == normalize_cwd("/synthetic/é")
    assert normalize_cwd("/") == "/"
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert normalize_cwd(str(link)) == normalize_cwd(str(target))
    assert normalize_cwd(str(link)) == normalize_cwd(os.path.realpath(link))


def test_unusable_paths_normalize_to_none() -> None:
    assert normalize_cwd("") is None
    assert normalize_cwd(None) is None
    assert normalize_cwd(123) is None
    assert normalize_cwd("x" * 4097) is None


def test_digest_is_deterministic_keyed_and_opaque(tmp_path: Path) -> None:
    key = load_or_create_link_key(tmp_path)
    assert key is not None
    (tmp_path / "second").mkdir()
    other = load_or_create_link_key(tmp_path / "second")
    assert other is not None and other != key
    digest = cwd_digest(key, "/synthetic/work/alpha")
    assert digest is not None and len(digest) == 32
    assert digest == cwd_digest(key, "/synthetic/work/alpha/")
    assert cwd_digest(key, "/synthetic/work/beta") != digest
    assert cwd_digest(other, "/synthetic/work/alpha") != digest
    assert "alpha" not in digest and "/" not in digest


def test_missing_key_or_path_means_no_digest_never_unkeyed(tmp_path: Path) -> None:
    key = load_or_create_link_key(tmp_path)
    assert cwd_digest(None, "/synthetic/work/alpha") is None
    assert cwd_digest(b"", "/synthetic/work/alpha") is None
    assert cwd_digest(key, "") is None
    assert cwd_digest(key, None) is None


def test_repo_fingerprint_is_keyed_and_optional(tmp_path: Path) -> None:
    key = load_or_create_link_key(tmp_path)
    assert key is not None
    fingerprint = repo_fingerprint(key, "repo-alpha")
    assert fingerprint is not None and len(fingerprint) == 32
    assert fingerprint == repo_fingerprint(key, "repo-alpha")
    assert repo_fingerprint(key, "repo-beta") != fingerprint
    assert repo_fingerprint(None, "repo-alpha") is None
    assert repo_fingerprint(key, "") is None
    assert repo_fingerprint(key, None) is None
