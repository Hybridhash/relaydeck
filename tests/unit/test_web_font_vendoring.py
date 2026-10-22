"""Font-vendoring integrity for the no-CDN web build.

The vendoring step lives in ``scripts/web_fonts.py`` (invoked by
``scripts/build_web_client.sh``); these tests run that exact script against
scratch OUT/cache dirs with a mocked ``urlopen``, so the shipped pipeline —
not a copy — is covered.
Truncated or malformed fonts must never reach OUT or poison the cache.

The WOFF2 positive fixture is a real vendored font
(``tests/fixtures/fallback-sample.woff2``: Noto Sans Lycian subset,
``notosanslycian/v15/QldVNSNMqAsHtsJ7UmqxBQA9r8wA5_zaCJwn00E.woff2``,
2172 bytes, sha256
``d3cedd06b088ff89664beacbff91e60082d3e48b6bba8d074b0b24aee38b74da``).
Its SIL OFL 1.1 notice is ``tests/fixtures/OFL-fallback-sample.txt``.
"""

from __future__ import annotations

import contextlib
import io
import json
import runpy
import struct
import sys
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
REL = "notosanssymbols2/v1/probe.woff2"


def _run_vendoring_script() -> None:
    script = (REPO / "scripts/build_web_client.sh").read_text()
    assert "web client ready" in script
    # The build must call the shipped script with OUT, cache and the CJK flag,
    # before it writes the reproducibility manifest and reports success.
    call = 'scripts/web_fonts.py" "$OUT" "$RD_FONT_CACHE" "${RD_WEB_CJK_FONTS:-1}"'
    assert call in script
    assert script.index(call) < script.rindex("web client ready")
    runpy.run_path(str(REPO / "scripts/web_fonts.py"), run_name="__main__")


def _valid_woff2() -> bytes:
    return (REPO / "tests/fixtures/fallback-sample.woff2").read_bytes()


def _woff2_single_table(
    orig_length: bytes = b"\x10",
    stream: bytes = b"Z" * 16,
    meta: tuple[int, int] = (0, 0),
    priv: tuple[int, int] = (0, 0),
    flavor: bytes = b"\x00\x01\x00\x00",
    collection: bytes = b"",
    compressed: int | None = None,
) -> bytes:
    """Minimal synthetic font: header + one cmap entry + stream.

    The validator never decodes brotli, so the stream is arbitrary bytes;
    every structural field is exact. ``orig_length`` is the raw
    UIntBase128 payload (one cmap entry, flags 0x00), which malformedness
    cases override.
    """
    entry = b"\x00" + orig_length
    body = stream
    meta_offset, meta_length = meta
    priv_offset, priv_length = priv
    if meta_offset:
        start = 48 + len(collection) + len(entry) + len(body)
        body += bytes(max(0, meta_offset - start))
        body += bytes(meta_length)
    if priv_offset:
        end = 48 + len(collection) + len(entry) + len(body)
        body += bytes(max(0, priv_offset - end))
        body += bytes(priv_length)
    total = 48 + len(collection) + len(entry) + len(body)
    header = struct.pack(
        ">4sIIHHIIHHIIIII",
        b"wOF2",
        int.from_bytes(flavor, "big"),
        total,
        1,
        0,
        100,
        len(stream) if compressed is None else compressed,
        1,
        0,
        meta_offset,
        meta_length,
        0,
        priv_offset,
        priv_length,
    )
    assert len(header) == 48
    return header + collection + entry + body


def _valid_sfnt(magic: bytes, total: int = 64) -> bytes:
    header = struct.pack(">4sHHHH", magic, 1, 16, 0, 16)
    record = struct.pack(">4sIII", b"head", 0, 28, total - 28)
    return header + record + bytes(total - 28)


class _FakeResponse(io.BytesIO):
    """BytesIO with an optional Content-Length header."""

    def __init__(self, body: bytes, content_length: int | None = -1) -> None:
        super().__init__(body)
        self._length = len(body) if content_length == -1 else content_length

    def getheader(self, name: str, default=None):
        if name.lower() == "content-length" and self._length is not None:
            return str(self._length)
        return default


