"""Device-local memory snapshots; never a credential/draft storage document."""

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from relaydeck.client.features.shell.ops import AttachmentInfo

SCREEN_CACHE_SLOTS = 8
SCREEN_CACHE_BYTES = 256 * 1024


@dataclass(frozen=True)
class ScreenSnapshot:
    text: str
    host_epoch: str
    attachment: AttachmentInfo
    stream_epoch: str = ""
    applied_offset: int = 0


class ScreenCache:
    def __init__(self) -> None:
        self._screens: OrderedDict[tuple[str, str, str], ScreenSnapshot] = OrderedDict()

    def put(self, key: tuple[str, str, str], snapshot: ScreenSnapshot) -> bool:
        if len(snapshot.text.encode("utf-8")) > SCREEN_CACHE_BYTES:
            self._screens.pop(key, None)
            return False
        self._screens[key] = snapshot
        self._screens.move_to_end(key)
        while len(self._screens) > SCREEN_CACHE_SLOTS:
            self._screens.popitem(last=False)
        return True

    def get(
        self, key: tuple[str, str, str], host_epoch: str, attachment: AttachmentInfo | None = None
    ) -> ScreenSnapshot | None:
        snapshot = self._screens.get(key)
        if snapshot is None:
            return None
        if snapshot.host_epoch != host_epoch or (
            attachment is not None
            and (
                snapshot.attachment.attachment_id,
                snapshot.attachment.epoch,
                snapshot.attachment.generation,
            )
            != (attachment.attachment_id, attachment.epoch, attachment.generation)
        ):
            self._screens.pop(key, None)
            return None
        self._screens.move_to_end(key)
        return snapshot

    def discard(self, key: tuple[str, str, str]) -> None:
        self._screens.pop(key, None)

    def prune(self, hosts: Mapping[str, str], resources: set[tuple[str, str, str]]) -> None:
        for key, snapshot in list(self._screens.items()):
            if key not in resources or hosts.get(key[0]) != snapshot.host_epoch:
                self.discard(key)

    def clear(self, host_id: str | None = None) -> None:
        if host_id is None:
            self._screens.clear()
        else:
            for key in list(self._screens):
                if key[0] == host_id:
                    del self._screens[key]

    def __len__(self) -> int:
        return len(self._screens)
