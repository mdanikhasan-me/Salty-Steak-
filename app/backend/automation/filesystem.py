"""Exact, recoverable Windows file actions for confirmed host proposals."""

from __future__ import annotations

import ctypes
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class FileActionError(RuntimeError):
    """Raised when a confirmed file action cannot be completed safely."""


def windows_temp_roots() -> list[dict[str, Any]]:
    """Return the three explicit Windows temporary-data roots.

    The roots themselves are never deleted. Missing roots remain visible in
    the review contract instead of being silently replaced by a broader path.
    """

    local_temp = Path(os.environ.get("TEMP") or tempfile.gettempdir())
    windows_root = Path(os.environ.get("WINDIR") or r"C:\Windows")
    declared = (
        ("User Temp", local_temp),
        ("Windows Temp", windows_root / "Temp"),
        ("Prefetch", windows_root / "Prefetch"),
    )
    roots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, candidate in declared:
        absolute = candidate.expanduser().absolute()
        identity = os.path.normcase(str(absolute))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            exists = absolute.exists()
        except OSError as error:
            roots.append(
                {
                    "name": name,
                    "path": str(absolute),
                    "exists": None,
                    "accessible": False,
                    "reason": f"{type(error).__name__}: {error}"[:240],
                }
            )
            continue
        if exists:
            try:
                resolved = absolute.resolve(strict=True)
                if not resolved.is_dir() or _is_reparse(resolved):
                    raise FileActionError(
                        f"Temporary-data root is not a safe directory: {resolved}"
                    )
                absolute = resolved
            except OSError as error:
                roots.append(
                    {
                        "name": name,
                        "path": str(absolute),
                        "exists": True,
                        "accessible": False,
                        "reason": f"{type(error).__name__}: {error}"[:240],
                    }
                )
                continue
        roots.append(
            {
                "name": name,
                "path": str(absolute),
                "exists": exists,
                "accessible": True,
            }
        )
    return roots


