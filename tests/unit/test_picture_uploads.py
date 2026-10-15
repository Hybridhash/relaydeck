"""Companion image sanitization, private retention, authenticated streamed intake."""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from relaydeck.companion.pictures import DAY_MS, MAX_BYTES, PictureError, PictureStore, sniff
from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.gateway.app import create_app
from tests.security.helpers import auth, make_state
from tests.support.pictures.uploads import picture


@pytest.mark.parametrize("kind", ["PNG", "JPEG", "WEBP"])
def test_sniff_decode_strip_and_private_retention(tmp_path: Path, kind: str) -> None:
    store = PictureStore(tmp_path / "pictures")
    encoded = picture(kind, exif=True)
    with Image.open(io.BytesIO(encoded)) as source:
        assert source.getexif().get_ifd(34853)
    assert sniff(encoded) == kind
    path = Path(store.save(encoded, DAY_MS + 1000))
    assert path.parent == store.root and path.name != "spoof.exe"
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    with Image.open(path) as image:
        assert not image.getexif() and "exif" not in image.info
        assert image.size == (24, 32)
    store.cleanup(2 * DAY_MS + 999)
    assert path.exists()
    store.cleanup(2 * DAY_MS + 1000)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["PNG", "JPEG", "WEBP"])
def test_private_thumbnail_size_orientation_and_metadata(tmp_path: Path, kind: str) -> None:
    original, preview = PictureStore(tmp_path).save_with_preview(
        picture(kind, exif=True, size=(768, 512)), 1000
    )
    assert 0 < len(preview) <= 24_000
    with Image.open(io.BytesIO(base64.b64decode(preview, validate=True))) as image:
        assert image.format == "JPEG" and image.size == (32, 48)
        assert not image.getexif()
        assert not {"exif", "icc_profile", "xmp", "comment"}.intersection(image.info)
    with Image.open(original) as image:
        assert image.size == (512, 768)


def test_thumbnail_flattens_transparency_and_failure_leaves_no_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaydeck.companion import pictures

    data = io.BytesIO()
    Image.new("RGBA", (32, 24), (255, 0, 0, 0)).save(data, format="PNG")
    _, preview = PictureStore(tmp_path / "good").save_with_preview(data.getvalue(), 0)
    with Image.open(io.BytesIO(base64.b64decode(preview))) as image:
        assert image.getpixel((10, 10)) == (255, 255, 255)

    def failure(_: bytes) -> str:
        raise PictureError("forbidden", "Invalid preview")

    monkeypatch.setattr(pictures, "thumbnail", failure)
    with pytest.raises(PictureError):
        PictureStore(tmp_path / "failed").save_with_preview(picture(), 0)
    assert not (tmp_path / "failed").exists()


@pytest.mark.parametrize("removal", ["expiry", "cleanup_missing", "result_missing"])
def test_thumbnail_lifetime_matches_original(tmp_path: Path, removal: str) -> None:
    store = PictureStore(tmp_path)
    identifier, record = store.begin("a", "one", "mac", "token", 1000)
    record.path, record.preview = store.save_with_preview(picture(), 1000)
    record.status, record.expires_ms = "ready", DAY_MS + 1000
    path = Path(record.path)
    if removal == "expiry":
        store.cleanup(record.expires_ms)
    else:
        path.unlink()
        if removal == "cleanup_missing":
            store.cleanup(1001)
        else:
            with pytest.raises(PictureError, match="not found"):
                store.result(identifier, record)
    assert not path.exists() and identifier not in store.uploads
    assert record.path == "" and record.preview == ""


def test_sniff_spoof_and_size_and_symlink(tmp_path: Path) -> None:
    for payload in (b"MZ.exe", b"<svg></svg>", b"\x89PNG\r\n\x1a\ninvalid"):
        with pytest.raises(PictureError):
            PictureStore(tmp_path / "pictures").save(payload, 0)
    with pytest.raises(PictureError, match="10 MB"):
        PictureStore(tmp_path).save(b"x" * (MAX_BYTES + 1), 0)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        PictureStore(link).save(picture(), 0)


