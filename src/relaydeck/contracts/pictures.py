"""Device-bound private picture upload contract; never an HTTP asset URL."""

from relaydeck.contracts.pairing import document

PICTURE_SCHEMAS = {
    "relaydeck/gateway/v1/picture-request": document(
        "picture-request",
        {
            "host_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    ),
    "relaydeck/gateway/v1/picture-grant": document(
        "picture-grant",
        {
            "picture_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            "upload_url": {
                "type": "string",
                "pattern": "^/v1/pictures/[a-f0-9]{32}/content\\?grant=[A-Za-z0-9_-]{43}$",
            },
            "expires_ms": {"type": "integer", "minimum": 0},
        },
    ),
    "relaydeck/gateway/v1/picture-result": document(
        "picture-result",
        {
            "picture_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            "status": {"enum": ["pending", "uploading", "ready", "failed"]},
            "message": {"type": "string", "maxLength": 160},
            "path": {"type": "string", "maxLength": 4096},
            "expires_ms": {"type": "integer", "minimum": 0},
            "preview": {
                "type": "string",
                "maxLength": 24_000,
                "pattern": "^[A-Za-z0-9+/]*={0,2}$",
            },
        },
    ),
}
