"""Explicit disposable packaged end-to-end validator.

This module refuses production-like destinations. It is shipped so the staged
package can prove its own private worker and training path without importing
repository utilities.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from .application import Application
from .database.control import utc_now
from .system.files import atomic_write_json
from .versions.checkpoint import inspect_checkpoint
from .versions.model import ModelConfig


def _safe_disposable_root(raw: str) -> Path:
    root = Path(raw).resolve()
    if "salty-disposable-e2e" not in root.name.casefold():
        raise ValueError("workspace name must contain salty-disposable-e2e")
    if root.exists() and any(root.iterdir()):
        raise ValueError("disposable workspace must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _wait(application: Application, operation: dict, *, timeout: float) -> dict:
    terminal = application.operations.wait(operation["id"], timeout=timeout)
    if terminal["state"] != "completed":
        raise RuntimeError(json.dumps(terminal, sort_keys=True))
    return terminal


def run(
    *,
    project_root: Path,
    workspace: Path,
    baseline_source: Path,
) -> dict:
    workspace = _safe_disposable_root(str(workspace))
    baseline_source = baseline_source.resolve()
    if workspace in baseline_source.parents or baseline_source == workspace:
        raise ValueError("baseline source must be outside the disposable workspace")
    baseline_id = str(uuid.uuid4())
    baseline = workspace / "versions" / baseline_id
    baseline.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(baseline_source, baseline)
    shutil.copytree(baseline / "tokenizer", workspace / "tokenizer")

    source = workspace / "source" / "tiny-conversations.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "messages": [
                {"role": "user", "content": f"Question {index}: name a careful potato habit."},
                {
                    "role": "assistant",
                    "content": f"Answer {index}: verify the evidence before reporting success.",
                },
            ]
        }
        for index in range(24)
    ]
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)

    application = Application(project_root, recover_operations=False)
    try:
        baseline_report = inspect_checkpoint(baseline, calculate_checksum=True)
        if not baseline_report.valid or not baseline_report.weight_sha256:
            raise RuntimeError("; ".join(baseline_report.errors))
        config = ModelConfig.from_file(baseline / "config.json")
        manifest = json.loads((baseline / "manifest.json").read_text(encoding="utf-8"))
        now = utc_now()
        application.database.execute(
            """
            INSERT INTO saved_versions(
                id, label, checkpoint_path, checksum, size_bytes,
                total_trained_steps, additional_steps, architecture_revision,
                context_tokens, tokenizer_checksum, integrity, imported,
                created_at, verified_at
            ) VALUES (?, 'Disposable baseline', ?, ?, ?, ?, 0, ?, ?, ?,
                      'verified', 1, ?, ?)
            """,
            (
                baseline_id,
                str(baseline),
                baseline_report.weight_sha256,
                sum(path.stat().st_size for path in baseline.rglob("*") if path.is_file()),
                manifest.get("total_steps"),
                config.architecture_version,
                config.max_position_embeddings,
                manifest["tokenizer_fingerprint"],
                now,
                now,
            ),
        )
        dataset = application.add_dataset(
            {
                "path": str(source),
                "name": "Disposable packaged training",
                "language": "English",
                "purpose": "Package validation only",
                "description": "Synthetic disposable end-to-end fixture.",
                "mapping": {
                    "type": "conversation",
                    "conversation_messages": "messages",
                },
            }
        )
        validation = _wait(
            application,
            application.validate_dataset(dataset["id"], "e2e-validate"),
            timeout=120,
        )
        preparation = _wait(
            application,
            application.prepare_dataset(
                dataset["id"],
                {"sequence_length": 32},
                "e2e-prepare",
            ),
            timeout=180,
        )
        prepared_id = preparation["result"]["prepared_dataset_id"]
        training = _wait(
            application,
            application.start_training(
                {
                    "prepared_dataset_id": prepared_id,
                    "starting_version_id": baseline_id,
                    "additional_steps": 1,
                    "use_completed_version_in_chat": True,
                    "settings": {
                        "sequence_length": 32,
                        "micro_batch_size": 1,
                        "gradient_accumulation": 1,
                        "warmup_steps": 0,
                        "validation_interval": 1,
                        "recovery_interval": 1,
                        "pinned_memory": False,
                    },
                },
                "e2e-training",
            ),
            timeout=1800,
        )
        version_id = training["result"]["saved_version_id"]
        versions = application.database.fetch_one(
            "SELECT COUNT(*) AS count FROM saved_versions WHERE id = ?",
            (version_id,),
        )
        attempts = application.database.fetch_all(
            """
            SELECT id, result_path, package_build_id, result_status, invalidated_at
            FROM finalisation_attempts WHERE operation_id = ?
            """,
            (training["id"],),
        )
        runtime = application.runtime.identity
        evidence = {
            "workspace": str(workspace),
            "project_root": str(project_root.resolve()),
            "dataset_id": dataset["id"],
            "validation_operation_id": validation["id"],
            "preparation_operation_id": preparation["id"],
            "training_operation_id": training["id"],
            "training_state": training["state"],
            "completion_outcome": training["result"]["completion_outcome"],
            "saved_version_id": version_id,
            "saved_version_registration_count": int(versions["count"]),
            "verification_attempts": attempts,
            "runtime_identity": runtime.to_dict() if runtime else None,
            "runtime_matches": bool(
                runtime
                and runtime.checkpoint_id == version_id
                and runtime.weight_sha256 == training["result"]["checksum"]
            ),
            "production_output_paths_used": False,
        }
    finally:
        application.close()

    restarted = Application(project_root, recover_operations=True)
    try:
        persisted = restarted.database.get_operation(evidence["training_operation_id"])
        evidence["restart_state"] = persisted["state"]
        evidence["restart_outcome"] = persisted.get("outcome")
        evidence["restart_phase"] = persisted["phase"]
    finally:
        restarted.close()
    atomic_write_json(workspace / "packaged-e2e-result.json", evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--baseline-source", required=True)
    arguments = parser.parse_args()
    result = run(
        project_root=Path(arguments.project_root),
        workspace=Path(arguments.workspace),
        baseline_source=Path(arguments.baseline_source),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
