"""Headless macOS Keychain keys; no CLI, cache, plaintext fallback or implicit UI."""

from __future__ import annotations

import ctypes as ct
import fcntl
import hashlib
import hmac
import os
import secrets
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from relaydeck.persistence_contracts import (
    BUSY_TIMEOUT_MS,
    MAX_SECRET_BYTES,
    KeyUnavailable,
    RestoreGeneration,
    SecretMaterial,
)

_KEYCHAIN_LOCK = threading.Lock()
_NOT_FOUND = -25300
_DUPLICATE = -25299
_GENERATION_PREFIX = b"RDGEN1"


def _generation(raw: bytes) -> int:
    if not raw.startswith(_GENERATION_PREFIX) or len(raw) != len(_GENERATION_PREFIX) + 8:
        raise KeyUnavailable()
    return int.from_bytes(raw[len(_GENERATION_PREFIX) :], "big")


class _Callbacks(ct.Structure):
    _fields_ = [
        ("version", ct.c_long),
        ("retain", ct.c_void_p),
        ("release", ct.c_void_p),
        ("description", ct.c_void_p),
        ("equal", ct.c_void_p),
    ]


class _KeyCallbacks(ct.Structure):
    _fields_ = _Callbacks._fields_ + [("hash", ct.c_void_p)]


class _PromptSelector(ct.Structure):
    _fields_ = [("version", ct.c_uint16), ("flags", ct.c_uint16)]


