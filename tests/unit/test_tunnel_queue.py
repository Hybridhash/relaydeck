"""Bounded in-flight admission, actual-send receipts and atomic snapshot batches."""

import asyncio

import pytest

from relaydeck.tunnel_queue import BoundedInbox, TunnelCapacityError, TunnelWriter, cancel_tasks


def test_inflight_items_keep_capacity_and_small_control_reserve() -> None:
    async def scenario() -> None:
        queue: BoundedInbox[str] = BoundedInbox(1, 10, control_reserve=1)
        queue.put("data", 5)
        entry = await queue.get()
        with pytest.raises(TunnelCapacityError):
            queue.put("another", 1)
        queue.put("pong", 1, control=True)
        with pytest.raises(TunnelCapacityError):
            queue.put("ping", 1, control=True)
        assert queue.count == 2 and queue.bytes == 6
        queue.finish(entry)
        queue.finish(await queue.get())
        assert queue.count == queue.bytes == 0
        with pytest.raises(TunnelCapacityError):
            queue.put("oversized", 11)

    asyncio.run(scenario())


def test_writer_receipt_is_actual_send_and_batch_is_contiguous() -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def send(text: str) -> None:
            if not sent:
                entered.set()
                await release.wait()
            sent.append(text)

        writer = TunnelWriter(send, lambda: True)
        runner = asyncio.create_task(writer.run())
        batch = asyncio.create_task(writer.batch(("begin", "chunk", "end")))
        await entered.wait()
        assert not batch.done() and writer.inbox.count == 1
        pong = writer.post(("pong",), control=True)
        release.set()
        assert await batch and await pong
        assert sent == ["begin", "chunk", "end", "pong"]
        await cancel_tasks([runner])

    asyncio.run(scenario())


def test_queued_dispatch_checks_deadline_at_writer_attempt_and_never_retries() -> None:
    async def scenario() -> None:
        release, entered = asyncio.Event(), asyncio.Event()
        sent, markers = [], []
        deadline_live = [True]

        async def send(text: str) -> None:
            entered.set()
            await release.wait()
            sent.append(text)

        async def marker() -> bool:
            markers.append(deadline_live[0])
            return deadline_live[0]

        writer = TunnelWriter(send, lambda: True)
        runner = asyncio.create_task(writer.run())
        first = writer.post(("held",))
        await entered.wait()
        second = writer.post(("expired_command",), before=marker)
        assert not markers
        deadline_live[0] = False
        release.set()
        assert await first and not await second
        assert sent == ["held"] and markers == [False]
        await cancel_tasks([runner])

    asyncio.run(scenario())


def test_superseded_writer_drops_queued_work_and_settles_receipts() -> None:
    async def scenario() -> None:
        current = [True]
        sent = []

        async def send(text: str) -> None:
            sent.append(text)

        writer = TunnelWriter(send, lambda: current[0])
        receipt = writer.post(("old_input",))
        current[0] = False
        with pytest.raises(ConnectionError):
            await writer.run()
        with pytest.raises(ConnectionError):
            await receipt
        assert not sent and writer.inbox.count == 0

    asyncio.run(scenario())


def test_writer_saturation_fails_connection_and_settles_queued_receipts():
    async def scenario():
        async def send(text):
            raise AssertionError("full writer must not forward queued work")

        writer = TunnelWriter(send, lambda: True)
        writer.inbox.frames = 1
        receipt = writer.post(("input",))
        with pytest.raises(TunnelCapacityError):
            writer.post(("unadmitted",))
        with pytest.raises(ConnectionError):
            await receipt
        with pytest.raises(ConnectionError):
            await writer.run()
        assert writer.inbox.count == writer.inbox.bytes == 0

    asyncio.run(scenario())


def test_writer_timeout_is_uncertain_and_never_retried(monkeypatch):
    async def scenario():
        calls = []

        async def send(text):
            calls.append(text)
            await asyncio.Event().wait()

        monkeypatch.setattr("relaydeck.tunnel_queue.SEND_TIMEOUT_S", 0.01)
        writer = TunnelWriter(send, lambda: True)
        receipt = writer.post(("uncertain",))
        queued = writer.post(("later",))
        with pytest.raises(TimeoutError):
            await writer.run()
        for future in [receipt, queued]:
            with pytest.raises(ConnectionError):
                await future
        assert calls == ["uncertain"]
        assert writer.inbox.count == writer.inbox.bytes == 0

    asyncio.run(scenario())
