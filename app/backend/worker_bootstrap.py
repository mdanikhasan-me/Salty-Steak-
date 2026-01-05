"""Side-effect-free identity probe for private worker processes.

This module intentionally imports only the Python standard library.  It must
remain safe to import before torch, CUDA, the database, model code, or the
desktop host have been initialised.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def worker_entry_path(root: Path | None = None) -> Path:
    base = (root or package_root()).resolve()
    return base / "app" / "backend" / "versions" / "checkpoint.py"


def package_build_id(root: Path | None = None) -> str:
    configured = os.environ.get("SALTY_POTATO_BUILD_ID")
    if configured:
        return configured
    manifest = (root or package_root()) / "package.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "development"
    return str(payload.get("build_id") or payload.get("version") or "development")


def environment_identity() -> dict[str, Any]:
    root = package_root().resolve()
    entry = worker_entry_path(root).resolve()
    if not entry.is_file():
        raise FileNotFoundError(f"verification worker entry is missing: {entry}")
    executable = Path(sys.executable).resolve()
    prefix = Path(sys.prefix).resolve()
    fingerprint = hashlib.sha256()
    for value in (
        str(executable),
        str(prefix),
        str(root),
        sys.version,
        os.environ.get("VIRTUAL_ENV", ""),
    ):
        fingerprint.update(value.encode("utf-8", errors="strict"))
        fingerprint.update(b"\0")
    configuration = prefix / "pyvenv.cfg"
    if configuration.is_file():
        fingerprint.update(configuration.read_bytes())
    return {
        "success": True,
        "executable": str(executable),
        "prefix": str(prefix),
        "package_root": str(root),
        "package_build_id": package_build_id(root),
        "worker_entry": str(entry),
        "worker_sha256": _sha256(entry),
        "environment_fingerprint": fingerprint.hexdigest(),
        "worker_pid": os.getpid(),
    }


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-package-root")
    parser.add_argument("--expected-build-id")
    arguments = parser.parse_args()
    try:
        identity = environment_identity()
        if arguments.expected_package_root:
            expected = str(Path(arguments.expected_package_root).resolve())
            if identity["package_root"] != expected:
                raise RuntimeError(
                    f"package root mismatch: expected {expected}, "
                    f"got {identity['package_root']}"
                )
        if (
            arguments.expected_build_id
            and identity["package_build_id"] != arguments.expected_build_id
        ):
            raise RuntimeError("package build identity mismatch")
        print(json.dumps(identity, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                    "worker_pid": os.getpid(),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
