"""Small, shared filesystem primitives for durable application state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import AppConfig, StoragePaths


HASH_CHUNK_BYTES = 4 * 1024 * 1024


def create_storage_layout(config: AppConfig | StoragePaths) -> StoragePaths:
    paths = config.paths if isinstance(config, AppConfig) else config
    for directory in dict.fromkeys(paths.directories()):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def sha256_file(path: str | Path, *, chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_fingerprint(path: str | Path, *, include_checksum: bool = True) -> dict[str, Any]:
    source = Path(path).resolve(strict=True)
    stat = source.stat()
    result: dict[str, Any] = {
        "path": str(source),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if include_checksum:
        result["checksum"] = sha256_file(source)
    return result


def ensure_within(path: str | Path, directory: str | Path) -> Path:
    """Resolve a path and reject traversal outside the named directory."""

    resolved = Path(path).resolve()
    parent = Path(directory).resolve()
    if resolved != parent and parent not in resolved.parents:
        raise ValueError(f"Path {resolved} is outside {parent}")
    return resolved


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: str | Path, payload: bytes) -> Path:
    destination = Path(path)
    _atomic_replace(destination, payload)
    return destination


def atomic_write_text(
    path: str | Path,
    value: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    destination = Path(path)
    _atomic_replace(destination, value.encode(encoding))
    return destination


def atomic_write_json(path: str | Path, value: Any) -> Path:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    )
    return atomic_write_text(path, payload + "\n")


def verify_files(
    directory: str | Path,
    expected: dict[str, str],
) -> tuple[bool, list[dict[str, str]]]:
    """Verify relative file paths against expected SHA-256 checksums."""

    root = Path(directory).resolve()
    problems: list[dict[str, str]] = []
    for relative_name, expected_checksum in expected.items():
        candidate = ensure_within(root / relative_name, root)
        if not candidate.is_file():
            problems.append({"file": relative_name, "error": "missing"})
            continue
        actual = sha256_file(candidate)
        if actual != expected_checksum:
            problems.append(
                {
                    "file": relative_name,
                    "error": "checksum_mismatch",
                    "expected": expected_checksum,
                    "actual": actual,
                }
            )
    return not problems, problems


def total_file_size(paths: Iterable[str | Path]) -> int:
    return sum(Path(path).stat().st_size for path in paths if Path(path).is_file())
