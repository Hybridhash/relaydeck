"""Companion-only Uvicorn redaction; pinned protocol dispatch, no listeners."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from typing import Any
from urllib.parse import quote

import h11
import pytest
import uvicorn
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter
from uvicorn.protocols.http.flow_control import FlowControl
from uvicorn.protocols.http.h11_impl import RequestResponseCycle
from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol
from uvicorn.server import ServerState

from relaydeck.companion import runtime as runtime_module
from relaydeck.companion.runtime import CompanionRuntime, _uvicorn_logging_config

SECRETS = ("PRIVATE_TICKET", "PRIVATE_GRANT", "PRIVATE_QUERY", "PRIVATE_COOKIE")
QUERIES = (
    b"ticket=PRIVATE_TICKET&grant=PRIVATE_GRANT&query=PRIVATE_QUERY&cookie=PRIVATE_COOKIE",
    b'grant="PRIVATE_GRANT"&ticket=PRIVATE_TICKET\nPRIVATE_QUERY\tPRIVATE_COOKIE',
    b"query=PRIVATE_QUERY%20PRIVATE_COOKIE&ticket=PRIVATE_TICKET&grant=PRIVATE_GRANT",
    b"",
)


class Capture(logging.Handler):
    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(formatter)
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "msg": record.msg,
                "args": record.args,
                "text": self.format(record),
                "message": getattr(record, "message", ""),
            }
        )


def logger(name: str) -> tuple[logging.Logger, list[Capture]]:
    config = _uvicorn_logging_config()
    child = logging.Logger(name, level=logging.INFO)
    child.parent = logging.Logger("uvicorn", level=logging.INFO)
    child.propagate = True
    for filter_name in config["loggers"][name]["filters"]:
        child.addFilter(config["filters"][filter_name]["()"]())
    formatter = (
        AccessFormatter(fmt='%(client_addr)s "%(request_line)s" %(status_code)s', use_colors=False)
        if child.name == "uvicorn.access"
        else DefaultFormatter(fmt="%(message)s", use_colors=False)
    )
    # Two own + two ancestor sinks all see the same already-redacted record.
    captures = [Capture(formatter) for _ in range(4)]
    for handler in captures[:2]:
        child.addHandler(handler)
    for handler in captures[2:]:
        child.parent.addHandler(handler)
    return child, captures


def assert_private(captures: list[Capture], path: str, query: bytes, status: str) -> None:
    expected = quote(path) + ("?[redacted]" if query else "")
    for handler in captures:
        assert len(handler.records) == 1
        record = handler.records[0]
        assert expected in record["text"] and status in record["text"]
        assert record["args"][1 if "WebSocket" in record["msg"] else 2] == expected
        serialized = json.dumps(record)
        assert all(secret not in serialized for secret in SECRETS)
        assert all(key not in serialized for key in ("ticket=", "grant=", "query=", "cookie="))


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize(
    "message,status",
    [
        ({"type": "websocket.accept"}, "[accepted]"),
        ({"type": "websocket.close", "code": 1008}, "403"),
        ({"type": "websocket.http.response.start", "status": 401, "headers": []}, "401"),
    ],
)
def test_real_websocket_dispatch_redacts_accept_reject_and_http_denial_on_all_handlers(
    query: bytes,
    message: dict[str, Any],
    status: str,
) -> None:
    async def scenario() -> None:
        async def app(_scope: Any, _receive: Any, _send: Any) -> None:
            pass

        protocol = WebSocketProtocol(uvicorn.Config(app, log_config=None), ServerState(), {})
        protocol.logger, captures = logger("uvicorn.error")
        path = "/v1/terminal/lease safe?part"
        protocol.scope = {  # type: ignore[typeddict-item]
            "type": "websocket",
            "path": path,
            "query_string": query,
            "client": ("127.0.0.1", 1234),
        }
        await protocol.asgi_send(message)  # type: ignore[arg-type]
        assert_private(captures, path, query, status)

    asyncio.run(scenario())


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("status", [200, 403])
def test_real_http_response_dispatch_keeps_access_formatter_args_and_hides_queries(
    query: bytes,
    status: int,
) -> None:
    async def scenario() -> None:
        class Transport:
            def write(self, data: bytes) -> None:
                assert data.startswith(b"HTTP/1.1 ")

        transport = Transport()
        connection = h11.Connection(h11.SERVER)
        connection.receive_data(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert isinstance(connection.next_event(), h11.Request)
        access, captures = logger("uvicorn.access")
        path = "/v1/pictures/private preview"
        cycle = RequestResponseCycle(
            scope={
                "type": "http",
                "path": path,
                "query_string": query,  # type: ignore[typeddict-item]
                "client": ("127.0.0.1", 1234),
                "method": "GET",
                "http_version": "1.1",
                "headers": [],
            },
            conn=connection,
            transport=transport,
            flow=FlowControl(transport),  # type: ignore[arg-type]
            logger=logging.Logger("uvicorn.error"),
            access_logger=access,
            access_log=True,
            default_headers=[],
            message_event=asyncio.Event(),
            on_response=lambda: None,
        )
        await cycle.send({"type": "http.response.start", "status": status, "headers": []})
        assert_private(captures, path, query, str(status))
        assert all(handler.records[0]["args"][4] == status for handler in captures)

    asyncio.run(scenario())


def test_config_copies_defaults_and_attaches_filter_at_both_logger_boundaries() -> None:
    before = deepcopy(LOGGING_CONFIG)
    config = _uvicorn_logging_config()
    assert LOGGING_CONFIG == before
    for name in ("uvicorn.error", "uvicorn.access"):
        assert config["loggers"][name]["filters"] == ["redact_queries"]
        assert config["loggers"][name].get("handlers") == before["loggers"][name].get("handlers")
    config["loggers"]["uvicorn.error"]["filters"].append("unrelated")
    assert _uvicorn_logging_config()["loggers"]["uvicorn.error"]["filters"] == ["redact_queries"]
    assert LOGGING_CONFIG == before


def test_companion_passes_redaction_config_without_starting_a_real_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        observed: dict[str, Any] = {}

        def config(app: Any, **options: Any) -> Any:
            observed.update(options)
            return SimpleNamespace(app=app, **options)

        class Server:
            started = True

            async def serve(self) -> None:
                pass

        monkeypatch.setattr(uvicorn, "Config", config)
        monkeypatch.setattr(runtime_module, "_route_first_server", lambda *_: Server())
        runtime = CompanionRuntime.__new__(CompanionRuntime)
        runtime.config = SimpleNamespace(bind_host="127.0.0.1", port=8125)
        runtime.app = object()
        runtime.server_factory = None
        await runtime._start_server()
        await runtime.server_task
        for name in ("uvicorn.error", "uvicorn.access"):
            assert observed["log_config"]["loggers"][name]["filters"] == ["redact_queries"]
        assert observed["access_log"] is False and observed["proxy_headers"] is False

    asyncio.run(scenario())


def test_real_uvicorn_dictconfig_filters_named_loggers_in_fresh_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
import uvicorn
from relaydeck.companion.runtime import _uvicorn_logging_config
uvicorn.Config(lambda *args: None, log_config=_uvicorn_logging_config(), access_log=True)
class Extra(logging.StreamHandler):
    def emit(self, record):
        print("extra", repr(record.msg), repr(record.args), record.getMessage())
for name in ("uvicorn.error", "uvicorn.access"):
    logging.getLogger(name).addHandler(Extra())
target = "/v1/terminal/lease?ticket=PRIVATE_TICKET&grant=PRIVATE_GRANT&query=PRIVATE_QUERY"
logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "local", target)
logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" 403', "local", target)
logging.getLogger("uvicorn.access").info(
    '%s - "%s %s HTTP/%s" %d', "local", "GET", target, "1.1", 200)
""",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert all(secret not in output for secret in SECRETS)
    assert all(key not in output for key in ("ticket=", "grant=", "query="))
    assert output.count("extra") == 3 and "?[redacted]" in output
    assert "[accepted]" in output and "403" in output and "200 OK" in output


@pytest.mark.parametrize("name", ["uvicorn.error", "uvicorn.access"])
@pytest.mark.parametrize("immutable", [False, True])
def test_filter_handles_mapping_args_and_preformatted_targets_idempotently(
    name: str,
    immutable: bool,
) -> None:
    config = _uvicorn_logging_config()
    filter_ = config["filters"]["redact_queries"]["()"]()
    record = logging.LogRecord(
        name,
        logging.INFO,
        "",
        1,
        "%(target)s",
        (
            (MappingProxyType if immutable else dict)(
                {"target": "/path?grant=PRIVATE_GRANT\nPRIVATE_QUERY"}
            ),
        ),
        None,
    )
    record.message = record.getMessage()
    assert filter_.filter(record) and filter_.filter(record)
    assert record.args == {"target": "/path?[redacted]"}
    assert record.message == "/path?[redacted]"
    flat = logging.LogRecord(
        name, logging.INFO, "", 1, "WebSocket /path?ticket=PRIVATE_TICKET [accepted]", (), None
    )
    assert filter_.filter(flat)
    assert flat.getMessage() == "WebSocket /path?[redacted] [accepted]"
