"""stable error table convergence.

The envelope carries exactly 14 codes with fixed HTTP statuses.
Everything else (malformed bodies, unknown kinds, upstream Herdr
outcomes) maps into the table via details, never by widening it.
"""

from __future__ import annotations

from relaydeck.contracts.schemas import ERROR_CODES, ERROR_STATUS, validate


def test_error_table_has_exactly_fourteen_codes() -> None:
    assert ERROR_CODES == (
        "unauthenticated",
        "forbidden",
        "not_found",
        "stale_epoch",
        "stale_revision",
        "command_conflict",
        "unsupported_capability",
        "host_offline",
        "lease_conflict",
        "lease_expired",
        "upstream_incompatible",
        "outcome_unknown",
        "too_large",
        "rate_limited",
    )


def test_error_status_covers_table() -> None:
    assert set(ERROR_STATUS) == set(ERROR_CODES)
    assert ERROR_STATUS["unauthenticated"] == 401
    assert ERROR_STATUS["forbidden"] == 400
    assert ERROR_STATUS["not_found"] == 404
    assert ERROR_STATUS["stale_epoch"] == 409
    assert ERROR_STATUS["stale_revision"] == 409
    assert ERROR_STATUS["command_conflict"] == 409
    assert ERROR_STATUS["unsupported_capability"] == 403
    assert ERROR_STATUS["host_offline"] == 503
    assert ERROR_STATUS["lease_conflict"] == 409
    assert ERROR_STATUS["lease_expired"] == 410
    assert ERROR_STATUS["upstream_incompatible"] == 500
    assert ERROR_STATUS["outcome_unknown"] == 409
    assert ERROR_STATUS["too_large"] == 413
    assert ERROR_STATUS["rate_limited"] == 429


def test_every_code_validates_with_and_without_details() -> None:
    for code in ERROR_CODES:
        validate("relaydeck/gateway/v1/error", {"code": code, "message": "m"})
        validate(
            "relaydeck/gateway/v1/error",
            {"code": code, "message": "m", "retryable": True, "details": {"k": "v"}},
        )


def test_malformed_and_cursor_maps_validate() -> None:
    validate(
        "relaydeck/gateway/v1/error",
        {"code": "forbidden", "message": "malformed", "details": {"reason": "malformed"}},
    )
    validate(
        "relaydeck/gateway/v1/error",
        {
            "code": "stale_epoch",
            "message": "cursor invalid",
            "retryable": True,
            "details": {"reason": "cursor"},
        },
    )
    validate(
        "relaydeck/gateway/v1/error",
        {
            "code": "host_offline",
            "message": "herdr refused",
            "retryable": True,
            "details": {"upstream_code": "agent_blocked"},
        },
    )
