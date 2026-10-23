"""Vendor the engine's fallback fonts into the no-CDN web build.

Usage: ``web_fonts.py OUT CACHE CJK`` where OUT is the built web client, CACHE
the persistent font cache and CJK is ``1`` to include CJK families (``0``
otherwise).

The engine fetches fallback fonts from ``<fontFallbackBaseUrl><family>/v<N>/<file>``,
which is ``assets/fonts/`` locally. This script extracts the engine's path list
from the built ``main.dart.js`` (version segments move with Flutter) and vendors
each file from gstatic through the cache. The web build enables all five CJK families.
Every font is structurally validated (WOFF2 header, table directory, compressed
stream, metadata/private blocks; sfnt table directory) before it reaches OUT or
the cache, and any fetch or validation failure fails the build loudly.
It was the last heredoc of ``scripts/build_web_client.sh``; the CLI is unchanged.
"""

import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import urllib.request
from pathlib import Path

CJK = ("notosanskr/", "notosansjp/", "notosanshk/", "notosanstc/", "notosanssc/")
FONT_NOTICES = (
    Path(__file__).resolve().parents[1]
    / "src/relaydeck/client/assets/design_system/fonts/web-fallback-notices.json"
)
NOTICE_MARKER = "\n\n" + "=" * 80 + "\nRelayDeck web fallback font notices\n\n"


class FontError(Exception):
    pass


def _u16(data: bytes, at: int) -> int:
    return struct.unpack_from(">H", data, at)[0]


def _u32(data: bytes, at: int) -> int:
    return struct.unpack_from(">I", data, at)[0]


def _base128(data: bytes, at: int) -> tuple[int, int]:
    # Strict UIntBase128: no leading zero group, at most 5 bytes, value
    # below 2**32, and the encoding must end inside the file.
    value = 0
    for i in range(5):
        if at >= len(data):
            raise FontError("truncated UIntBase128")
        byte = data[at]
        at += 1
        if i == 0 and byte == 0x80:
            raise FontError("UIntBase128 leading zero")
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            if value >= 2**32:
                raise FontError("UIntBase128 overflow")
            return value, at
    raise FontError("UIntBase128 exceeds 5 bytes")


def _check_woff2(data: bytes) -> None:
    # Declared length must equal the file size; parse the table directory per spec to find the
    # compressed stream, which must fit. Metadata/private blocks must be inside the file, 4-byte
    # aligned, non-overlapping and after the stream.
    if len(data) < 48 or data[:4] != b"wOF2":
        raise FontError("bad WOFF2 header")
    length = _u32(data, 8)
    total_sfnt, total_compressed = _u32(data, 16), _u32(data, 20)
    if length != len(data):
        raise FontError(f"declared length {length} != actual {len(data)}")
    if total_sfnt < 1 or total_compressed < 1:
        raise FontError("empty font data")
    if data[4:8] == b"ttcf":
        # Collections need a dedicated directory parser (entries first,
        # then version/counts/flavor/indices); none of the vendored fonts
        # uses this flavor, so reject loudly rather than guess bounds.
        raise FontError("WOFF2 collections (ttcf) are not supported")
    entries = _u16(data, 12)
    at = 48
    if entries < 1:
        raise FontError("no tables")
    # WOFF2 known-table index. Tag 63 = explicit 4-byte tag.
    known_tags = (
        b"cmap",
        b"head",
        b"hhea",
        b"hmtx",
        b"maxp",
        b"name",
        b"OS/2",
        b"post",
        b"cvt ",
        b"fpgm",
        b"glyf",
        b"loca",
        b"prep",
        b"CFF ",
        b"VORG",
        b"EBDT",
        b"EBLC",
        b"gasp",
        b"hdmx",
        b"kern",
        b"LTSH",
        b"PCLT",
        b"VDMX",
        b"vhea",
        b"vmtx",
        b"BASE",
        b"GDEF",
        b"GPOS",
        b"GSUB",
        b"EBSC",
        b"JSTF",
        b"MATH",
        b"CBDT",
        b"CBLC",
        b"COLR",
        b"CPAL",
        b"SVG ",
        b"sbix",
        b"acnt",
        b"avar",
        b"bdat",
        b"bloc",
        b"bsln",
        b"cvar",
        b"fdsc",
        b"feat",
        b"fmtx",
        b"fvar",
        b"gvar",
        b"hsty",
        b"just",
        b"lcar",
        b"mort",
        b"morx",
        b"opbd",
        b"prop",
        b"trak",
        b"Zapf",
        b"Silf",
        b"Glat",
        b"Gloc",
        b"Feat",
        b"Sill",
    )
    for _ in range(entries):
        if at >= len(data):
            raise FontError("truncated table directory")
        flags = data[at]
        at += 1
        index, version = flags & 0x3F, flags >> 6
        if index == 63:
            if at + 4 > len(data):
                raise FontError("truncated table tag")
            tag = data[at : at + 4]
            at += 4
        else:
            tag = known_tags[index]
        _, at = _base128(data, at)  # origLength
        transformed = (tag in (b"glyf", b"loca") and version == 0) or (
            tag == b"hmtx" and version == 1
        )
        if transformed:
            _, at = _base128(data, at)  # transformLength
    stream_end = at + total_compressed
    if stream_end > length:
        raise FontError("compressed stream exceeds file")
    for base in (28, 40):  # meta, then private block
        offset, size = _u32(data, base), _u32(data, base + 4)
        if not size:
            continue
        if offset % 4:
            raise FontError("metadata/private block misaligned")
        if offset < stream_end or offset + size > length:
            raise FontError("metadata/private block out of bounds")
    meta_offset, meta_size = _u32(data, 28), _u32(data, 32)
    priv_offset, priv_size = _u32(data, 40), _u32(data, 44)
    if (
        meta_size
        and priv_size
        and meta_offset < priv_offset + priv_size
        and priv_offset < meta_offset + meta_size
    ):
        raise FontError("metadata/private blocks overlap")


