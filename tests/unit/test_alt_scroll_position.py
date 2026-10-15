from relaydeck.client.features.terminal.scrolling import MAX_ALT_PAGES, AltScrollPosition


def test_alt_scroll_counts_net_distance_and_caps_explicit_jump() -> None:
    position = AltScrollPosition(page_lines=23)
    position.move(23)
    position.move(23)
    position.move(-23)
    position.move(1)
    assert position.latest() == (b"\x1b[6~",) * 2
    position.move(-24)
    assert position.lines == 0 and position.latest() == ()
    position.move(23 * 1000)
    assert position.lines == 23 * MAX_ALT_PAGES
    assert position.latest() == (b"\x1b[6~",) * MAX_ALT_PAGES
    position.move(-100000)
    assert position.lines == 0
