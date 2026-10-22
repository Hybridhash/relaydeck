"""Gateway<->client terminal WebSocket framing v2.

Binary messages carry bytes; JSON text messages carry control documents
(validated against relaydeck/gateway/v1/terminal-control: ack, gap,
reset, bye). v1 (0x01) frames are rejected: no silent downgrade.

Client -> gateway input (0x02):
  [0x02][u16 lease_len][lease utf8][u32 generation][u32 input_seq]
  [u8 kind][u32 len][payload]
  kind 0x01 text:   payload is UTF-8 (validated), <= 16 KiB
  kind 0x02 key:     payload is a Herdr key-combo name (validated
                    against relaydeck.contracts.keys), <= 64 bytes
  kind 0x03 resize:  payload is exactly [u16 cols][u16 rows], 1..1024

Gateway -> client output (0x03):
  [0x03][u16 epoch_len][epoch utf8][u64 offset][u32 len][bytes <= 64 KiB]
`offset` is the cumulative byte offset of this chunk. An epoch change
means reset; an offset gap means resync. Gap/reset markers themselves
ride the text control channel.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from relaydeck.contracts.keys import MAX_KEY_LEN as MAX_KEY_LEN
from relaydeck.contracts.keys import is_valid_key as is_valid_key

INPUT_V2 = 0x02
OUTPUT_V2 = 0x03

KIND_TEXT = 0x01
KIND_KEY = 0x02
KIND_RESIZE = 0x03

KIND_NAMES = {KIND_TEXT: "text", KIND_KEY: "key", KIND_RESIZE: "resize"}

MAX_OUTPUT_CHUNK = 64 * 1024
MAX_INPUT_CHUNK = 16 * 1024
MAX_RESIZE_CELL = 1024


@dataclass(frozen=True)
class OutputChunk:
    epoch: str
    offset: int
    data: bytes


@dataclass(frozen=True)
class InputChunk:
    lease_id: str
    generation: int
    input_seq: int
    kind: str
    data: bytes
    cols: int = 0
    rows: int = 0


def encode_output(epoch: str, offset: int, data: bytes) -> bytes:
    if len(data) > MAX_OUTPUT_CHUNK:
        raise ValueError("output chunk too large")
    epoch_bytes = epoch.encode()
    if len(epoch_bytes) > 0xFFFF:
        raise ValueError("epoch too long")
    return (
        struct.pack(">BH", OUTPUT_V2, len(epoch_bytes))
        + epoch_bytes
        + struct.pack(">QI", offset, len(data))
        + data
    )


def decode_output(frame: bytes) -> OutputChunk:
    if len(frame) < 3 or frame[0] != OUTPUT_V2:
        raise ValueError("bad output frame")
    (epoch_len,) = struct.unpack_from(">H", frame, 1)
    head = 3 + epoch_len
    if len(frame) < head + 12:
        raise ValueError("bad output frame")
    try:
        epoch = frame[3:head].decode()
    except UnicodeDecodeError as exc:
        raise ValueError("bad output epoch") from exc
    offset, length = struct.unpack_from(">QI", frame, head)
    if length > MAX_OUTPUT_CHUNK or len(frame) != head + 12 + length:
        raise ValueError("bad output length")
    return OutputChunk(epoch, offset, frame[head + 12 :])


def encode_input(lease_id: str, generation: int, input_seq: int, kind: str, data: bytes) -> bytes:
    marker = {v: k for k, v in KIND_NAMES.items()}.get(kind)
    if marker is None:
        raise ValueError("unknown input kind")
    if kind == "text":
        if len(data) > MAX_INPUT_CHUNK:
            raise ValueError("input chunk too large")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("text is not UTF-8") from exc
    elif kind == "key":
        if len(data) > MAX_KEY_LEN or not data:
            raise ValueError("bad key length")
        try:
            name = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("key is not UTF-8") from exc
        if not is_valid_key(name):
            raise ValueError(f"unknown key {name!r}")
    else:  # resize
        if len(data) != 4:
            raise ValueError("bad resize payload")
        cols, rows = struct.unpack(">HH", data)
        if not (1 <= cols <= MAX_RESIZE_CELL and 1 <= rows <= MAX_RESIZE_CELL):
            raise ValueError("resize out of range")
    lease = lease_id.encode()
    if len(lease) > 0xFFFF:
        raise ValueError("lease id too long")
    return (
        struct.pack(">BH", INPUT_V2, len(lease))
        + lease
        + struct.pack(">IIBI", generation, input_seq, marker, len(data))
        + data
    )


def encode_text(lease_id: str, generation: int, input_seq: int, data: bytes) -> bytes:
    return encode_input(lease_id, generation, input_seq, "text", data)


def encode_key(lease_id: str, generation: int, input_seq: int, name: str) -> bytes:
    return encode_input(lease_id, generation, input_seq, "key", name.encode())


def encode_resize(lease_id: str, generation: int, input_seq: int, cols: int, rows: int) -> bytes:
    return encode_input(lease_id, generation, input_seq, "resize", struct.pack(">HH", cols, rows))


def decode_input(frame: bytes) -> InputChunk:
    if len(frame) < 3 or frame[0] != INPUT_V2:
        raise ValueError("bad input frame")
    (lease_len,) = struct.unpack_from(">H", frame, 1)
    head = 3 + lease_len
    if len(frame) < head + 13:
        raise ValueError("bad input frame")
    try:
        lease_id = frame[3:head].decode()
    except UnicodeDecodeError as exc:
        raise ValueError("bad input lease") from exc
    generation, input_seq, marker, length = struct.unpack_from(">IIBI", frame, head)
    kind = KIND_NAMES.get(marker)
    if kind is None:
        raise ValueError("unknown input kind")
    if length > MAX_INPUT_CHUNK or len(frame) != head + 13 + length:
        raise ValueError("bad input length")
    data = frame[head + 13 :]
    cols = rows = 0
    if kind == "text":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("text is not UTF-8") from exc
    elif kind == "key":
        if len(data) > MAX_KEY_LEN or not data:
            raise ValueError("bad key length")
        try:
            name = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("key is not UTF-8") from exc
        if not is_valid_key(name):
            raise ValueError(f"unknown key {name!r}")
    else:
        if len(data) != 4:
            raise ValueError("bad resize payload")
        cols, rows = struct.unpack(">HH", data)
        if not (1 <= cols <= MAX_RESIZE_CELL and 1 <= rows <= MAX_RESIZE_CELL):
            raise ValueError("resize out of range")
    return InputChunk(lease_id, generation, input_seq, kind, data, cols, rows)