@pytest.mark.parametrize("decoder_installed", [False, True])
def test_heic_rejected_without_importing_optional_codec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decoder_installed: bool
) -> None:
    import sys
    from types import SimpleNamespace

    def forbidden_import() -> None:
        raise AssertionError("HEIF codec must not be activated")

    monkeypatch.setitem(
        sys.modules,
        "pillow_heif",
        SimpleNamespace(register_heif_opener=forbidden_import) if decoder_installed else None,
    )
    data = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
    assert sniff(data) == "HEIF"
    with pytest.raises(PictureError, match="send a JPEG"):
        PictureStore(tmp_path).save(data, 0)


def test_rate_limit_is_per_device_and_hour() -> None:
    store = PictureStore()
    for _ in range(20):
        store.begin("a", "device1", "mac", "token", 1000)
    with pytest.raises(PictureError, match="limit"):
        store.begin("a", "device1", "mac", "token", 1001)
    store.begin("a", "device2", "mac", "token", 1001)
    store.begin("a", "device1", "mac", "token", 3_601_000)


@pytest.mark.parametrize("boundary", ["expiry", "revoke", "cancel"])
def test_boundary_loss_during_stream_never_stores_file(tmp_path: Path, boundary: str) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        clock = [state.now_ms()]
        state.now_ms = lambda: clock[0]
        state.pictures = PictureStore(tmp_path / "pictures")
        token, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, clock[0], device_id="one"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://test"
        ) as client:
            grant = (
                await client.post("/v1/pictures", headers=auth(token), json={"host_id": "home-mac"})
            ).json()

            async def stream():
                data = picture()
                yield data[:16]
                if boundary == "expiry":
                    clock[0] = grant["expires_ms"]
                elif boundary == "revoke":
                    state.sessions.revoke_session(token)
                else:
                    state.pictures.cancel(grant["picture_id"])
                yield data[16:]

            response = await client.put(grant["upload_url"], headers=auth(token), content=stream())
            assert response.status_code == 401
            assert not state.pictures.root.exists()

    asyncio.run(scenario())


