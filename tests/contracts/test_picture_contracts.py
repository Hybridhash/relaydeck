"""Examples and strict attachment permission/response boundaries."""

import json
from pathlib import Path

import pytest

from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.contracts.schemas import SchemaError, validate


@pytest.mark.parametrize("name", ["picture-request", "picture-grant", "picture-result"])
def test_picture_examples(name: str) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/gateway" / f"{name}.json"
    doc = json.loads(fixture.read_text())
    validate(f"relaydeck/gateway/v1/{name}", doc)
    with pytest.raises(SchemaError):
        validate(f"relaydeck/gateway/v1/{name}", {**doc, "device_id": "forged-owner"})


def test_pairing_has_upload_without_account_administration() -> None:
    assert "attachments.upload" in DEVICE_CAPABILITIES
    assert "hosts.enroll" not in DEVICE_CAPABILITIES


@pytest.mark.parametrize("preview", ["https://public.invalid/image.jpg", "a" * 24_001, None])
def test_preview_is_bounded_inline_base64_only(preview: object) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/gateway/picture-result.json"
    doc = json.loads(fixture.read_text())
    with pytest.raises(SchemaError):
        validate("relaydeck/gateway/v1/picture-result", {**doc, "preview": preview})
