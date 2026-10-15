"""Shared persistence test helpers (moved out of test modules)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from relaydeck import persistence_contracts as limits
from relaydeck.persistence_contracts import KeyUnavailable, Record, Table


@dataclass(frozen=True)
class Config:
    state_dir: Path


class MemoryKeys:
    def __init__(self) -> None:
        self.keys: dict[str, bytes] = {}
        self.locked = False
        self.prompts: list[bool] = []

    def get(self, name: str, *, create: bool = False, allow_prompt: bool = False) -> bytes:
        self.prompts.append(allow_prompt)
        if self.locked:
            raise KeyUnavailable()
        if name not in self.keys:
            if not create:
                raise KeyUnavailable()
            self.keys[name] = secrets.token_bytes(32)
        return self.keys[name]

    def retire(self, name: str) -> None:
        if self.locked:
            raise KeyUnavailable()
        self.keys.pop(name, None)

    def import_secret(
        self, name: str, material: limits.SecretMaterial, *, allow_prompt: bool = False
    ) -> limits.SecretMaterial:
        if self.locked:
            raise KeyUnavailable()
        old = self.keys.setdefault("material/" + name, material.value)
        if old != material.value:
            raise KeyUnavailable()
        return limits.SecretMaterial(old)

    def read_secret(self, name: str) -> limits.SecretMaterial:
        if self.locked or "material/" + name not in self.keys:
            raise KeyUnavailable()
        return limits.SecretMaterial(self.keys["material/" + name])

    def restore_generation(
        self, name: str, *, create: bool = False, allow_prompt: bool = False
    ) -> limits.RestoreGeneration:
        if self.locked or ("restore/" + name not in self.keys and not create):
            raise KeyUnavailable()
        raw = self.keys.setdefault("restore/" + name, bytes(8))
        return limits.RestoreGeneration(int.from_bytes(raw, "big"))

    def advance_restore_generation(self, name: str) -> limits.RestoreGeneration:
        old = self.restore_generation(name)
        value = old.value + 1
        self.keys["restore/" + name] = value.to_bytes(8, "big")
        return limits.RestoreGeneration(value)


def record(key: str = "token", *, expiry: int = 2_000_000_000_000) -> Record:
    return Record(Table.WEB_SESSIONS, key, ("origin", "account", "device"), b"bearer", 1, 1, expiry)
