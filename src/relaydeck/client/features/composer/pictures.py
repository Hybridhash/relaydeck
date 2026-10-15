"""Inert Mac picture references retained with secure composer drafts."""

import base64
import re
import unicodedata
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Picture:
    picture_id: str
    path: str
    expires_ms: int
    preview: str = ""

    @classmethod
    def from_wire(cls, doc: Any) -> "Picture":
        if (
            not isinstance(doc, dict)
            or not isinstance(doc.get("picture_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", doc["picture_id"])
            or not isinstance(doc.get("path"), str)
            or not doc["path"].startswith("/")
            or any(unicodedata.category(c) in ("Cc", "Cs") for c in doc["path"])
            or len(doc["path"]) > 4096
            or type(doc.get("expires_ms")) is not int
            or not isinstance(doc.get("preview", ""), str)
            or len(doc.get("preview", "")) > 24_000
        ):
            raise ValueError("Invalid picture reference")
        if doc.get("preview"):
            preview = base64.b64decode(doc["preview"], validate=True)
            if not preview.startswith(b"\xff\xd8\xff"):
                raise ValueError("Invalid picture preview")
        return cls(doc["picture_id"], doc["path"], doc["expires_ms"], doc.get("preview", ""))

    def document(self) -> dict[str, Any]:
        return {
            "picture_id": self.picture_id,
            "path": self.path,
            "expires_ms": self.expires_ms,
            "preview": self.preview,
        }

    def prompt_line(self) -> str:
        # Plain prompt text, NOT a claim of an upstream structured-image API.
        return f"[image: {self.path}]"
