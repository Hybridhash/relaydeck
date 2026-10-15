"""Content-free failure diagnostics for UI callback boundaries.

Exception messages, args, tracebacks and chained text can embed user data
(terminal bytes, paths, tokens, clipboard text), so callback failures are
logged as a fixed stage plus an allowlisted exception category only.
"""

from __future__ import annotations

# Literal stage labels used by `spawn` callers -> stable log ids. Only these
# are ever logged; anything else (including free text or a token that merely
# looks like an id) becomes "unknown".
_STAGES: dict[str, str] = {
    "terminal-size-report": "terminal-size-report",
    "terminal-input": "terminal-input",
    "terminal-mode-report": "terminal-mode-report",
    "terminal-view-report": "terminal-view-report",
    "terminal-bind": "terminal-bind",
    "terminal-unbind": "terminal-unbind",
    "terminal-scroll-hold": "terminal-scroll-hold",
    "picture-message-clear": "picture-message-clear",
    "phone-select-newest": "phone-select-newest",
    "Direct typing focus": "direct-typing-focus",
    "Close Direct typing focus": "close-direct-typing-focus",
}
# Builtin exception classes that may be named in logs. Anything else
# (third-party, application-defined) is reported as "other".
_CATEGORIES = frozenset(
    {
        "AssertionError",
        "AttributeError",
        "ConnectionError",
        "IndexError",
        "KeyError",
        "LookupError",
        "NotImplementedError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "StopAsyncIteration",
        "TimeoutError",
        "TypeError",
        "UnicodeError",
        "ValueError",
    }
)


def failure_category(exc: BaseException) -> str:
    """Nearest allowlisted builtin class name in the MRO, else "other"."""
    for cls in type(exc).__mro__:
        if cls.__module__ == "builtins" and cls.__name__ in _CATEGORIES:
            return cls.__name__
    return "other"


def safe_stage(label: object) -> str:
    """Stable id for a known literal stage label, else "unknown"."""
    return _STAGES.get(label, "unknown") if isinstance(label, str) else "unknown"
