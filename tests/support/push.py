"""Scripted push provider for headless tests."""

from __future__ import annotations

from relaydeck.gateway.push import ProviderOutcome, ProviderPort


class StubProvider(ProviderPort):
    """Script entries may be outcomes or Exceptions (raised from send, treated as retryable)."""

    def __init__(
        self,
        script: list[ProviderOutcome | Exception] | None = None,
        default: ProviderOutcome = ProviderOutcome.SENT,
    ) -> None:
        self._script = list(script) if script else []
        self._default = default
        self.sends: list[tuple[str, dict[str, str]]] = []

    def send(self, token: str, payload: dict[str, str]) -> ProviderOutcome:
        self.sends.append((token, dict(payload)))
        if self._script:
            next_item = self._script.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item
        return self._default
