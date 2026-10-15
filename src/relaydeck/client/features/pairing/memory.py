"""Server-held web attempt secrets; never used for Android credentials."""

from relaydeck.client.adapters.ports import CredentialStore


class MemorySecrets(CredentialStore):
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def available(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def clear_account(self) -> None:
        self.values.clear()
