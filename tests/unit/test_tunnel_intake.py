"""Byte-bounded high-cadence intake and atomic snapshot admission."""

import asyncio
import base64
import hashlib
import json

import pytest

from relaydeck.contracts.tunnel import SnapshotIntake
from relaydeck.tunnel_queue import MAX_BYTES, BoundedInbox, TunnelCapacityError, TunnelIntake


def test_a_second_of_fourteen_terminal_output_cadence_fits_document_admission():
    inbox = BoundedInbox()
    doc = {"kind": "terminal.output", "data_b64": "eA=="}
    for _ in range(14 * 200):
        inbox.put(doc, len(json.dumps(doc).encode()))
    assert inbox.count == 2800 and inbox.bytes < MAX_BYTES
    with pytest.raises(TunnelCapacityError, match="tunnel_queue_full"):
        inbox.put(doc, MAX_BYTES)


def test_snapshot_batch_counts_once_and_all_wire_bytes_count():
    async def scenario():
        inbox = BoundedInbox(frames=1)
        reader = TunnelIntake(inbox, SnapshotIntake())
        payload = json.dumps({"value": "x" * 65536}).encode()
        chunks = [payload[i : i + 32768] for i in range(0, len(payload), 32768)]
        docs = [
            {
                "kind": "snapshot.begin",
                "host_id": "h",
                "transfer_id": "s",
                "total_bytes": len(payload),
                "total_chunks": len(chunks),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ]
        docs += [
            {
                "kind": "snapshot.chunk",
                "host_id": "h",
                "transfer_id": "s",
                "seq": n,
                "data_b64": base64.b64encode(piece).decode(),
            }
            for n, piece in enumerate(chunks)
        ]
        docs.append({"kind": "snapshot.end", "host_id": "h", "transfer_id": "s"})
        for doc in docs:
            reader.put(doc, len(json.dumps(doc).encode()), "h", 1)
        assert inbox.count == 1
        assert inbox.bytes == sum(len(json.dumps(d).encode()) for d in docs)
        assert (await inbox.get())[0]["snapshot"] == json.loads(payload)

    asyncio.run(scenario())


def test_partial_snapshot_and_queued_docs_share_the_byte_bound():
    inbox = BoundedInbox(byte_limit=128)
    inbox.put({}, 100)
    reader = TunnelIntake(inbox, SnapshotIntake())
    with pytest.raises(TunnelCapacityError, match="tunnel_queue_full"):
        reader.put({"kind": "snapshot.begin"}, 29, "h", 1)