def test_startup_cleanup_keeps_unrelated_and_symlink_entries(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, _ = make_state(now=3 * DAY_MS)
        state.pictures = PictureStore(tmp_path / "pictures")
        old = Path(state.pictures.save(picture(), DAY_MS))
        recent = Path(state.pictures.save(picture(), 3 * DAY_MS))
        unrelated = state.pictures.root / "keep.txt"
        unrelated.write_text("unrelated user work")
        symlink = state.pictures.root / ("f" * 48 + ".jpg")
        symlink.symlink_to(unrelated)
        app = create_app(state)
        async with app.router.lifespan_context(app):
            assert not old.exists() and recent.exists()
            assert unrelated.exists() and symlink.is_symlink()

    asyncio.run(scenario())


def test_upload_contract_scope_device_replay_expiry_and_no_http_assets(tmp_path: Path) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        state.pictures = PictureStore(tmp_path / "pictures", host_id="home-mac")
        cap = frozenset(DEVICE_CAPABILITIES)
        token, _ = state.sessions.create("a", cap, {"home-mac"}, state.now_ms(), device_id="one")
        other, _ = state.sessions.create("a", cap, {"home-mac"}, state.now_ms(), device_id="two")
        missing, _ = state.sessions.create(
            "a", cap - {"attachments.upload"}, {"home-mac"}, state.now_ms(), device_id="one"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://test"
        ) as client:
            unauthenticated = await client.post("/v1/pictures", json={"host_id": "home-mac"})
            assert unauthenticated.status_code == 401
            assert unauthenticated.headers["cache-control"] == "private, no-store"
            assert (
                await client.post(
                    "/v1/pictures", headers=auth(missing), json={"host_id": "home-mac"}
                )
            ).status_code == 403
            grant_response = await client.post(
                "/v1/pictures", headers=auth(token), json={"host_id": "home-mac"}
            )
            assert grant_response.headers["cache-control"] == "private, no-store"
            grant = grant_response.json()
            identifier, url = grant["picture_id"], grant["upload_url"]
            assert "preview" not in grant and "path" not in grant
            pending = await client.get(f"/v1/pictures/{identifier}", headers=auth(token))
            assert pending.json()["preview"] == "" and pending.json()["path"] == ""
            assert (
                await client.put(
                    url,
                    content=picture(),
                    headers={**auth(token), "content-type": "application/exe"},
                )
            ).status_code == 204
            assert (
                await client.put(url, headers=auth(token), content=picture())
            ).status_code == 400
            result = await client.get(f"/v1/pictures/{identifier}", headers=auth(token))
            path = Path(result.json()["path"])
            assert path.exists() and result.json()["status"] == "ready"
            assert result.headers["cache-control"] == "private, no-store"
            with Image.open(io.BytesIO(base64.b64decode(result.json()["preview"]))) as image:
                assert image.format == "JPEG" and max(image.size) <= 48
            # A grant is not authorization to read metadata or the thumbnail.
            denied = await client.get(
                f"/v1/pictures/{identifier}",
                params={"grant": state.pictures.uploads[identifier].secret},
            )
            assert denied.status_code == 401 and "preview" not in denied.json()
            for method in ("GET", "DELETE"):
                foreign = await client.request(
                    method, f"/v1/pictures/{identifier}", headers=auth(other)
                )
                assert foreign.status_code == 404 and "preview" not in foreign.json()
                assert foreign.headers["cache-control"] == "private, no-store"
            assert (await client.get(f"/attachments/{path.name}")).status_code == 404
            record = state.pictures.uploads[identifier]
            deleted = await client.delete(f"/v1/pictures/{identifier}", headers=auth(token))
            assert deleted.status_code == 204
            assert deleted.headers["cache-control"] == "private, no-store"
            assert not path.exists() and identifier not in state.pictures.uploads
            assert record.preview == "" and record.path == ""
            assert (
                await client.get(f"/v1/pictures/{identifier}", headers=auth(token))
            ).status_code == 404
            new = (
                await client.post("/v1/pictures", headers=auth(token), json={"host_id": "home-mac"})
            ).json()
            state.sessions.revoke_session(token)
            assert (
                await client.put(new["upload_url"], headers=auth(token), content=picture())
            ).status_code == 401

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["revoke", "cancel"])
def test_thumbnail_worker_result_losing_authorization_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        store = state.pictures = PictureStore(tmp_path)
        token, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://test"
        ) as client:
            grant = (
                await client.post("/v1/pictures", headers=auth(token), json={"host_id": "home-mac"})
            ).json()
            record = store.uploads[grant["picture_id"]]
            saved = store.save_with_preview
            generated: list[tuple[str, str]] = []

            def finish_worker(data: bytes, now: int) -> tuple[str, str]:
                result = saved(data, now)
                generated.append(result)
                if boundary == "revoke":
                    state.sessions.revoke_session(token)
                else:
                    store.cancel(grant["picture_id"])
                return result

            monkeypatch.setattr(store, "save_with_preview", finish_worker)
            response = await client.put(grant["upload_url"], headers=auth(token), content=picture())
            assert response.status_code == 401
            assert response.headers["cache-control"] == "private, no-store"
            assert generated[0][1] and not Path(generated[0][0]).exists()
            assert record.preview == "" and record.path == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("cap", ["header", "stream"])
def test_early_and_streamed_caps(tmp_path: Path, cap: str) -> None:
    async def scenario() -> None:
        state, _ = make_state()
        state.pictures = PictureStore(tmp_path / "pictures")
        token, _ = state.sessions.create(
            "a", DEVICE_CAPABILITIES, {"home-mac"}, state.now_ms(), device_id="one"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://test"
        ) as client:
            grant = (
                await client.post("/v1/pictures", headers=auth(token), json={"host_id": "home-mac"})
            ).json()
            chunks = [0]

            async def stream():
                for _ in range(11):
                    chunks[0] += 1
                    yield b"x" * 1024 * 1024

            result = await client.put(
                grant["upload_url"],
                content=stream(),
                headers={**auth(token), "content-length": str(MAX_BYTES + 1)}
                if cap == "header"
                else auth(token),
            )
            assert result.status_code == 413
            assert chunks[0] == (0 if cap == "header" else 11)
            assert not state.pictures.root.exists()

    asyncio.run(scenario())
