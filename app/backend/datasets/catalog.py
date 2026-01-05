"""Persist dataset registrations and reconcile source-file identity."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.backend.database.control import Database, json_text, new_id, parse_json, utc_now
from app.backend.system.files import file_fingerprint, sha256_file

from .errors import DatasetValidationError
from .mapping import normalize_mapping
from .prepared_cache import prepared_contract_summary
from .source_inspection import inspect_source


def add_dataset(
    database: Database,
    source_path: str | Path,
    *,
    name: str,
    language: str,
    purpose: str,
    mapping: Mapping[str, Any],
    description: str | None = None,
) -> dict[str, Any]:
    if not name.strip() or not language.strip() or not purpose.strip():
        raise DatasetValidationError("Name, language, and purpose are required")
    inspection = inspect_source(source_path)
    normalized = normalize_mapping(mapping, format_name=inspection["format"])
    fingerprint = file_fingerprint(inspection["path"])
    identifier = new_id()
    now = utc_now()
    database.execute(
        """
        INSERT INTO datasets(
            id, name, source_filename, source_path, format, encoding,
            size_bytes, source_mtime_ns, source_checksum,
            language, purpose, description, mapping_json,
            record_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identifier,
            name.strip(),
            inspection["source_filename"],
            inspection["path"],
            inspection["format"],
            inspection["encoding"],
            fingerprint["size_bytes"],
            fingerprint["mtime_ns"],
            fingerprint["checksum"],
            language.strip(),
            purpose.strip(),
            description.strip() if description and description.strip() else None,
            json_text(normalized),
            int(inspection["record_estimate"]),
            now,
            now,
        ),
    )
    record = get_dataset(database, identifier)
    assert record is not None
    return record


def get_dataset(database: Database, dataset_id: str) -> dict[str, Any] | None:
    row = database.fetch_one(
        """
        SELECT d.*,
               CASE WHEN d.source_changed = 0 AND EXISTS (
                   SELECT 1 FROM prepared_datasets p
                   WHERE p.dataset_id = d.id
                     AND p.status = 'ready'
                     AND p.verified = 1
                     AND p.source_checksum = d.source_checksum
               ) THEN 1 ELSE 0 END AS training_ready,
               (
                   SELECT p.path FROM prepared_datasets p
                   WHERE p.dataset_id = d.id
                     AND p.status = 'ready'
                     AND p.verified = 1
                     AND p.source_checksum = d.source_checksum
                   ORDER BY p.created_at DESC
                   LIMIT 1
               ) AS latest_prepared_path
        FROM datasets d
        WHERE d.id = ?
        """,
        (dataset_id,),
    )
    return dataset_record(row) if row else None


def list_datasets(database: Database) -> list[dict[str, Any]]:
    rows = database.fetch_all(
        """
        SELECT d.*,
               CASE WHEN d.source_changed = 0 AND EXISTS (
                   SELECT 1 FROM prepared_datasets p
                   WHERE p.dataset_id = d.id
                     AND p.status = 'ready'
                     AND p.verified = 1
                     AND p.source_checksum = d.source_checksum
               ) THEN 1 ELSE 0 END AS training_ready,
               (
                   SELECT p.path FROM prepared_datasets p
                   WHERE p.dataset_id = d.id
                     AND p.status = 'ready'
                     AND p.verified = 1
                     AND p.source_checksum = d.source_checksum
                   ORDER BY p.created_at DESC
                   LIMIT 1
               ) AS latest_prepared_path
        FROM datasets d
        ORDER BY d.created_at DESC
        """
    )
    return [dataset_record(row) for row in rows]


def dataset_record(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["mapping"] = parse_json(result.pop("mapping_json"))
    result["validation_summary"] = parse_json(
        result.pop("validation_summary_json"), None
    )
    result["source_changed"] = bool(result["source_changed"])
    candidate_ready = bool(result["training_ready"])
    latest_path = result.pop("latest_prepared_path", None)
    contract = (
        prepared_contract_summary(Path(latest_path))
        if candidate_ready and latest_path
        else {
            "format_version": None,
            "training_eligible": False,
            "contract_problems": [],
        }
    )
    result["prepared_contract"] = contract
    result["training_ready"] = candidate_ready and bool(
        contract["training_eligible"]
    )
    return result


def check_source_changed(
    database: Database,
    dataset_id: str,
    *,
    force_checksum: bool = True,
) -> dict[str, Any]:
    dataset = get_dataset(database, dataset_id)
    if dataset is None:
        raise KeyError(f"Dataset does not exist: {dataset_id}")
    path = Path(dataset["source_path"])
    if not path.is_file():
        mark_source_stale(database, dataset_id)
        return {
            "dataset_id": dataset_id,
            "changed": True,
            "reason": "missing",
            "previous_checksum": dataset["source_checksum"],
            "current_checksum": None,
        }
    stat = path.stat()
    stat_changed = (
        stat.st_size != dataset["size_bytes"]
        or stat.st_mtime_ns != dataset["source_mtime_ns"]
    )
    if not force_checksum and not stat_changed:
        return {
            "dataset_id": dataset_id,
            "changed": bool(dataset["source_changed"]),
            "reason": "metadata_unchanged",
            "previous_checksum": dataset["source_checksum"],
            "current_checksum": dataset["source_checksum"],
        }
    current_checksum = sha256_file(path)
    changed = current_checksum != dataset["source_checksum"]
    now = utc_now()
    if changed:
        mark_source_stale(database, dataset_id)
    else:
        with database.transaction() as connection:
            connection.execute(
                """
                UPDATE datasets
                SET size_bytes = ?, source_mtime_ns = ?, source_changed = 0,
                    validation_status = CASE
                        WHEN validation_status = 'stale'
                             AND validation_summary_json IS NOT NULL
                        THEN 'valid'
                        ELSE validation_status
                    END,
                    prepared_status = CASE WHEN EXISTS (
                        SELECT 1 FROM prepared_datasets p
                        WHERE p.dataset_id = datasets.id
                          AND p.source_checksum = datasets.source_checksum
                          AND p.verified = 1
                    ) THEN 'ready' ELSE prepared_status END,
                    updated_at = ?
                WHERE id = ?
                """,
                (stat.st_size, stat.st_mtime_ns, now, dataset_id),
            )
            connection.execute(
                """
                UPDATE prepared_datasets
                SET status = 'ready'
                WHERE dataset_id = ? AND source_checksum = ? AND verified = 1
                """,
                (dataset_id, current_checksum),
            )
    return {
        "dataset_id": dataset_id,
        "changed": changed,
        "reason": "checksum_changed" if changed else "checksum_matches",
        "previous_checksum": dataset["source_checksum"],
        "current_checksum": current_checksum,
    }


def mark_source_stale(database: Database, dataset_id: str) -> None:
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE datasets
            SET source_changed = 1,
                validation_status = 'stale',
                prepared_status = CASE
                    WHEN prepared_status = 'ready' THEN 'stale'
                    ELSE prepared_status
                END,
                token_count = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, dataset_id),
        )
        connection.execute(
            """
            UPDATE prepared_datasets
            SET status = 'stale'
            WHERE dataset_id = ? AND status = 'ready'
            """,
            (dataset_id,),
        )
