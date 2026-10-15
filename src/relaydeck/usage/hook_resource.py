"""Canonical reviewed wheel resource: verify the manifest AND compiled review pin."""

from __future__ import annotations

import hashlib
from importlib.resources import files

from relaydeck.usage.hook_files import HookFileError, document
from relaydeck.usage.hook_setup import (
    HOOK_FILENAME,
    HOOK_SHA256,
    HOOK_VERSION,
    PREVIOUS_HOOK_SHA256,
)


def hook_bytes() -> bytes:
    resource = files("relaydeck.usage.resources")
    with resource.joinpath("hook_manifest.json").open("rb") as stream:
        raw_manifest = stream.read(2049)
    if len(raw_manifest) > 2048:
        raise HookFileError("resource_invalid")
    manifest = document(raw_manifest)
    with resource.joinpath(HOOK_FILENAME).open("rb") as stream:
        body = stream.read(64 * 1024 + 1)
    if (
        manifest
        != {
            "version": HOOK_VERSION,
            "script": HOOK_FILENAME,
            "sha256": HOOK_SHA256,
            "previous_sha256": list(PREVIOUS_HOOK_SHA256),
        }
        or type(manifest.get("version")) is not int
        or len(body) > 64 * 1024
        or hashlib.sha256(body).hexdigest() != HOOK_SHA256
    ):
        raise HookFileError("resource_invalid")
    return body
