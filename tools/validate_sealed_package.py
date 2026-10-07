"""Independent audit for a staged Salty Steak desktop package.

The build script creates the package and its manifest.  This checker deliberately
does not trust either: it walks the filesystem, recalculates every listed hash,
checks for extras/bytecode, and exercises the package-local Python environment
from a working directory outside the repository.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Any


# This validator runs against a sealed package from outside the repository, so
# it cannot import the application. The expected schema is declared here and
# must be raised alongside ``Database.SCHEMA_VERSION``.
TARGET_SCHEMA_VERSION = 17
LEGACY_SCHEMA_VERSIONS = frozenset({7, 8, 9, 10, 11, 12, 13, 14, 15, 16})


OBSOLETE_FRONTEND_MARKERS = (
    "index-BHlel_4Z.js",
    "index-DrEP3W5H.css",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalise_manifest_path(value: str) -> str:
    path = value.replace("\\", "/")
    if not path or path.startswith("/") or ":" in path:
        raise ValueError(f"manifest path is not relative: {value!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"manifest path contains unsafe components: {value!r}")
    return "/".join(parts)


def physical_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != "integrity-manifest.json"
    ]


def reseal_source_provenance(root: Path, source_backup_sha256: str) -> dict[str, Any]:
    """Update source provenance and regenerate the complete integrity manifest."""

    root = root.resolve()
    package_path = root / "package.json"
    manifest_path = root / "integrity-manifest.json"
    package = json.loads(package_path.read_text(encoding="utf-8-sig"))
    previous_source_backup = package.get("source_backup_sha256")
    previous_package_hash = sha256(package_path)
    previous_manifest_hash = sha256(manifest_path)
    package["source_backup_sha256"] = source_backup_sha256.lower()
    package_path.write_text(
        json.dumps(package, indent=2) + "\n",
        encoding="utf-8",
    )

    entries = []
    for path in sorted(
        physical_files(root),
        key=lambda item: item.relative_to(root).as_posix().casefold(),
    ):
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    manifest = {
        "product": package.get("product"),
        "build_id": package.get("build_id"),
        "algorithm": "SHA-256",
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "files": entries,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "previous_source_backup_sha256": previous_source_backup,
        "source_backup_sha256": source_backup_sha256.lower(),
        "previous_package_json_sha256": previous_package_hash,
        "package_json_sha256": sha256(package_path),
        "previous_manifest_sha256": previous_manifest_hash,
        "manifest_sha256": sha256(manifest_path),
        "file_count": len(entries),
        "declared_total_bytes": sum(entry["bytes"] for entry in entries),
    }


def run_private_python(root: Path, code: str, *, cwd: Path) -> dict[str, Any]:
    private_home = root / ".python"
    packaged_runtime = (private_home / "python.exe").is_file()
    executable = (
        private_home / "python.exe"
        if packaged_runtime
        else root / ".venv" / "Scripts" / "python.exe"
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    if packaged_runtime:
        environment["PYTHONHOME"] = str(private_home)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(root), str(root / ".venv" / "Lib" / "site-packages")]
        )
        environment["VIRTUAL_ENV"] = str(root / ".venv")
        environment["PYTHONNOUSERSITE"] = "1"
        isolation_flags = ["-s", "-B"]
    else:
        # Source-only migration unit tests have a normal venv and no packaged
        # base interpreter. Release audits cannot take this branch because a
        # missing `.python` is already a hard package issue below.
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        isolation_flags = ["-I", "-B"]
    completed = subprocess.run(
        [str(executable), *isolation_flags, "-c", code],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return {
        "executable": str(executable),
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_tables(connection: sqlite3.Connection) -> list[str]:
    return sorted(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )


def _database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
        )
        for table in _database_tables(connection)
    }


def _normalise_database_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _protected_record_snapshot(
    connection: sqlite3.Connection,
    columns_by_table: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fingerprint logical records while allowing only the schema-version bump.

    For the first v7 -> current comparison, ``columns_by_table`` contains the v7
    columns.  Querying those same columns after migration proves every legacy
    field and row survived even when later schemas add columns. The one intentionally
    mutable record is ``application_metadata.schema_version``.
    """

    if columns_by_table is None:
        columns_by_table = {
            table: [
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({_quote_identifier(table)})"
                )
            ]
            for table in _database_tables(connection)
        }
    snapshot: dict[str, dict[str, Any]] = {}
    for table, columns in columns_by_table.items():
        if table not in _database_tables(connection):
            snapshot[table] = {
                "columns": columns,
                "record_count": -1,
                "records_sha256": "missing",
            }
            continue
        selected_columns = ", ".join(_quote_identifier(item) for item in columns)
        query = f"SELECT {selected_columns} FROM {_quote_identifier(table)}"
        parameters: tuple[str, ...] = ()
        if table == "application_metadata" and "key" in columns:
            query += " WHERE key <> ?"
            parameters = ("schema_version",)
        records = [
            [_normalise_database_value(value) for value in row]
            for row in connection.execute(query, parameters).fetchall()
        ]
        records.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        encoded = json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot[table] = {
            "columns": columns,
            "record_count": len(records),
            "records_sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return snapshot


def _database_health(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    return {
        "integrity_check": integrity,
        "integrity_ok": integrity == ["ok"],
        "foreign_key_violations": foreign_keys,
        "foreign_keys_clean": not foreign_keys,
    }


def audit_disposable_v7_migration(
    root: Path,
    migration_db: Path,
    *,
    neutral_cwd: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Exercise package-local supported migration and current idempotence.

    Both v7 (pre-automation) and v8 (the current production database before
    generated-image artifacts) are real release inputs. A current-schema copy
    is intentionally rejected because it cannot prove migration safety.
    """

    copied_db = neutral_cwd / "migration" / "salty-potato.db"
    copied_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(migration_db, copied_db)
    issues: list[str] = []

    before = sqlite3.connect(copied_db)
    before.row_factory = sqlite3.Row
    before_version = int(before.execute("PRAGMA user_version").fetchone()[0])
    before_counts = _database_counts(before)
    legacy_columns = {
        table: [
            str(row[1])
            for row in before.execute(
                f"PRAGMA table_info({_quote_identifier(table)})"
            )
        ]
        for table in _database_tables(before)
    }
    before_records = _protected_record_snapshot(before, legacy_columns)
    before_health = _database_health(before)
    before.close()

    first_open = run_private_python(
        root,
        (
            f"import sys; sys.path.insert(0, r'{root}'); "
            "from app.backend.database.control import Database; "
            f"Database(r'{copied_db}')"
        ),
        cwd=neutral_cwd,
    )
    if first_open.get("returncode") != 0:
        issues.append("disposable legacy database migration failed")

    after = sqlite3.connect(copied_db)
    after.row_factory = sqlite3.Row
    after_version = int(after.execute("PRAGMA user_version").fetchone()[0])
    after_counts = _database_counts(after)
    after_records = _protected_record_snapshot(after, legacy_columns)
    after_health = _database_health(after)
    schema_metadata = after.execute(
        "SELECT value FROM application_metadata WHERE key = 'schema_version'"
    ).fetchone()
    schema_metadata_value = str(schema_metadata[0]) if schema_metadata else None
    after.close()

    changed_legacy_counts = {
        name: [before_counts[name], after_counts.get(name)]
        for name in before_counts
        if before_counts[name] != after_counts.get(name)
    }
    changed_legacy_records = {
        name: {
            "before": before_records[name],
            "after": after_records.get(name),
        }
        for name in before_records
        if before_records[name] != after_records.get(name)
    }
    added_rows = {
        name: count
        for name, count in after_counts.items()
        if name not in before_counts and count
    }

    before_idempotent = sqlite3.connect(copied_db)
    before_idempotent.row_factory = sqlite3.Row
    idempotent_columns = {
        table: [
            str(row[1])
            for row in before_idempotent.execute(
                f"PRAGMA table_info({_quote_identifier(table)})"
            )
        ]
        for table in _database_tables(before_idempotent)
    }
    idempotent_before_counts = _database_counts(before_idempotent)
    idempotent_before_records = _protected_record_snapshot(
        before_idempotent, idempotent_columns
    )
    before_idempotent.close()

    second_open = run_private_python(
        root,
        (
            f"import sys; sys.path.insert(0, r'{root}'); "
            "from app.backend.database.control import Database; "
            f"Database(r'{copied_db}')"
        ),
        cwd=neutral_cwd,
    )
    if second_open.get("returncode") != 0:
        issues.append("disposable current-schema idempotence open failed")

    idempotent = sqlite3.connect(copied_db)
    idempotent.row_factory = sqlite3.Row
    idempotent_version = int(idempotent.execute("PRAGMA user_version").fetchone()[0])
    idempotent_after_counts = _database_counts(idempotent)
    idempotent_after_records = _protected_record_snapshot(
        idempotent, idempotent_columns
    )
    idempotent_health = _database_health(idempotent)
    idempotent.close()

    idempotent_count_changes = {
        name: [idempotent_before_counts.get(name), idempotent_after_counts.get(name)]
        for name in sorted(
            set(idempotent_before_counts) | set(idempotent_after_counts)
        )
        if idempotent_before_counts.get(name) != idempotent_after_counts.get(name)
    }
    idempotent_record_changes = {
        name: {
            "before": idempotent_before_records.get(name),
            "after": idempotent_after_records.get(name),
        }
        for name in sorted(
            set(idempotent_before_records) | set(idempotent_after_records)
        )
        if idempotent_before_records.get(name) != idempotent_after_records.get(name)
    }

    if before_version not in LEGACY_SCHEMA_VERSIONS or after_version != TARGET_SCHEMA_VERSION:
        expected = "-or-".join(str(value) for value in sorted(LEGACY_SCHEMA_VERSIONS))
        issues.append(
            f"unexpected migration versions: {before_version}->{after_version}; "
            f"required {expected}->{TARGET_SCHEMA_VERSION}"
        )
    if idempotent_version != TARGET_SCHEMA_VERSION:
        issues.append(
            f"unexpected idempotence schema version: {idempotent_version}; "
            f"required {TARGET_SCHEMA_VERSION}"
        )
    if schema_metadata_value != str(TARGET_SCHEMA_VERSION):
        issues.append(
            f"database schema metadata was not updated to {TARGET_SCHEMA_VERSION}"
        )
    if not before_health["integrity_ok"] or not before_health["foreign_keys_clean"]:
        issues.append("input legacy database was not healthy")
    if not after_health["integrity_ok"] or not after_health["foreign_keys_clean"]:
        issues.append("legacy to current-schema migration produced an unhealthy database")
    if (
        not idempotent_health["integrity_ok"]
        or not idempotent_health["foreign_keys_clean"]
    ):
        issues.append("current-schema idempotence open produced an unhealthy database")
    if changed_legacy_counts or changed_legacy_records or added_rows:
        issues.append("database migration changed protected records")
    if idempotent_count_changes or idempotent_record_changes:
        issues.append("current-schema idempotence open changed records")

    result = {
        "skipped": False,
        "source": str(migration_db.resolve()),
        "copy": str(copied_db),
        "schema_contract": {
            "required_input": sorted(LEGACY_SCHEMA_VERSIONS),
            "required_output": TARGET_SCHEMA_VERSION,
            "idempotent_version": TARGET_SCHEMA_VERSION,
        },
        "before_version": before_version,
        "after_version": after_version,
        "idempotent_version": idempotent_version,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "changed_legacy_counts": changed_legacy_counts,
        "changed_legacy_records": changed_legacy_records,
        "added_rows": added_rows,
        "first_open": first_open,
        "second_open": second_open,
        "before_health": before_health,
        "after_health": after_health,
        "idempotent_health": idempotent_health,
        "schema_metadata_value": schema_metadata_value,
        "idempotent_count_changes": idempotent_count_changes,
        "idempotent_record_changes": idempotent_record_changes,
        "protected_records_preserved": not changed_legacy_records,
        "idempotent_records_preserved": not idempotent_record_changes,
        "valid": not issues,
    }
    return result, issues


def audit_package(
    root: Path,
    *,
    expected_build_id: str | None,
    expected_fingerprint: str | None,
    expected_source_backup: str | None,
    expected_source_tree: str | None,
    source_manifest: Path | None,
    migration_db: Path | None,
) -> dict[str, Any]:
    started = time.perf_counter()
    root = root.resolve()
    issues: list[str] = []
    manifest_path = root / "integrity-manifest.json"
    package_path = root / "package.json"

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # pragma: no cover - defensive audit path
        return {"valid": False, "issues": [f"manifest unreadable: {exc}"]}
    try:
        package = json.loads(package_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # pragma: no cover - defensive audit path
        return {"valid": False, "issues": [f"package metadata unreadable: {exc}"]}

    listed: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        try:
            relative = normalise_manifest_path(str(entry["path"]))
        except (KeyError, ValueError) as exc:
            issues.append(str(exc))
            continue
        key = relative.casefold()
        if key in {item.casefold() for item in listed}:
            issues.append(f"duplicate manifest path: {relative}")
        listed[relative] = entry

    physical = physical_files(root)
    physical_map: dict[str, Path] = {}
    for path in physical:
        relative = path.relative_to(root).as_posix()
        key = relative.casefold()
        if key in physical_map:
            issues.append(f"case-insensitive duplicate file: {relative}")
        physical_map[key] = path

    listed_keys = {path.casefold() for path in listed}
    physical_keys = set(physical_map)
    extras = sorted(
        path.relative_to(root).as_posix()
        for key, path in physical_map.items()
        if key not in listed_keys
    )
    missing = sorted(path for path in listed if path.casefold() not in physical_keys)
    if extras:
        issues.append(f"unmanifested files: {len(extras)}")
    if missing:
        issues.append(f"missing manifest files: {len(missing)}")

    checked_bytes = 0
    hash_mismatches: list[str] = []
    for relative, entry in listed.items():
        path = root / Path(relative)
        if not path.is_file():
            continue
        actual_bytes = path.stat().st_size
        actual_hash = sha256(path)
        checked_bytes += actual_bytes
        if int(entry.get("bytes", -1)) != actual_bytes:
            hash_mismatches.append(f"{relative}: byte count")
        if str(entry.get("sha256", "")).lower() != actual_hash:
            hash_mismatches.append(f"{relative}: sha256")
    if hash_mismatches:
        issues.append(f"manifest content mismatches: {len(hash_mismatches)}")

    forbidden = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if "__pycache__" in path.parts
        or any(
            part in {".pytest_cache", ".mypy_cache", ".ruff_cache"}
            for part in path.parts
        )
        or (
            path.is_file()
            and path.suffix.lower() in {".pyc", ".pyo", ".tmp", ".log"}
        )
    )
    if forbidden:
        issues.append(f"forbidden Python cache entries: {len(forbidden)}")

    required_metadata = {
        "product": "Salty Steak",
        "build_id": expected_build_id,
        "native_host_version": expected_build_id,
        "frontend_build_id": expected_build_id,
        "source_dependency": False,
        "repository_isolation": True,
        "workspace_isolation": True,
    }
    metadata_checks: dict[str, bool] = {}
    for key, expected in required_metadata.items():
        if expected is None:
            continue
        actual = package.get(key)
        metadata_checks[key] = actual == expected
        if actual != expected:
            issues.append(f"package metadata {key!r}={actual!r}, expected {expected!r}")
    if expected_fingerprint is not None:
        metadata_checks["protected_artifact_fingerprint"] = (
            str(package.get("protected_artifact_fingerprint", "")).lower()
            == expected_fingerprint.lower()
        )
        if not metadata_checks["protected_artifact_fingerprint"]:
            issues.append("protected-artifact fingerprint mismatch")
    if expected_source_backup is not None:
        metadata_checks["source_backup_sha256"] = (
            str(package.get("source_backup_sha256", "")).lower()
            == expected_source_backup.lower()
        )
        if not metadata_checks["source_backup_sha256"]:
            issues.append("source-backup hash mismatch")
    if expected_source_tree is not None:
        metadata_checks["source_tree_sha256"] = (
            str(package.get("source_tree_sha256", "")).lower()
            == expected_source_tree.lower()
        )
        if not metadata_checks["source_tree_sha256"]:
            issues.append("source-tree hash mismatch")

    source_provenance: dict[str, Any] = {"skipped": source_manifest is None}
    if source_manifest is not None:
        source_manifest = source_manifest.resolve()
        source_payload = json.loads(
            source_manifest.read_text(encoding="utf-8")
        )
        source_manifest_hash = sha256(source_manifest)
        source_tree_hash = str(
            source_payload.get("source_tree_sha256", "")
        ).lower()
        source_provenance = {
            "skipped": False,
            "manifest_path": str(source_manifest),
            "manifest_sha256": source_manifest_hash,
            "source_tree_sha256": source_tree_hash,
            "declared_file_count": source_payload.get("file_count"),
            "packaged_source_files_checked": 0,
            "packaged_source_mismatches": [],
        }
        if (
            str(package.get("source_manifest_sha256", "")).lower()
            != source_manifest_hash
        ):
            issues.append("package source-manifest hash mismatch")
        if (
            str(package.get("source_tree_sha256", "")).lower()
            != source_tree_hash
        ):
            issues.append("package source-tree hash differs from source manifest")
        packaged_prefixes = (
            "app/backend/",
            "app/frontend/public/",
            "config/",
        )
        for entry in source_payload.get("files", []):
            relative = str(entry.get("path", ""))
            if relative == "app/__init__.py" or relative.startswith(
                packaged_prefixes
            ):
                packaged = root / Path(relative)
                source_provenance["packaged_source_files_checked"] += 1
                if (
                    not packaged.is_file()
                    or packaged.stat().st_size != int(entry.get("bytes", -1))
                    or sha256(packaged) != str(entry.get("sha256", ""))
                ):
                    source_provenance[
                        "packaged_source_mismatches"
                    ].append(relative)
        if source_provenance["packaged_source_mismatches"]:
            issues.append(
                "packaged source differs from frozen source manifest: "
                f"{len(source_provenance['packaged_source_mismatches'])}"
            )

    performance = package.get("performance_profile", {})
    expected_performance = {
        "training_micro_batch": 16,
        "training_gradient_accumulation": 1,
        "training_effective_batch": 16,
        "training_sequence_length": 512,
        "training_precision": "bf16",
        "evaluation_batch_size": 32,
    }
    if performance != expected_performance:
        issues.append(f"performance profile mismatch: {performance!r}")

    venv_configuration = root / ".venv" / "pyvenv.cfg"
    venv_home = ""
    if not venv_configuration.is_file():
        issues.append("private venv configuration is missing")
    else:
        for line in venv_configuration.read_text(
            encoding="utf-8-sig"
        ).splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip().casefold() == "home":
                venv_home = value.strip().replace("/", "\\")
                break
        if venv_home != ".python":
            issues.append(
                "private venv home is not package-relative: "
                f"{venv_home or 'missing'}"
            )

    private_python = root / ".python" / "python.exe"
    private_python_result = {
        "exists": private_python.is_file(),
        "version": "",
        "sha256": sha256(private_python) if private_python.is_file() else None,
    }
    if not private_python_result["exists"]:
        issues.append("private Python executable is missing")
    else:
        version = subprocess.run(
            [str(private_python), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        private_python_result["version"] = (
            (version.stdout or version.stderr).strip()
        )
        if version.returncode != 0:
            issues.append("private Python did not report its identity")
        expected_hash = str(package.get("private_python", {}).get("executable_sha256", "")).lower()
        if private_python_result["sha256"] != expected_hash:
            issues.append("private Python hash does not match package metadata")

    with tempfile.TemporaryDirectory(prefix="salty-r4-package-audit-") as temporary:
        neutral_cwd = Path(temporary)
        import_result = run_private_python(
            root,
            (
                f"import sys; sys.path.insert(0, r'{root}'); "
                "import app, os; "
                "from app.backend.database.control import Database; "
                "print({'app': app.__file__, 'cwd': os.getcwd(), "
                "'python': sys.executable, 'pythonpath_env': "
                "os.environ.get('PYTHONPATH'), 'pythonhome_env': "
                "os.environ.get('PYTHONHOME')})"
            ),
            cwd=neutral_cwd,
        )
        if import_result["returncode"] != 0:
            issues.append("package-local Python import failed")
        else:
            app_path = str(root / "app").replace("\\", "/").casefold()
            import_stdout = import_result["stdout"].replace("\\", "/").casefold()
            while "//" in import_stdout:
                import_stdout = import_stdout.replace("//", "/")
            if app_path not in import_stdout:
                issues.append("package import did not resolve to package-local app")

        migration_result: dict[str, Any] = {"skipped": migration_db is None}
        if migration_db is not None:
            migration_result, migration_issues = audit_disposable_v7_migration(
                root,
                migration_db,
                neutral_cwd=neutral_cwd,
            )
            issues.extend(migration_issues)

    frontend_root = root / "app" / "frontend" / "dist"
    frontend_files = [
        path for path in frontend_root.rglob("*") if path.is_file()
    ] if frontend_root.is_dir() else []
    frontend_marker_absence = {
        marker: not any(path.name == marker for path in frontend_files)
        for marker in OBSOLETE_FRONTEND_MARKERS
    }
    if not all(frontend_marker_absence.values()):
        issues.append("obsolete white-UI frontend asset marker present")
    frontend_build_id_present = False
    for path in frontend_files:
        try:
            if expected_build_id and expected_build_id.encode() in path.read_bytes():
                frontend_build_id_present = True
                break
        except OSError:
            continue
    if not frontend_build_id_present:
        issues.append("frontend build identity was not found in packaged assets")

    result = {
        "valid": not issues,
        "root": str(root),
        "build_id": package.get("build_id"),
        "manifest": {
            "declared_file_count": len(listed),
            "physical_file_count_excluding_manifest": len(physical),
            "declared_total_bytes": sum(int(item.get("bytes", 0)) for item in listed.values()),
            "checked_bytes": checked_bytes,
            "sha256": sha256(manifest_path),
        },
        "extras": extras,
        "missing": missing,
        "hash_mismatches": hash_mismatches,
        "forbidden_bytecode": forbidden,
        "metadata_checks": metadata_checks,
        "source_provenance": source_provenance,
        "private_python": private_python_result,
        "private_venv_home": venv_home,
        "import_isolation": import_result,
        "migration": migration_result,
        "frontend_marker_absence": frontend_marker_absence,
        "frontend_build_id_present": frontend_build_id_present,
        "issues": issues,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--expected-build-id")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--expected-source-backup")
    parser.add_argument("--expected-source-tree")
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--migration-db", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--reseal-source-backup",
        help="Set this source archive SHA-256 and regenerate the manifest before audit.",
    )
    args = parser.parse_args()
    reseal_result = None
    if args.reseal_source_backup:
        reseal_result = reseal_source_provenance(
            args.package,
            args.reseal_source_backup,
        )
    result = audit_package(
        args.package,
        expected_build_id=args.expected_build_id,
        expected_fingerprint=args.expected_fingerprint,
        expected_source_backup=args.expected_source_backup,
        expected_source_tree=args.expected_source_tree,
        source_manifest=args.source_manifest,
        migration_db=args.migration_db,
    )
    if reseal_result is not None:
        result["reseal"] = reseal_result
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
