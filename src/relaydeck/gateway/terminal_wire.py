"""Compatibility exports for the neutral Q4 contract module."""

# compat: remove in Q4-cleanup after remaining server/test importers migrate.
from relaydeck.contracts.wire.terminal import (
    INPUT_V2 as INPUT_V2,
)
from relaydeck.contracts.wire.terminal import (
    KIND_KEY as KIND_KEY,
)
from relaydeck.contracts.wire.terminal import (
    KIND_NAMES as KIND_NAMES,
)
from relaydeck.contracts.wire.terminal import (
    KIND_RESIZE as KIND_RESIZE,
)
from relaydeck.contracts.wire.terminal import (
    KIND_TEXT as KIND_TEXT,
)
from relaydeck.contracts.wire.terminal import (
    MAX_INPUT_CHUNK as MAX_INPUT_CHUNK,
)
from relaydeck.contracts.wire.terminal import (
    MAX_KEY_LEN as MAX_KEY_LEN,
)
from relaydeck.contracts.wire.terminal import (
    MAX_OUTPUT_CHUNK as MAX_OUTPUT_CHUNK,
)
from relaydeck.contracts.wire.terminal import (
    MAX_RESIZE_CELL as MAX_RESIZE_CELL,
)
from relaydeck.contracts.wire.terminal import (
    OUTPUT_V2 as OUTPUT_V2,
)
from relaydeck.contracts.wire.terminal import (
    InputChunk as InputChunk,
)
from relaydeck.contracts.wire.terminal import (
    OutputChunk as OutputChunk,
)
from relaydeck.contracts.wire.terminal import (
    decode_input as decode_input,
)
from relaydeck.contracts.wire.terminal import (
    decode_output as decode_output,
)
from relaydeck.contracts.wire.terminal import (
    encode_input as encode_input,
)
from relaydeck.contracts.wire.terminal import (
    encode_key as encode_key,
)
from relaydeck.contracts.wire.terminal import (
    encode_output as encode_output,
)
from relaydeck.contracts.wire.terminal import (
    encode_resize as encode_resize,
)
from relaydeck.contracts.wire.terminal import (
    encode_text as encode_text,
)
from relaydeck.contracts.wire.terminal import (
    is_valid_key as is_valid_key,
)
