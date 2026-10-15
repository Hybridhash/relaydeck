"""Bounded connector tunnel documents, separate from the upstream API.

These additive gateway envelopes do not introduce Herdr methods. Both tunnel
ends validate the same input shape; snapshot content uses the frozen schema.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, TypedDict

from relaydeck.contracts.keys import MAX_KEY_LEN, is_valid_key

MAX_INPUT_BYTES = 16 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SNAPSHOT_CHUNKS = 128
MAX_SNAPSHOT_CHUNK_BYTES = 48 * 1024
SNAPSHOT_TIMEOUT_S = 10.0
SEND_TIMEOUT_S = 5.0


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("bad tunnel identity")
    return value


def _integer(value: Any, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("bad tunnel integer")
    return value


@dataclass(frozen=True)
class TerminalInput:
    terminal_id: str
    epoch: str
    lease_id: str
    generation: int
    input_seq: int
    kind: str
    data: bytes = b""
    cols: int = 0
    rows: int = 0


class _InputIdentity(TypedDict):
    terminal_id: str
    epoch: str
    lease_id: str
    generation: int
    input_seq: int


def parse_terminal_input(doc: dict[str, Any], host_id: str) -> TerminalInput:
    if doc.get("host_id") != host_id:
        raise ValueError("input host mismatch")
    common: _InputIdentity = {
        "terminal_id": _identifier(doc.get("terminal_id")),
        "epoch": _identifier(doc.get("epoch")),
        "lease_id": _identifier(doc.get("lease_id")),
        "generation": _integer(doc.get("generation"), 1, 0xFFFFFFFF),
        "input_seq": _integer(doc.get("input_seq"), 1, 0xFFFFFFFF),
    }
    if doc.get("kind") == "terminal.resize":
        return TerminalInput(
            **common,
            kind="resize",
            cols=_integer(doc.get("cols"), 1, 1024),
            rows=_integer(doc.get("rows"), 1, 1024),
        )
    if doc.get("kind") != "terminal.input" or doc.get("input_kind") not in ("text", "key"):
        raise ValueError("bad tunnel input kind")
    encoded = doc.get("data_b64")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_INPUT_BYTES + 2) // 3) * 4:
        raise ValueError("bad tunnel input size")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    text = data.decode("utf-8")
    if doc["input_kind"] == "key" and (len(data) > MAX_KEY_LEN or not is_valid_key(text)):
        raise ValueError("bad key combo")
    return TerminalInput(**common, kind=doc["input_kind"], data=data)


@dataclass
class SnapshotIntake:
    """One transfer per tunnel; strict order, byte/digest and time bounds."""

    transfer_id: str = ""
    total_bytes: int = 0
    total_chunks: int = 0
    sha256: str = ""
    deadline: float = 0
    next_seq: int = 0
    buffer: bytearray = field(default_factory=bytearray)

    @property
    def active(self) -> bool:
        return bool(self.transfer_id)

    def check_deadline(self, now: float) -> None:
        if self.active and now >= self.deadline:
            raise ValueError("snapshot transfer expired")

    def accept(self, doc: dict[str, Any], host_id: str, now: float) -> dict[str, Any] | None:
        self.check_deadline(now)
        if doc.get("host_id") != host_id:
            raise ValueError("snapshot host mismatch")
        kind = doc.get("kind")
        transfer_id = _identifier(doc.get("transfer_id"))
        if kind == "snapshot.begin":
            if self.active:
                raise ValueError("snapshot already active")
            total_bytes = _integer(doc.get("total_bytes"), 1, MAX_SNAPSHOT_BYTES)
            total_chunks = _integer(doc.get("total_chunks"), 1, MAX_SNAPSHOT_CHUNKS)
            digest = doc.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
                raise ValueError("bad snapshot digest")
            self.transfer_id, self.total_bytes, self.total_chunks = (
                transfer_id,
                total_bytes,
                total_chunks,
            )
            self.sha256, self.deadline = digest, now + SNAPSHOT_TIMEOUT_S
            return None
        if not self.active or transfer_id != self.transfer_id:
            raise ValueError("unknown snapshot transfer")
        if kind == "snapshot.chunk":
            if (
                self.next_seq >= self.total_chunks
                or _integer(doc.get("seq"), 0, MAX_SNAPSHOT_CHUNKS - 1) != self.next_seq
            ):
                raise ValueError("snapshot chunk order")
            encoded = doc.get("data_b64")
            if not isinstance(encoded, str) or len(encoded) > MAX_SNAPSHOT_CHUNK_BYTES * 4 // 3:
                raise ValueError("snapshot chunk too large")
            piece = base64.b64decode(encoded, validate=True)
            if not piece or len(self.buffer) + len(piece) > self.total_bytes:
                raise ValueError("bad snapshot byte count")
            self.buffer.extend(piece)
            self.next_seq += 1
            return None
        if kind != "snapshot.end" or self.next_seq != self.total_chunks:
            raise ValueError("snapshot transfer incomplete")
        if (
            len(self.buffer) != self.total_bytes
            or hashlib.sha256(self.buffer).hexdigest() != self.sha256
        ):
            raise ValueError("snapshot integrity mismatch")
        snapshot = json.loads(self.buffer)
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot not an object")
        self.transfer_id = ""
        self.buffer.clear()
        self.next_seq = 0
        return snapshot
