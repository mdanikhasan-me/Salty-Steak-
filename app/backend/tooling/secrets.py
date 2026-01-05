"""Small Windows DPAPI-backed secret store for connector credentials.

SQLite stores only an opaque reference.  Secret bytes are protected for the
current Windows user before they are written to the workspace control folder.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Protocol


class SecretStore(Protocol):
    def set(self, reference: str, value: str) -> None: ...

    def get(self, reference: str) -> str | None: ...

    def delete(self, reference: str) -> None: ...


class SecretStoreUnavailable(RuntimeError):
    """Raised when the operating-system secret protector is unavailable."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_ulong),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class DpapiSecretStore:
    """Persist current-user DPAPI ciphertext, never plaintext credentials."""

    _UI_FORBIDDEN = 0x1

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def set(self, reference: str, value: str) -> None:
        checked = _checked_reference(reference)
        plaintext = str(value).encode("utf-8")
        if not plaintext:
            raise ValueError("Connector credential cannot be empty")
        ciphertext = self._protect(plaintext)
        path = self._path(checked)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def get(self, reference: str) -> str | None:
        path = self._path(_checked_reference(reference))
        if not path.is_file():
            return None
        try:
            return self._unprotect(path.read_bytes()).decode("utf-8")
        except UnicodeDecodeError as error:
            raise SecretStoreUnavailable(
                "The protected connector credential is not valid UTF-8"
            ) from error

    def delete(self, reference: str) -> None:
        self._path(_checked_reference(reference)).unlink(missing_ok=True)

    def _path(self, reference: str) -> Path:
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.dpapi"

    @classmethod
    def _protect(cls, value: bytes) -> bytes:
        crypt32, kernel32 = _windows_crypto()
        source, source_buffer = _blob(value)
        output = _DataBlob()
        if not crypt32.CryptProtectData(
            ctypes.byref(source),
            "Salty Steak connector credential",
            None,
            None,
            None,
            cls._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise SecretStoreUnavailable(
                f"Windows DPAPI protection failed ({ctypes.get_last_error()})"
            )
        del source_buffer
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            kernel32.LocalFree(output.data)

    @classmethod
    def _unprotect(cls, value: bytes) -> bytes:
        crypt32, kernel32 = _windows_crypto()
        source, source_buffer = _blob(value)
        output = _DataBlob()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            cls._UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise SecretStoreUnavailable(
                f"Windows DPAPI decryption failed ({ctypes.get_last_error()})"
            )
        del source_buffer
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            kernel32.LocalFree(output.data)


def _checked_reference(value: str) -> str:
    checked = str(value).strip()
    if not checked or len(checked) > 200 or "\x00" in checked:
        raise ValueError("Invalid connector credential reference")
    return checked


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return _DataBlob(len(value), pointer), buffer


def _windows_crypto():
    if os.name != "nt":
        raise SecretStoreUnavailable(
            "Connector credential storage requires Windows DPAPI"
        )
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    crypt32.CryptProtectData.restype = ctypes.c_bool
    crypt32.CryptUnprotectData.restype = ctypes.c_bool
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32
