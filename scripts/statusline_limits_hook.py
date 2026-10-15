"""Development shim; the reviewed standalone hook ships in the package resource."""

from relaydeck.usage.resources.statusline_limits_hook import main, persist, windows_from

__all__ = ["main", "persist", "windows_from"]


if __name__ == "__main__":
    raise SystemExit(main())