def _check_sfnt(data: bytes) -> None:
    # TrueType/OpenType table directory: every record must fit in the file.
    if len(data) < 12 or data[:4] not in (b"\x00\x01\x00\x00", b"OTTO"):
        raise FontError("bad sfnt header")
    num_tables = _u16(data, 4)
    if num_tables < 1 or 12 + num_tables * 16 > len(data):
        raise FontError("sfnt directory exceeds file")
    for i in range(num_tables):
        offset = _u32(data, 12 + i * 16 + 8)
        if offset + _u32(data, 12 + i * 16 + 12) > len(data):
            raise FontError("sfnt table out of bounds")


def _check_font(data: bytes, suffix: str) -> None:
    if suffix == ".woff2":
        _check_woff2(data)
    elif suffix in (".ttf", ".otf"):
        _check_sfnt(data)
    else:
        raise FontError(f"unsupported suffix {suffix}")


def _content_length(resp: object) -> int | None:
    try:
        get = getattr(resp, "getheader", None)
        value = get("Content-Length") if get else resp.headers.get("Content-Length")
    except Exception:
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise FontError("https only")
    req = urllib.request.Request(url, headers={"User-Agent": "RelayDeck-web-build/1.0"})  # noqa: S310 - https checked above
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - https checked above
        body = resp.read()
        advertised = _content_length(resp)
    if advertised is not None and advertised != len(body):
        raise FontError(f"Content-Length {advertised} != received {len(body)}")
    return body


def _install(data: bytes, dest: Path) -> None:
    # Unique sibling temp file plus atomic replace: safe for concurrent
    # cache writers and never leaves a half-written destination.
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)  # mkstemp is 0600; build output must stay servable
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _notices(paths: list[str]) -> dict[str, str]:
    manifest = json.loads(FONT_NOTICES.read_text())
    notices: dict[str, str] = {}
    for family in sorted({path.split("/")[0] for path in paths}):
        row = manifest.get(family, {})
        text = row.get("text", "")
        if (
            not text.startswith("Copyright")
            or "SIL OPEN FONT LICENSE Version 1.1" not in text
            or "TERMINATION" not in text
            or "DISCLAIMER" not in text
            or hashlib.sha256(text.encode()).hexdigest() != row.get("sha256")
        ):
            raise FontError(f"missing or invalid fallback font notice: {family}")
        notices[family] = f"{family}\nSource: {row['url']}\n\n{text}"
    return notices


def _write_notices(out: Path, paths: list[str]) -> None:
    # Include any fallback files retained from an earlier run against this OUT too.
    root = out / "assets" / "fonts"
    installed = [str(path.relative_to(root)) for path in root.glob("*/v*/*") if path.is_file()]
    notices = _notices(paths + installed)
    dest = out / "assets" / "NOTICES"
    existing = dest.read_text() if dest.exists() else ""
    base = existing.split(NOTICE_MARKER, 1)[0]
    separator = "\n\n" + "=" * 80 + "\n"
    _install((base + NOTICE_MARKER + separator.join(notices.values())).encode(), dest)
    packaged = dest.read_text()
    if any(text not in packaged for text in notices.values()):
        raise FontError("fallback font notices packaging check failed")
    print(f"fallback font notices: {len(notices)}/{len(notices)} families verified -> {dest}")


def main(argv: list[str]) -> None:
    out, cache, cjk = Path(argv[1]), Path(argv[2]), argv[3] == "1"
    js = (out / "main.dart.js").read_text()
    paths = sorted(set(re.findall(r'"([a-z0-9]+/v[0-9]+/[A-Za-z0-9_.-]+\.(?:woff2|ttf|otf))"', js)))
    if not paths:
        raise SystemExit("no fallback font paths found in main.dart.js")
    if not cjk:
        paths = [p for p in paths if not p.startswith(CJK)]
    elif {p.split("/")[0] + "/" for p in paths if p.startswith(CJK)} != set(CJK):
        raise FontError("CJK fallback families must include all five families")
    _notices(paths)  # Fail before publishing a font whose licence notice is missing.
    total = 0
    for rel in paths:
        dest = out / "assets" / "fonts" / rel
        cached = cache / rel
        try:
            data = cached.read_bytes()
            _check_font(data, cached.suffix)
        except (OSError, FontError):
            # Missing or invalid cache entry: re-fetch, validate before
            # publishing anything, and fail the build loudly on failure.
            last: Exception | None = None
            data = b""
            for _ in range(3):
                try:
                    data = _fetch("https://fonts.gstatic.com/s/" + rel)
                    _check_font(data, cached.suffix)
                    _install(data, cached)
                    last = None
                    break
                except Exception as exc:  # noqa: BLE001 - retried, then fatal
                    last = exc
            if last is not None:
                raise SystemExit(f"fallback font fetch failed: {rel}: {last}") from last
        _install(data, dest)
        _check_font(dest.read_bytes(), dest.suffix)  # never publish a truncated file
        total += dest.stat().st_size
    _write_notices(out, paths)
    print(
        f"fallback fonts: {len(paths)} files, {total} bytes "
        f"(cjk={'on' if cjk else 'off'}) -> {out}/assets/fonts/"
    )


if __name__ == "__main__":
    main(sys.argv)
