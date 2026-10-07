"""Print the persisted identity chain for one committed Chat generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _row(connection: sqlite3.Connection, query: str, parameters=()):
    result = connection.execute(query, parameters).fetchone()
    return dict(result) if result is not None else None


def _rows(connection: sqlite3.Connection, query: str, parameters=()):
    return [dict(row) for row in connection.execute(query, parameters)]


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def trace_generation(
    database_path: str | Path,
    generation_id: str,
) -> dict[str, Any]:
    database = Path(database_path).resolve()
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        assistant = None
        for candidate in _rows(
            connection,
            """
            SELECT * FROM messages
            WHERE role = 'assistant'
            ORDER BY created_at DESC
            """,
        ):
            details = _json(candidate.get("technical_details_json"), {})
            if str(
                details.get("generation_id")
                or details.get("generation_operation_id")
            ) == str(generation_id):
                assistant = candidate
                assistant["technical_details"] = details
                break
        if assistant is None:
            raise KeyError(
                f"No committed assistant response owns generation {generation_id}"
            )

        details = assistant["technical_details"]
        version_id = (
            assistant.get("saved_version_id")
            or details.get("active_version_id")
            or details.get("checkpoint_id")
        )
        runtime_id = assistant.get("runtime_id") or details.get("runtime_id")
        version = _row(
            connection,
            "SELECT * FROM saved_versions WHERE id = ?",
            (version_id,),
        )
        if version is None:
            raise RuntimeError(
                f"Generation references missing saved version {version_id}"
            )
        lineages = _rows(
            connection,
            """
            SELECT vl.*, dl.dataset_id, dl.prepared_dataset_id,
                   dl.dataset_fingerprint, dl.prepared_fingerprint,
                   dl.tokenizer_fingerprint, dl.architecture_identity,
                   dl.baseline_version_id
            FROM version_lineage vl
            JOIN dataset_lineages dl ON dl.id = vl.lineage_id
            WHERE vl.saved_version_id = ?
            ORDER BY vl.created_at
            """,
            (version_id,),
        )
        training = (
            _row(
                connection,
                "SELECT * FROM training_operations WHERE id = ?",
                (version.get("training_operation_id"),),
            )
            if version.get("training_operation_id")
            else None
        )
        prepared_id = version.get("prepared_dataset_id") or (
            training.get("prepared_dataset_id") if training else None
        )
        prepared = (
            _row(
                connection,
                "SELECT * FROM prepared_datasets WHERE id = ?",
                (prepared_id,),
            )
            if prepared_id
            else None
        )
        dataset = (
            _row(
                connection,
                "SELECT * FROM datasets WHERE id = ?",
                (prepared.get("dataset_id"),),
            )
            if prepared
            else None
        )
        runtime = (
            _row(
                connection,
                "SELECT * FROM runtime_states WHERE id = ?",
                (runtime_id,),
            )
            if runtime_id
            else None
        )
        evaluations = _rows(
            connection,
            """
            SELECT id, operation_id, status, records_completed, total_records,
                   metrics_json, result_path, result_checksum, finished_at
            FROM evaluations
            WHERE saved_version_id = ?
            ORDER BY created_at
            """,
            (version_id,),
        )
        for evaluation in evaluations:
            evaluation["metrics"] = _json(
                evaluation.pop("metrics_json", None),
                {},
            )

        prepared_manifest = None
        prepared_manifest_sha256 = None
        if prepared:
            manifest_path = Path(prepared["path"]) / "manifest.json"
            prepared_manifest_sha256 = _sha256(manifest_path)
            if manifest_path.is_file():
                prepared_manifest = _json(
                    manifest_path.read_text(encoding="utf-8"),
                    None,
                )
        checkpoint_path = Path(version["checkpoint_path"])
        config_sha256 = _sha256(checkpoint_path / "config.json")
        training_settings = (
            _json(training.get("settings_json"), {}) if training else {}
        )
        missing = []
        for label, value in (
            ("conversation_id", assistant.get("conversation_id")),
            ("user_message_id", details.get("user_message_id")),
            ("generation_id", generation_id),
            ("template_version", details.get("template_version")),
            ("runtime", runtime),
            ("saved_version", version),
            ("prepared_dataset", prepared),
            ("raw_dataset", dataset),
            ("prepared_manifest", prepared_manifest),
        ):
            if value is None or value == "":
                missing.append(label)

        return {
            "format": "salty-potato-generation-identity-chain-v1",
            "complete": not missing,
            "missing_links": missing,
            "chat_generation": {
                "generation_id": generation_id,
                "conversation_id": assistant.get("conversation_id"),
                "user_message_id": details.get("user_message_id"),
                "assistant_message_id": assistant.get("id"),
                "created_at": assistant.get("created_at"),
                "template_version": details.get("template_version"),
                "cancellation_token": details.get("cancellation_token"),
                "generation_state": details.get("generation_state"),
            },
            "active_runtime": {
                "runtime_id": runtime_id,
                "record": runtime,
            },
            "saved_version": {
                **version,
                "model_config_sha256_current": config_sha256,
                "lineages": lineages,
            },
            "training_run": {
                "record": training,
                "settings": training_settings,
                "training_config_digest": (
                    _canonical_digest(training_settings) if training else None
                ),
            },
            "evaluations": evaluations,
            "prepared_dataset": {
                "record": prepared,
                "manifest_sha256_current": prepared_manifest_sha256,
                "manifest": prepared_manifest,
            },
            "raw_dataset": dataset,
            "identity_summary": {
                "raw_dataset_digest": (
                    dataset.get("source_checksum") if dataset else None
                ),
                "prepared_dataset_digest": (
                    prepared.get("artifact_checksum") if prepared else None
                ),
                "tokenizer_digest": version.get("tokenizer_checksum"),
                "model_config_digest": config_sha256,
                "checkpoint_digest": version.get("checksum"),
                "runtime_export_digest": (
                    runtime.get("artifact_checksum") if runtime else None
                ),
            },
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Trace one committed Chat response back through runtime, checkpoint, "
            "training, prepared data, and raw dataset identities."
        )
    )
    parser.add_argument("generation_id")
    parser.add_argument(
        "--database",
        default="workspace/control/salty-potato.db",
        help="Path to the Salty Steak control database.",
    )
    arguments = parser.parse_args()
    try:
        payload = trace_generation(arguments.database, arguments.generation_id)
    except (KeyError, RuntimeError, OSError, sqlite3.Error) as error:
        print(json.dumps({"success": False, "error": str(error)}, indent=2))
        return 1
    print(json.dumps({"success": True, "identity_chain": payload}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
