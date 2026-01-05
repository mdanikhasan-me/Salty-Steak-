"""Real packaged timeout and restart-recovery validation on disposable data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from .application import Application
from .database.control import json_text, new_id, utc_now
from .system.files import atomic_write_json
from .versions.checkpoint import IsolatedWorkerError
from .versions.finalisation import FinalisationCoordinator


def _insert_incomplete_training(
    application: Application,
    *,
    checkpoint: Path,
    prepared_id: str,
    baseline_id: str,
    failure_code: str,
) -> tuple[str, dict]:
    operation, _ = application.database.create_operation("training")
    manifest_path = checkpoint / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["operation_id"] = operation["id"]
    manifest["prepared_dataset_id"] = prepared_id
    manifest["starting_version_id"] = baseline_id
    manifest["additional_steps"] = 1
    atomic_write_json(manifest_path, manifest)
    now = utc_now()
    original_error = {
        "code": failure_code,
        "message": "Original finalisation failure preserved for restart audit.",
    }
    with application.database.transaction() as connection:
        connection.execute(
            """
            UPDATE operations
            SET state = 'failed', phase = 'Finalisation requires attention',
                error_json = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (json_text(original_error), now, now, operation["id"]),
        )
        connection.execute(
            """
            INSERT INTO training_operations(
                id, operation_id, prepared_dataset_id, starting_version_id,
                requested_steps, completed_steps, settings_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 1, '{}', ?, ?)
            """,
            (
                new_id(),
                operation["id"],
                prepared_id,
                baseline_id,
                now,
                now,
            ),
        )
    return operation["id"], original_error


def run(project_root: Path, workspace: Path) -> dict:
    workspace = workspace.resolve()
    if "salty-disposable-e2e" not in workspace.name.casefold():
        raise ValueError("workspace is not an approved disposable fixture")
    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)
    application = Application(project_root, recover_operations=False)
    try:
        lineage = application.database.fetch_one(
            """
            SELECT l.prepared_dataset_id, l.baseline_version_id,
                   vl.saved_version_id
            FROM dataset_lineages l
            JOIN version_lineage vl ON vl.lineage_id = l.id
            WHERE vl.role = 'trained'
            ORDER BY vl.created_at DESC LIMIT 1
            """
        )
        if not lineage:
            raise RuntimeError("the real packaged training fixture is missing")
        source = Path(
            application.database.fetch_one(
                "SELECT checkpoint_path FROM saved_versions WHERE id = ?",
                (lineage["saved_version_id"],),
            )["checkpoint_path"]
        )

        timeout_checkpoint = workspace / "versions" / str(uuid.uuid4())
        shutil.copytree(source, timeout_checkpoint)
        timeout_operation, timeout_error = _insert_incomplete_training(
            application,
            checkpoint=timeout_checkpoint,
            prepared_id=lineage["prepared_dataset_id"],
            baseline_id=lineage["baseline_version_id"],
            failure_code="fixture_timeout",
        )
        timeout_coordinator = FinalisationCoordinator(
            application.database,
            workspace / "training" / "timeout-results",
            bootstrap_timeout_seconds=10,
            integrity_timeout_seconds=0.001,
            model_load_timeout_seconds=0.001,
            verification_timeout_seconds=0.01,
        )
        timeout_raised = False
        try:
            timeout_coordinator.verify(
                operation_id=timeout_operation,
                checkpoint_path=timeout_checkpoint,
                context_limit=8192,
                device="cpu",
            )
        except IsolatedWorkerError:
            timeout_raised = True
        timeout_attempt = application.database.fetch_one(
            """
            SELECT * FROM finalisation_attempts
            WHERE operation_id = ? ORDER BY started_at DESC LIMIT 1
            """,
            (timeout_operation,),
        )
    finally:
        application.close()

    after_timeout_restart = Application(project_root, recover_operations=True)
    try:
        timeout_record = after_timeout_restart.database.get_operation(timeout_operation)
        timeout_registration = after_timeout_restart.database.fetch_one(
            """
            SELECT result_version_id FROM training_operations
            WHERE operation_id = ?
            """,
            (timeout_operation,),
        )

        recovery_checkpoint = workspace / "versions" / str(uuid.uuid4())
        shutil.copytree(source, recovery_checkpoint)
        recovery_operation, recovery_error = _insert_incomplete_training(
            after_timeout_restart,
            checkpoint=recovery_checkpoint,
            prepared_id=lineage["prepared_dataset_id"],
            baseline_id=lineage["baseline_version_id"],
            failure_code="fixture_recoverable",
        )
        result, passed_attempt = after_timeout_restart.finalisation.verify(
            operation_id=recovery_operation,
            checkpoint_path=recovery_checkpoint,
            context_limit=8192,
            device="cpu",
        )
    finally:
        after_timeout_restart.close()

    recovered_restart = Application(project_root, recover_operations=True)
    try:
        recovered = recovered_restart.database.get_operation(recovery_operation)
        recovery_training = recovered_restart.database.fetch_one(
            """
            SELECT result_version_id FROM training_operations
            WHERE operation_id = ?
            """,
            (recovery_operation,),
        )
        recovery_count = recovered_restart.database.fetch_one(
            """
            SELECT COUNT(*) AS count FROM saved_versions
            WHERE checkpoint_path = ?
            """,
            (str(recovery_checkpoint.resolve()),),
        )
        events = recovered_restart.database.fetch_all(
            """
            SELECT event_type FROM operation_events
            WHERE operation_id = ? ORDER BY created_at
            """,
            (recovery_operation,),
        )
        evidence = {
            "timeout_operation_id": timeout_operation,
            "timeout_raised": timeout_raised,
            "timeout_attempt_status": timeout_attempt["result_status"],
            "timeout_attempt_invalidated": bool(timeout_attempt["invalidated_at"]),
            "timeout_worker_pid": timeout_attempt["worker_pid"],
            "timeout_restart_state": timeout_record["state"],
            "timeout_restart_phase": timeout_record["phase"],
            "timeout_error_preserved": timeout_record["error"] == timeout_error,
            "timeout_registered_version": timeout_registration["result_version_id"],
            "recovery_operation_id": recovery_operation,
            "recovery_attempt_id": passed_attempt["id"],
            "recovery_verification_success": result["success"],
            "recovery_restart_state": recovered["state"],
            "recovery_restart_phase": recovered["phase"],
            "recovery_outcome": recovered["outcome"],
            "recovery_error_preserved": recovered["error"] == recovery_error,
            "recovery_saved_version_id": recovery_training["result_version_id"],
            "recovery_registration_count": int(recovery_count["count"]),
            "recovery_events": [event["event_type"] for event in events],
        }
    finally:
        recovered_restart.close()
    atomic_write_json(workspace / "packaged-recovery-result.json", evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--workspace", required=True)
    arguments = parser.parse_args()
    result = run(Path(arguments.project_root), Path(arguments.workspace))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