def clean_temp_root_contents(
    roots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete only children of reviewed temp roots and skip locked entries."""

    deleted_files = 0
    deleted_directories = 0
    reclaimed_bytes = 0
    skipped = 0
    skipped_samples: list[dict[str, str]] = []

    def record_skip(path: Path, reason: str) -> None:
        nonlocal skipped
        skipped += 1
        if len(skipped_samples) < 25:
            skipped_samples.append({"path": str(path), "reason": reason[:240]})

    def remove_entry(path: Path) -> None:
        nonlocal deleted_files, deleted_directories, reclaimed_bytes
        try:
            info = path.lstat()
        except OSError as error:
            record_skip(path, f"{type(error).__name__}: {error}")
            return
        if stat.S_ISLNK(info.st_mode) or _is_reparse(path, info):
            record_skip(path, "Reparse points and symbolic links are not followed")
            return
        if stat.S_ISDIR(info.st_mode):
            try:
                children = list(path.iterdir())
            except OSError as error:
                record_skip(path, f"{type(error).__name__}: {error}")
                return
            for child in children:
                remove_entry(child)
            try:
                path.rmdir()
                deleted_directories += 1
            except OSError as error:
                record_skip(path, f"{type(error).__name__}: {error}")
            return
        size = int(info.st_size)
        try:
            path.unlink()
            deleted_files += 1
            reclaimed_bytes += size
        except OSError as error:
            record_skip(path, f"{type(error).__name__}: {error}")

    root_results: list[dict[str, Any]] = []
    for record in roots:
        name = str(record.get("name") or "Windows temporary data")
        raw_path = str(record.get("path") or "").strip()
        if not raw_path:
            raise ValueError("Temporary-data roots require exact absolute paths")
        root = Path(raw_path)
        if not root.is_absolute():
            raise ValueError("Temporary-data roots must be absolute")
        if record.get("accessible") is False:
            reason = str(record.get("reason") or "Windows denied access")[:240]
            record_skip(root, reason)
            root_results.append(
                {
                    "name": name,
                    "path": str(root),
                    "status": "inaccessible",
                    "deleted_files": 0,
                    "deleted_directories": 0,
                    "reclaimed_bytes": 0,
                    "skipped_entries": 1,
                    "reason": reason,
                }
            )
            continue
        if not root.exists():
            root_results.append({"name": name, "path": str(root), "status": "missing"})
            continue
        resolved = root.resolve(strict=True)
        if not resolved.is_dir() or _is_reparse(resolved):
            raise FileActionError(f"Temporary-data root changed or is unsafe: {resolved}")
        before_files = deleted_files
        before_directories = deleted_directories
        before_bytes = reclaimed_bytes
        before_skipped = skipped
        try:
            children = list(resolved.iterdir())
        except OSError as error:
            record_skip(resolved, f"{type(error).__name__}: {error}")
            children = []
        for child in children:
            remove_entry(child)
        root_results.append(
            {
                "name": name,
                "path": str(resolved),
                "status": "cleaned_with_skips" if skipped > before_skipped else "cleaned",
                "deleted_files": deleted_files - before_files,
                "deleted_directories": deleted_directories - before_directories,
                "reclaimed_bytes": reclaimed_bytes - before_bytes,
                "skipped_entries": skipped - before_skipped,
            }
        )
    return {
        "status": "completed_with_skips" if skipped else "completed",
        "deleted_files": deleted_files,
        "deleted_directories": deleted_directories,
        "reclaimed_bytes": reclaimed_bytes,
        "skipped_entries": skipped,
        "skipped_samples": skipped_samples,
        "roots": root_results,
        "root_directories_preserved": True,
        "recovery": "not_available_for_temporary_data_cleanup",
    }


def _is_reparse(path: Path, info: os.stat_result | None = None) -> bool:
    checked = info or path.lstat()
    attributes = int(getattr(checked, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def snapshot_regular_file(path: str | Path) -> dict[str, Any]:
    """Return stable evidence for one exact existing non-reparse file."""

    raw = str(path or "").strip()
    if not raw:
        raise ValueError("A file path is required")
    if "\x00" in raw or any(character in raw for character in "*?"):
        raise ValueError("File actions require one exact path without wildcards")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("File actions require an absolute path")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("The selected target is not a regular file")
    stat = resolved.stat()
    attributes = int(getattr(stat, "st_file_attributes", 0))
    if resolved.is_symlink() or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Reparse points and symbolic links cannot be removed by Chat")
    return {
        "path": str(resolved),
        "size_bytes": int(stat.st_size),
        "modified_ns": int(stat.st_mtime_ns),
    }


def recycle_confirmed_file(
    path: str | Path,
    *,
    expected_size_bytes: int,
    expected_modified_ns: int,
    recycler: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Revalidate and move one exact file to the Windows Recycle Bin."""

    before = snapshot_regular_file(path)
    if before["size_bytes"] != int(expected_size_bytes):
        raise FileActionError("The file size changed after review; confirmation is no longer valid")
    if before["modified_ns"] != int(expected_modified_ns):
        raise FileActionError("The file changed after review; confirmation is no longer valid")
    target = Path(before["path"])
    (recycler or _windows_recycle_file)(target)
    if target.exists():
        raise FileActionError("Windows reported success but the file still exists")
    return {
        **before,
        "status": "succeeded",
        "disposition": "windows_recycle_bin",
        "recoverable": True,
    }


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def _windows_recycle_file(path: Path) -> None:
    if os.name != "nt":
        raise FileActionError("The Windows Recycle Bin is unavailable on this platform")
    operation = _SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = f"{path}\0\0"
    operation.fFlags = (
        FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    )
    result = int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)))
    if result != 0:
        raise FileActionError(f"Windows Recycle Bin operation failed with code {result}")
    if operation.fAnyOperationsAborted:
        raise FileActionError("The Windows Recycle Bin operation was cancelled")
