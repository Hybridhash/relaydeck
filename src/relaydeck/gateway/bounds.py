"""Request, page and tunnel bounds the gateway enforces."""

# server-enforced bounded expiry for submitted commands.
MAX_EXPIRES_FUTURE_MS = 300_000

# 64 KiB JSON request bound, enforced for every HTTP route.
MAX_JSON_BODY = 64 * 1024

# Largest WebSocket frame uvicorn accepts: above the 1 MiB snapshot, far above every input frame.
WS_MAX_SIZE = 2 * 1024 * 1024

# Seconds uvicorn waits for open streams before it closes them on shutdown.
GRACEFUL_SHUTDOWN_S = 5

# 1 MiB bounded snapshot messages (compromised-connector DoS cap).
MAX_SNAPSHOT_BYTES = 1024 * 1024

# inventory page bound.
MAX_AGENTS_PER_PAGE = 200

# attachment/lease request bounds. These documents have no schema in
# contracts/schemas.py, so bodies are validated inline here and responses
# are plain versioned dicts (no validate() call) plus the shared error
# envelope.
ATTACHMENT_KINDS = ("agent", "shell")
LEASE_MODES = ("view", "control")
MAX_TERMINAL_ID_CHARS = 128
MAX_ATTACHMENT_ID_CHARS = 128
MAX_LEASE_ID_CHARS = 128
DEFAULT_PTY_COLS = 80
DEFAULT_PTY_ROWS = 24

# SSE keepalive: comment frames the parser skips.
EVENTS_HEARTBEAT_S = 15.0

# How often a live stream checks whether the gateway is still serving.
SHUTDOWN_POLL_S = 1.0

# live-push + tunnel pump bounds (OWASP WebSocket hardening:
# bounded polls, per-message size limits, single live tunnel per host).
TERMINAL_PUSH_POLL_S = 0.05
TUNNEL_POLL_S = 0.05
TUNNEL_MAX_TEXT = 64 * 1024
TUNNEL_MAX_OUTPUT = 64 * 1024
