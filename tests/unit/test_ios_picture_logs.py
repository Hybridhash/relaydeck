"""The companion routes only the approved fixed-category picture diagnostics."""

import subprocess
import sys
from copy import deepcopy

from uvicorn.config import LOGGING_CONFIG

from relaydeck.companion.runtime import _uvicorn_logging_config


def test_picture_logger_has_one_info_sink_without_changing_uvicorn_defaults() -> None:
    before = deepcopy(LOGGING_CONFIG)
    config = _uvicorn_logging_config()
    assert config["loggers"]["relaydeck.client.features.terminal.pictures"] == {
        "level": "INFO",
        "handlers": ["default"],
        "propagate": False,
    }
    assert "root" not in config
    for name in ("uvicorn.error", "uvicorn.access"):
        expected = {**before["loggers"][name], "filters": ["redact_queries"]}
        assert config["loggers"][name] == expected
    assert config["loggers"]["uvicorn"] == before["loggers"]["uvicorn"]
    config["loggers"]["relaydeck.client.features.terminal.pictures"]["handlers"].append("extra")
    assert _uvicorn_logging_config()["loggers"]["relaydeck.client.features.terminal.pictures"][
        "handlers"
    ] == ["default"]
    assert LOGGING_CONFIG == before


def test_actual_companion_config_emits_picture_info_once_at_default_root_level() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
from types import SimpleNamespace
import flet as ft
import uvicorn
from relaydeck.companion.runtime import _uvicorn_logging_config
from relaydeck.client.features.terminal.pictures import _log_result, _PictureFeedback
root = logging.getLogger()
root.setLevel(logging.WARNING)
class Extra(logging.Handler):
    def emit(self, record):
        print('unexpected_root_record', record.getMessage())
root.addHandler(Extra())
uvicorn.Config(lambda *args: None, log_config=_uvicorn_logging_config())
assert root.level == logging.WARNING
_log_result([ft.FilePickerFile(id=7, name='PRIVATE_NAME', size=2_000_000,
    path='/Users/PRIVATE_PATH?grant=PRIVATE_GRANT&ticket=PRIVATE_TICKET',
    bytes=b'PRIVATE_IMAGE')])
_PictureFeedback(SimpleNamespace(), lambda _: None).stopped(
    SimpleNamespace(), 'insert', 'boundary_changed')
print('config_valid')
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    output = result.stdout + result.stderr
    assert "config_valid" in output
    assert output.count("Picture pick result: file_count=1 size_bucket=under_5mb") == 1
    assert output.count("Picture selection stopped: stage=insert reason=boundary_changed") == 1
    assert "unexpected_root_record" not in output
    assert "PRIVATE_" not in output
