"""swipe origin protocol codec and page mapping units."""

import pytest
from relaydeck_terminal import protocol


def test_swipe_origin_values():
    assert protocol.ORIGIN_SWIPE == 4
    assert protocol.SWIPE_UP == 0
    assert protocol.SWIPE_DOWN == 1


@pytest.mark.parametrize(
    "direction,count,body",
    [
        (protocol.SWIPE_UP, 1, bytes([0, 1])),
        (protocol.SWIPE_DOWN, 23, bytes([1, 23])),
        (protocol.SWIPE_UP, 255, bytes([0, 255])),
    ],
)
def test_encode_swipe_round_trip(direction, count, body):
    raw = protocol.encode_swipe(direction, count)
    assert raw == protocol.encode_input(body, protocol.ORIGIN_SWIPE)
    buf = bytearray(raw)
    packets = protocol.decode_packets(buf)
    assert len(buf) == 0
    framed = bytes([protocol.ORIGIN_SWIPE]) + body
    assert packets == [protocol.DecodedPacket(protocol.INPUT, framed)]
    origin, payload = protocol.decode_input(packets[0].payload)
    assert origin == protocol.ORIGIN_SWIPE
    assert protocol.decode_swipe(payload) == (direction, count)


@pytest.mark.parametrize("bad", [-1, 2, 3, 255, "0", None, True])
def test_encode_swipe_bad_direction_rejected(bad):
    with pytest.raises(ValueError):
        protocol.encode_swipe(bad, 10)


@pytest.mark.parametrize("bad", [0, -1, 256, 1000, "23", None, True, 2.0])
def test_encode_swipe_bad_count_rejected(bad):
    with pytest.raises(ValueError):
        protocol.encode_swipe(protocol.SWIPE_DOWN, bad)


@pytest.mark.parametrize(
    "bad",
    [b"", b"\x01", b"\x01\x02\x03", b"\x02\x05", b"\x01\x00", b"\xff\x10"],
)
def test_decode_swipe_malformed_rejected(bad):
    with pytest.raises(ValueError):
        protocol.decode_swipe(bad)


@pytest.mark.parametrize(
    "count,page_lines,pages",
    [
        (1, 23, 1),
        (5, 23, 1),
        (12, 23, 1),
        (23, 23, 1),
        (24, 23, 1),
        (34, 23, 1),
        (35, 23, 2),
        (46, 23, 2),
        (255, 23, 2),
        (1, 1, 1),
        (3, 4, 1),
        (255, 1023, 1),
        (10, 4, 2),
    ],
)
def test_swipe_pages_bounded(count, page_lines, pages):
    from relaydeck.client.features.terminal.scrolling import swipe_pages

    assert swipe_pages(count, page_lines) == pages


def test_swipe_pages_never_zero_or_unbounded():
    from relaydeck.client.features.terminal.scrolling import swipe_pages

    for page_lines in (1, 2, 4, 23, 24, 100, 1024):
        for count in (1, 2, 7, 64, 127, 255):
            assert 1 <= swipe_pages(count, page_lines) <= protocol.MAX_SWIPE_PAGES
