"""Create or verify the content-addressed source input set for a release build."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Iterable


DIRECTORIES = (
    "app/backend",
    "app/frontend/src",
    "app/frontend/public",
    "app/desktop/browser",
    "app/desktop/native",
    "app/desktop/uia",
    "config",
    "tests",
)
FILES = (
    "app/__init__.py",
    "app/frontend/index.html",
    "app/frontend/vite.config.js",
    "package.json",
    "package-lock.json",
    "pytest.ini",
    "requirements-dev.txt",
    "requirements.txt",
    "tools/build_desktop.ps1",
    "tools/build_salty_vision_candidate.ps1",
    "tools/acceptance_discord_agent.py",
    "tools/acceptance_code_artifact.py",
    "tools/acceptance_interactive_latency.py",
    "tools/acceptance_image_generation.py",
    "tools/acceptance_packaged_runtime.py",
    "tools/acceptance_route_boundaries.py",
    "tools/capture_window.ps1",
    "tools/gpu_validation_guard.py",
    "tools/smoke_model_bundle_chat.py",
    "tools/smoke_saltnative_worker.py",
    "tools/smoke_steak_gen_image.py",
    "tools/snapshot_release_source.py",
    "tools/stage_steak20_candidate_workspace.ps1",
    "tools/validate_generation_matrix.py",
    "tools/validate_steak_gen_transformer.py",
    "tools/validate_salty_vision_candidate.py",
    "tools/validate_sealed_package.py",
    "tools/window_input.ps1",
)
IGNORED_PARTS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "bin",
        "obj",
    }
)
IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".tmp", ".log"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files(root: Path) -> list[Path]:
    paths: set[Path] = set()
    for relative in DIRECTORIES:
        directory = root / relative
        if not directory.is_dir():
            raise FileNotFoundError(f"release source directory is missing: {relative}")
        for path in directory.rglob("*"):
            if (
                path.is_file()
                and not IGNORED_PARTS.intersection(path.relative_to(root).parts)
                and path.suffix.casefold() not in IGNORED_SUFFIXES
                and path.name != "local.toml"
            ):
                paths.add(path.resolve())
    for relative in FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"release source file is missing: {relative}")
        paths.add(path.resolve())
    return sorted(
        paths,
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )


def entries_for(root: Path, paths: Iterable[Path]) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in paths
    ]


def tree_digest(entries: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(str(entry["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def snapshot(root: Path) -> dict[str, object]:
    root = root.resolve()
    entries = entries_for(root, source_files(root))
    return {
        "format": "salty-potato-release-source-v1",
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "root_name": root.name,
        "file_count": len(entries),
        "total_bytes": sum(int(entry["bytes"]) for entry in entries),
        "source_tree_sha256": tree_digest(entries),
        "files": entries,
    }


def verify(root: Path, manifest_path: Path) -> dict[str, object]:
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = snapshot(root)
    expected_files = {
        str(entry["path"]): entry for entry in expected.get("files", [])
    }
    actual_files = {
        str(entry["path"]): entry for entry in actual.get("files", [])
    }
    missing = sorted(set(expected_files) - set(actual_files))
    extras = sorted(set(actual_files) - set(expected_files))
    mismatches = sorted(
        path
        for path in set(expected_files) & set(actual_files)
        if expected_files[path].get("bytes") != actual_files[path].get("bytes")
        or expected_files[path].get("sha256") != actual_files[path].get("sha256")
    )
    valid = (
        expected.get("format") == "salty-potato-release-source-v1"
        and expected.get("source_tree_sha256") == actual["source_tree_sha256"]
        and not missing
        and not extras
        and not mismatches
    )
    return {
        "valid": valid,
        "source_tree_sha256": actual["source_tree_sha256"],
        "file_count": actual["file_count"],
        "total_bytes": actual["total_bytes"],
        "missing": missing,
        "extras": extras,
        "mismatches": mismatches,
    }


def write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if bool(args.output) == bool(args.verify):
        parser.error("choose exactly one of --output or --verify")
    if args.output:
        result = snapshot(args.root)
        write_atomic(args.output, result)
        valid = True
    else:
        result = verify(args.root, args.verify)
        valid = bool(result["valid"])
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