def _run(
    js: str,
    cache_bytes: dict[str, bytes] | None = None,
    response: bytes | _FakeResponse | Exception | None = None,
    cjk: str = "0",
):
    root = Path(tempfile.mkdtemp(prefix="web-fonts-", dir="/private/tmp"))
    out = root / "out"
    cache = root / "cache"
    out.mkdir(parents=True)
    (out / "main.dart.js").write_text(js)
    for rel, data in (cache_bytes or {}).items():
        target = cache / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    calls: list[str] = []

    def fetch(req, timeout):
        calls.append(req.full_url)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise AssertionError("unexpected network access")
        if isinstance(response, _FakeResponse):
            return response
        return io.BytesIO(response)

    error = None
    with (
        patch.object(sys, "argv", ["vendor", str(out), str(cache), cjk]),
        patch.object(urllib.request, "urlopen", fetch),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        try:
            _run_vendoring_script()
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
    return out, cache, calls, error


def _dest(out: Path, rel: str = REL) -> Path:
    return out / "assets" / "fonts" / rel


def test_truncated_http_body_rejected() -> None:
    good = _valid_woff2()
    out, cache, calls, error = _run(
        json.dumps(REL),
        response=_FakeResponse(good[:128], content_length=len(good)),
    )
    assert error is not None
    assert not _dest(out).exists()
    assert not list(cache.rglob("*part*"))


def test_short_body_without_content_length_rejected() -> None:
    # No header to compare against: structure alone must reject it.
    out, _cache, _calls, error = _run(json.dumps(REL), response=_valid_woff2()[:100])
    assert error is not None
    assert not _dest(out).exists()


def test_compressed_size_equal_to_file_size_rejected() -> None:
    # Review repro: only the compressed-size field changes, so declared
    # length still matches; header + directory + stream cannot fit.
    good = bytearray(_valid_woff2())
    struct.pack_into(">I", good, 20, len(good))
    out, _cache, _calls, error = _run(json.dumps(REL), response=bytes(good))
    assert error is not None
    assert not _dest(out).exists()


def test_directory_plus_stream_overflow_rejected() -> None:
    # Compressed size alone fits, but header + directory + stream exceed
    # the file (stream starts at 48 + >=2 directory bytes).
    good = bytearray(_valid_woff2())
    struct.pack_into(">I", good, 20, len(good) - 49)
    out, _cache, _calls, error = _run(json.dumps(REL), response=bytes(good))
    assert error is not None
    assert not _dest(out).exists()


def test_magic_only_cache_refetched() -> None:
    good = _valid_woff2()
    out, cache, calls, error = _run(json.dumps(REL), cache_bytes={REL: b"wOF2"}, response=good)
    assert error is None, error
    assert len(calls) == 1
    assert _dest(out).read_bytes() == good
    assert (cache / REL).read_bytes() == good


def test_truncated_cache_refetched() -> None:
    good = _valid_woff2()
    out, cache, calls, error = _run(json.dumps(REL), cache_bytes={REL: good[:100]}, response=good)
    assert error is None, error
    assert len(calls) == 1
    assert _dest(out).read_bytes() == good


def test_html_body_rejected() -> None:
    out, cache, _calls, error = _run(json.dumps(REL), response=b"<html>not a font</html>")
    assert error is not None
    assert not _dest(out).exists()
    assert not (cache / REL).exists()
    assert not list(cache.rglob("*part*"))


def test_valid_cache_reused_offline() -> None:
    good = _valid_woff2()
    out, _cache, calls, error = _run(json.dumps(REL), cache_bytes={REL: good})
    assert error is None, error
    assert calls == []
    assert _dest(out).read_bytes() == good


def test_valid_ttf_and_otf_accepted() -> None:
    for suffix, magic in (("ttf", b"\x00\x01\x00\x00"), ("otf", b"OTTO")):
        rel = f"notosanssymbols/v1/probe.{suffix}"
        good = _valid_sfnt(magic)
        out, _cache, calls, error = _run(json.dumps(rel), cache_bytes={rel: good})
        assert error is None, error
        assert calls == []
        assert _dest(out, rel).read_bytes() == good


def test_truncated_sfnt_directory_rejected() -> None:
    rel = "notosanssymbols/v1/probe.ttf"
    good = bytearray(_valid_sfnt(b"\x00\x01\x00\x00", total=48))
    struct.pack_into(">H", good, 4, 3)  # claims 3 records, file holds 1
    out, _cache, _calls, error = _run(json.dumps(rel), response=bytes(good))
    assert error is not None
    assert not _dest(out, rel).exists()


def test_valid_real_font_accepted_over_http() -> None:
    good = _valid_woff2()
    out, cache, calls, error = _run(json.dumps(REL), response=good)
    assert error is None, error
    assert len(calls) == 1
    assert _dest(out).read_bytes() == good
    assert (cache / REL).read_bytes() == good


def test_malformed_uintbase128_rejected() -> None:
    cases = {
        "leading zero": b"\x80\x10",
        "over 5 bytes": b"\x81\x80\x80\x80\x80\x00",
        "overflow": b"\x90\x80\x80\x80\x00",  # 2**32
    }
    for label, encoding in cases.items():
        font = _woff2_single_table(orig_length=encoding)
        out, _cache, _calls, error = _run(json.dumps(REL), response=font)
        assert error is not None, label
        assert not _dest(out).exists(), label


def test_truncated_directory_entry_rejected() -> None:
    # origLength encoding runs past EOF while declared length matches.
    font = _woff2_single_table(orig_length=b"\x81\x82", stream=b"", compressed=4)
    out, _cache, _calls, error = _run(json.dumps(REL), response=font)
    assert error is not None
    assert not _dest(out).exists()


def test_collection_flavor_rejected() -> None:
    # No vendored font uses ttcf; the flavor is rejected outright rather
    # than parsed approximately (covers the review's cache/HTTP overflow).
    collection = struct.pack(">IH", 0x00010000, 1) + struct.pack(">H", 1)
    font = _woff2_single_table(flavor=b"ttcf", collection=collection)
    out, _cache, _calls, error = _run(json.dumps(REL), response=font)
    assert error is not None
    assert not _dest(out).exists()
    out, _cache, calls, error = _run(json.dumps(REL), cache_bytes={REL: font})
    assert error is not None
    assert len(calls) == 3  # invalid cache entry triggers re-fetch, then fails
    assert not _dest(out).exists()


def test_aligned_meta_block_accepted() -> None:
    # Stream ends at 66; meta block sits after it, 4-aligned, in-file.
    font = _woff2_single_table(meta=(68, 4))
    out, _cache, calls, error = _run(json.dumps(REL), response=font)
    assert error is None, error
    assert len(calls) == 1
    assert _dest(out).read_bytes() == font


def test_misaligned_meta_block_rejected() -> None:
    font = _woff2_single_table(meta=(67, 4))
    out, _cache, _calls, error = _run(json.dumps(REL), response=font)
    assert error is not None
    assert not _dest(out).exists()


def test_overlapping_meta_priv_rejected() -> None:
    font = _woff2_single_table(meta=(68, 4), priv=(68, 8))
    out, _cache, _calls, error = _run(json.dumps(REL), response=font)
    assert error is not None
    assert not _dest(out).exists()


def test_font_notices_packaged_with_original_flutter_notices_and_idempotent() -> None:
    good = _valid_woff2()
    out, cache, _calls, error = _run(json.dumps(REL), cache_bytes={REL: good})
    assert error is None, error
    api = runpy.run_path(str(REPO / "scripts/web_fonts.py"))
    dest = out / "assets/NOTICES"
    dest.write_text("Original Flutter notices\n" + dest.read_text())
    with patch.object(sys, "argv", ["vendor", str(out), str(cache), "0"]):
        _run_vendoring_script()
        _run_vendoring_script()
    notice = json.loads(api["FONT_NOTICES"].read_text())["notosanssymbols2"]["text"]
    assert notice in dest.read_text()
    assert dest.read_text().startswith("Original Flutter notices\n")
    assert dest.read_text().count(api["NOTICE_MARKER"]) == 1


@pytest.mark.parametrize("broken", ["missing", "truncated"])
def test_missing_or_corrupt_font_notice_fails_build(tmp_path: Path, broken: str) -> None:
    api = runpy.run_path(str(REPO / "scripts/web_fonts.py"))
    notice = tmp_path / "notices.json"
    rows = json.loads(api["FONT_NOTICES"].read_text())
    if broken == "missing":
        del rows["notosanssymbols2"]
    else:
        rows["notosanssymbols2"]["text"] = "Copyright; SIL OPEN FONT LICENSE Version 1.1"
    notice.write_text(json.dumps(rows))
    api["_notices"].__globals__["FONT_NOTICES"] = notice
    with pytest.raises(api["FontError"], match="missing or invalid fallback font notice"):
        api["_notices"]([REL])


def test_unknown_font_family_fails_before_download_or_publication() -> None:
    rel = "notosansexampleunlicensed/v1/probe.woff2"
    out, cache, calls, error = _run(json.dumps(rel), response=_valid_woff2())
    assert error is not None and "missing or invalid fallback font notice" in error
    assert calls == []
    assert not _dest(out, rel).exists()
    assert not (cache / rel).exists()


def test_cjk_fonts_all_five_or_none_and_notices_packaged() -> None:
    families = ("notosanskr", "notosansjp", "notosanshk", "notosanstc", "notosanssc")
    paths = [REL, *(f"{family}/v1/probe.woff2" for family in families)]
    cached = dict.fromkeys(paths, _valid_woff2())
    out, _cache, calls, error = _run(json.dumps(paths), cache_bytes=cached, cjk="1")
    assert error is None, error
    assert calls == []
    for rel in paths:
        assert _dest(out, rel).is_file()
        assert rel.split("/")[0] in (out / "assets/NOTICES").read_text()
    out, _cache, calls, error = _run(json.dumps(paths), cache_bytes=cached, cjk="0")
    assert error is None, error
    assert calls == []
    assert _dest(out).is_file()
    assert all(not _dest(out, rel).exists() for rel in paths[1:])
    _out, _cache, _calls, error = _run(json.dumps(paths[:-1]), cache_bytes=cached, cjk="1")
    assert error is not None and "all five" in error