class _Keychain:
    def __init__(self, service: str) -> None:
        self.service = service
        self._lock_root = Path("/private/tmp")
        self.cf = ct.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec = ct.CDLL("/System/Library/Frameworks/Security.framework/Security")
        signatures = {
            "CFRelease": ([ct.c_void_p], None),
            "CFStringCreateWithCString": ([ct.c_void_p, ct.c_char_p, ct.c_uint32], ct.c_void_p),
            "CFDataCreate": ([ct.c_void_p, ct.c_void_p, ct.c_long], ct.c_void_p),
            "CFDataGetLength": ([ct.c_void_p], ct.c_long),
            "CFDataGetBytePtr": ([ct.c_void_p], ct.c_void_p),
            "CFGetTypeID": ([ct.c_void_p], ct.c_ulong),
            "CFDataGetTypeID": ([], ct.c_ulong),
            "CFDictionaryCreate": (
                [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_long, ct.c_void_p, ct.c_void_p],
                ct.c_void_p,
            ),
            "CFArrayCreate": ([ct.c_void_p, ct.c_void_p, ct.c_long, ct.c_void_p], ct.c_void_p),
            "CFArrayGetCount": ([ct.c_void_p], ct.c_long),
            "CFArrayGetValueAtIndex": ([ct.c_void_p, ct.c_long], ct.c_void_p),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.cf, name)
            fn.argtypes, fn.restype = args, result
        for name, args in {
            "SecKeychainCopyDefault": [ct.c_void_p],
            "SecKeychainGetStatus": [ct.c_void_p, ct.c_void_p],
            "SecKeychainGetUserInteractionAllowed": [ct.c_void_p],
            "SecKeychainSetUserInteractionAllowed": [ct.c_ubyte],
            "SecItemCopyMatching": [ct.c_void_p, ct.c_void_p],
            "SecItemAdd": [ct.c_void_p, ct.c_void_p],
            "SecItemDelete": [ct.c_void_p],
            "SecItemUpdate": [ct.c_void_p, ct.c_void_p],
            "SecTrustedApplicationCreateFromPath": [ct.c_char_p, ct.c_void_p],
            "SecAccessCreate": [ct.c_void_p, ct.c_void_p, ct.c_void_p],
            "SecKeychainItemCopyAccess": [ct.c_void_p, ct.c_void_p],
            "SecACLCopyContents": [ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p],
            "SecTrustedApplicationCopyData": [ct.c_void_p, ct.c_void_p],
        }.items():
            fn = getattr(self.sec, name)
            fn.argtypes, fn.restype = args, ct.c_int32
        self.sec.SecAccessCopyMatchingACLList.argtypes = [ct.c_void_p, ct.c_void_p]
        self.sec.SecAccessCopyMatchingACLList.restype = ct.c_void_p
        self.key_callbacks = _KeyCallbacks.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks")
        self.value_callbacks = _Callbacks.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks")
        self.array_callbacks = _Callbacks.in_dll(self.cf, "kCFTypeArrayCallBacks")

    def _constant(self, name: str) -> int:
        lib = self.cf if name == "kCFBooleanTrue" else self.sec
        value = ct.c_void_p.in_dll(lib, name).value
        if value is None:
            raise KeyUnavailable()
        return value

    def _access(self, keep: Callable[[int | None], int]) -> int:
        """Legacy ACL: only the current interpreter is a no-confirmation reader."""
        trusted, access = ct.c_void_p(), ct.c_void_p()
        status = self.sec.SecTrustedApplicationCreateFromPath(None, ct.byref(trusted))
        trusted_ref = keep(trusted.value)
        if status:
            raise KeyUnavailable()
        apps = (ct.c_void_p * 1)(trusted_ref)
        array = keep(self.cf.CFArrayCreate(None, apps, 1, ct.byref(self.array_callbacks)))
        label = keep(
            self.cf.CFStringCreateWithCString(None, b"RelayDeck private storage", 0x08000100)
        )
        status = self.sec.SecAccessCreate(label, array, ct.byref(access))
        result = keep(access.value)
        if status:
            raise KeyUnavailable()
        return result

    def is_locked(self) -> bool:
        """Read-only native acceptance probe; never locks/unlocks a user's Keychain."""
        with _KEYCHAIN_LOCK:
            chain, state = ct.c_void_p(), ct.c_uint32()
            try:
                if self.sec.SecKeychainCopyDefault(ct.byref(chain)) or not chain.value:
                    raise KeyUnavailable()
                if self.sec.SecKeychainGetStatus(chain, ct.byref(state)):
                    raise KeyUnavailable()
                return not bool(state.value & 1)
            finally:
                if chain.value:
                    self.cf.CFRelease(chain)

    def trusts_current_application(self, name: str) -> bool:
        """Inspect the native decrypt ACL without modifying it or permitting UI."""
        with _KEYCHAIN_LOCK:
            refs: list[int] = []
            previous = ct.c_ubyte()
            changed = False

            def keep(value: int | None) -> int:
                if not value:
                    raise KeyUnavailable()
                refs.append(value)
                return value

            def copied(function: Callable[..., int], *args: object) -> int:
                result = ct.c_void_p()
                status = function(*args, ct.byref(result))
                value = keep(result.value)
                if status:
                    raise KeyUnavailable()
                return value

            try:
                if self.sec.SecKeychainGetUserInteractionAllowed(ct.byref(previous)):
                    raise KeyUnavailable()
                if self.sec.SecKeychainSetUserInteractionAllowed(False):
                    raise KeyUnavailable()
                changed = True
                chain = copied(self.sec.SecKeychainCopyDefault)
                item = ct.c_void_p()
                with self._query(name, ct.c_void_p(chain), allow_prompt=False, reference=True) as q:
                    status = self.sec.SecItemCopyMatching(q, ct.byref(item))
                item_ref = keep(item.value)
                if status:
                    raise KeyUnavailable()
                access = copied(self.sec.SecKeychainItemCopyAccess, item_ref)
                acls = keep(
                    self.sec.SecAccessCopyMatchingACLList(
                        access, self._constant("kSecACLAuthorizationDecrypt")
                    )
                )
                current = copied(self.sec.SecTrustedApplicationCreateFromPath, None)

                def data(application: int) -> bytes:
                    raw = copied(self.sec.SecTrustedApplicationCopyData, application)
                    return ct.string_at(self.cf.CFDataGetBytePtr(raw), self.cf.CFDataGetLength(raw))

                expected = data(current)
                if self.cf.CFArrayGetCount(acls) != 1:
                    return False
                acl = self.cf.CFArrayGetValueAtIndex(acls, 0)
                apps, description, prompt = ct.c_void_p(), ct.c_void_p(), _PromptSelector()
                status = self.sec.SecACLCopyContents(
                    acl, ct.byref(apps), ct.byref(description), ct.byref(prompt)
                )
                # Both copy results are owned even on an error path.
                if apps.value:
                    refs.append(apps.value)
                if description.value:
                    refs.append(description.value)
                if status or not apps.value:
                    raise KeyUnavailable()
                if self.cf.CFArrayGetCount(apps) != 1:
                    return False
                trusted = self.cf.CFArrayGetValueAtIndex(apps, 0)
                return bool(trusted) and hmac.compare_digest(data(trusted), expected)
            finally:
                for ref in reversed(refs):
                    self.cf.CFRelease(ref)
                if changed and self.sec.SecKeychainSetUserInteractionAllowed(previous.value):
                    raise KeyUnavailable()

    @contextmanager
    def _generation_lock(self, name: str) -> Iterator[None]:
        """Private interprocess lock; the generation itself lives only in Keychain."""
        directory = self._lock_root / ("relaydeck-keystore-" + str(os.getuid()))
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise KeyUnavailable()
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                label = hashlib.sha256((self.service + "\0" + name).encode()).hexdigest() + ".lock"
                fd = os.open(label, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            finally:
                os.close(parent)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise KeyUnavailable()
                deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise KeyUnavailable() from None
                        time.sleep(0.01)
                yield
            finally:
                os.close(fd)
        except OSError:
            raise KeyUnavailable() from None

    def _update(self, name: str, chain: ct.c_void_p, value: bytes) -> None:
        data = self.cf.CFDataCreate(None, value, len(value))
        update = None
        try:
            if not data:
                raise KeyUnavailable()
            keys = (ct.c_void_p * 1)(self._constant("kSecValueData"))
            values = (ct.c_void_p * 1)(data)
            update = self.cf.CFDictionaryCreate(
                None, keys, values, 1, ct.byref(self.key_callbacks), ct.byref(self.value_callbacks)
            )
            if not update:
                raise KeyUnavailable()
            with self._query(name, chain, allow_prompt=False) as query:
                if self.sec.SecItemUpdate(query, update):
                    raise KeyUnavailable()
            # Do not roll back an uncertain/superseded advance to an old generation.
            if self._read(name, chain, False) != value:
                raise KeyUnavailable()
        finally:
            if update:
                self.cf.CFRelease(update)
            if data:
                self.cf.CFRelease(data)

    @contextmanager
    def _query(
        self,
        name: str,
        keychain: ct.c_void_p,
        *,
        allow_prompt: bool,
        adding: bytes | None = None,
        reading: bool = False,
        reference: bool = False,
    ) -> Iterator[int]:
        refs: list[int] = []

        def keep(ref: int | None) -> int:
            if not ref:
                raise KeyUnavailable()
            refs.append(ref)
            return ref

        try:
            values = {
                "kSecClass": self._constant("kSecClassGenericPassword"),
                "kSecAttrService": keep(
                    self.cf.CFStringCreateWithCString(None, self.service.encode(), 0x08000100)
                ),
                "kSecAttrAccount": keep(
                    self.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
                ),
                "kSecUseAuthenticationUI": self._constant(
                    "kSecUseAuthenticationUIAllow"
                    if allow_prompt
                    else "kSecUseAuthenticationUIFail"
                ),
            }
            if adding is not None:
                values["kSecValueData"] = keep(self.cf.CFDataCreate(None, adding, len(adding)))
                if keychain.value is None:
                    raise KeyUnavailable()
                values["kSecUseKeychain"] = keychain.value
                values["kSecAttrAccess"] = self._access(keep)
            else:
                array = (ct.c_void_p * 1)(keychain.value)
                values["kSecMatchSearchList"] = keep(
                    self.cf.CFArrayCreate(None, array, 1, ct.byref(self.array_callbacks))
                )
            if reading or reference:
                values["kSecReturnRef" if reference else "kSecReturnData"] = self._constant(
                    "kCFBooleanTrue"
                )
                values["kSecMatchLimit"] = self._constant("kSecMatchLimitOne")
            keys = (ct.c_void_p * len(values))(*(self._constant(k) for k in values))
            vals = (ct.c_void_p * len(values))(*values.values())
            query = keep(
                self.cf.CFDictionaryCreate(
                    None,
                    keys,
                    vals,
                    len(values),
                    ct.byref(self.key_callbacks),
                    ct.byref(self.value_callbacks),
                )
            )
            yield query
        finally:
            for ref in reversed(refs):
                self.cf.CFRelease(ref)

    def _read(self, name: str, chain: ct.c_void_p, allow_prompt: bool) -> bytes | None:
        result = ct.c_void_p()
        try:
            with self._query(name, chain, allow_prompt=allow_prompt, reading=True) as query:
                status = self.sec.SecItemCopyMatching(query, ct.byref(result))
            if status == _NOT_FOUND:
                return None
            if (
                status
                or not result.value
                or self.cf.CFGetTypeID(result) != self.cf.CFDataGetTypeID()
            ):
                raise KeyUnavailable()
            size = self.cf.CFDataGetLength(result)
            if not 0 <= size <= MAX_SECRET_BYTES:
                raise KeyUnavailable()
            return ct.string_at(self.cf.CFDataGetBytePtr(result), size)
        finally:
            if result.value:
                self.cf.CFRelease(result)

    def perform(
        self,
        name: str,
        *,
        create: bool,
        allow_prompt: bool,
        delete: bool = False,
        importing: bytes | None = None,
        initial: bytes | None = None,
        advance: bool = False,
    ) -> bytes:
        # Check the default Keychain each time, even when the caller holds an AEAD key.
        with _KEYCHAIN_LOCK, self._generation_lock(name) if advance else nullcontext():
            chain, state, previous = ct.c_void_p(), ct.c_uint32(), ct.c_ubyte()
            changed = False
            try:
                if self.sec.SecKeychainGetUserInteractionAllowed(ct.byref(previous)):
                    raise KeyUnavailable()
                if not allow_prompt:
                    if self.sec.SecKeychainSetUserInteractionAllowed(False):
                        raise KeyUnavailable()
                    changed = True
                elif not previous.value:
                    if self.sec.SecKeychainSetUserInteractionAllowed(True):
                        raise KeyUnavailable()
                    changed = True
                if self.sec.SecKeychainCopyDefault(ct.byref(chain)) or not chain.value:
                    raise KeyUnavailable()
                if self.sec.SecKeychainGetStatus(chain, ct.byref(state)):
                    raise KeyUnavailable()
                if not allow_prompt and not state.value & 1:
                    raise KeyUnavailable()
                if delete:
                    with self._query(name, chain, allow_prompt=allow_prompt) as query:
                        status = self.sec.SecItemDelete(query)
                    if status not in (0, _NOT_FOUND):
                        raise KeyUnavailable()
                    return b""
                existing = self._read(name, chain, allow_prompt)
                if existing is not None:
                    if importing is not None and not hmac.compare_digest(existing, importing):
                        raise KeyUnavailable()
                    if advance:
                        number = _generation(existing)
                        if number >= 2**64 - 1:
                            raise KeyUnavailable()
                        newer = _GENERATION_PREFIX + (number + 1).to_bytes(8, "big")
                        self._update(name, chain, newer)
                        return newer
                    return existing
                if advance or (not create and importing is None):
                    raise KeyUnavailable()
                key = (
                    importing
                    if importing is not None
                    else initial
                    if initial is not None
                    else secrets.token_bytes(32)
                )
                with self._query(name, chain, allow_prompt=allow_prompt, adding=key) as query:
                    status = self.sec.SecItemAdd(query, None)
                if status == _DUPLICATE:
                    winner = self._read(name, chain, allow_prompt)
                    if winner is not None:
                        if importing is not None and not hmac.compare_digest(winner, importing):
                            raise KeyUnavailable()
                        return winner
                if status:
                    raise KeyUnavailable()
                return key
            finally:
                if chain.value:
                    self.cf.CFRelease(chain)
                if changed and self.sec.SecKeychainSetUserInteractionAllowed(previous.value):
                    raise KeyUnavailable()


class MacKeyProvider:
    """Only explicit setup may pass allow_prompt=True; ordinary reads never prompt."""

    def __init__(self, *, service: str = "dev.relaydeck.persistence") -> None:
        if sys.platform != "darwin" or not service or len(service) > 256 or "\0" in service:
            raise KeyUnavailable()
        try:
            self._backend = _Keychain(service)
        except (OSError, ValueError, AttributeError):
            raise KeyUnavailable() from None

    def get(self, name: str, *, create: bool = False, allow_prompt: bool = False) -> bytes:
        if (
            not name
            or len(name) > 512
            or "\0" in name
            or name.startswith(("material/", "restore/"))
        ):
            raise KeyUnavailable()
        try:
            key = self._backend.perform(name, create=create, allow_prompt=allow_prompt)
            if len(key) != 32:
                raise KeyUnavailable()
            return key
        except (OSError, ValueError, AttributeError, ct.ArgumentError):
            raise KeyUnavailable() from None

    def delete(self, name: str, *, allow_prompt: bool = False) -> None:
        """Explicit maintenance/test cleanup of this provider's item only."""
        if (
            not name
            or len(name) > 512
            or "\0" in name
            or name.startswith(("material/", "restore/"))
        ):
            raise KeyUnavailable()
        try:
            self._backend.perform(name, create=False, allow_prompt=allow_prompt, delete=True)
        except (OSError, ValueError, AttributeError, ct.ArgumentError):
            raise KeyUnavailable() from None

    def retire(self, name: str) -> None:
        if name.startswith(("material/", "restore/")):
            raise KeyUnavailable()
        self.delete(name)

    def _material(
        self,
        prefix: str,
        name: str,
        *,
        create: bool = False,
        allow_prompt: bool = False,
        importing: bytes | None = None,
        initial: bytes | None = None,
        advance: bool = False,
    ) -> bytes:
        if not name or len(name) > 480 or "\0" in name:
            raise KeyUnavailable()
        try:
            return self._backend.perform(
                prefix + name,
                create=create,
                allow_prompt=allow_prompt,
                importing=importing,
                initial=initial,
                advance=advance,
            )
        except (OSError, ValueError, AttributeError, ct.ArgumentError):
            raise KeyUnavailable() from None

    def import_secret(
        self, name: str, material: SecretMaterial, *, allow_prompt: bool = False
    ) -> SecretMaterial:
        if not isinstance(material, SecretMaterial) or not isinstance(material.value, bytes):
            raise KeyUnavailable()
        if len(material.value) > MAX_SECRET_BYTES:
            raise KeyUnavailable()
        return SecretMaterial(
            self._material("material/", name, importing=material.value, allow_prompt=allow_prompt)
        )

    def read_secret(self, name: str) -> SecretMaterial:
        return SecretMaterial(self._material("material/", name))

    def restore_generation(
        self, name: str, *, create: bool = False, allow_prompt: bool = False
    ) -> RestoreGeneration:
        raw = self._material(
            "restore/",
            name,
            create=create,
            allow_prompt=allow_prompt,
            initial=_GENERATION_PREFIX + bytes(8),
        )
        return RestoreGeneration(_generation(raw))

    def advance_restore_generation(self, name: str) -> RestoreGeneration:
        return RestoreGeneration(_generation(self._material("restore/", name, advance=True)))
