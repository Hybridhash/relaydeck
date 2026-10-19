"""The opt-in terminal history baseline is typed, explicit and closed."""

import pytest

from relaydeck.contracts.schemas import SchemaError, validate

SCHEMA = "relaydeck/gateway/v1/terminal-control"
BASELINE = {"kind": "history_reset", "epoch": "e1", "offset": 524288, "incomplete": True}


def test_history_reset_accepts_explicit_baseline():
    validate(SCHEMA, BASELINE)


@pytest.mark.parametrize("field", ["epoch", "offset", "incomplete"])
def test_history_reset_requires_each_baseline_field(field):
    with pytest.raises(SchemaError):
        validate(SCHEMA, {key: value for key, value in BASELINE.items() if key != field})


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", ""),
        ("epoch", "x" * 129),
        ("offset", -1),
        ("offset", True),
        ("incomplete", "true"),
        ("extra", "untrusted"),
    ],
)
def test_history_reset_rejects_malformed_baseline(field, value):
    with pytest.raises(SchemaError):
        validate(SCHEMA, {**BASELINE, field: value})
