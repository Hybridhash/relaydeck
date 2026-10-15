"""Picture upload builder."""

from __future__ import annotations

import io

from PIL import Image


def picture(kind: str = "PNG", exif: bool = False, size: tuple[int, int] = (32, 24)) -> bytes:
    out = io.BytesIO()
    metadata = Image.Exif()
    metadata[0x010E] = "private image metadata"
    metadata[274] = 6
    metadata[34853] = {1: "N", 2: (10.0, 0.0, 0.0), 3: "E", 4: (20.0, 0.0, 0.0)}
    Image.new("RGB", size, "red").save(out, format=kind, **({"exif": metadata} if exif else {}))
    return out.getvalue()
