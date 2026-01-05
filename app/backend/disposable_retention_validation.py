"""Physical retention proof for the disposable packaged lineage."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from .application import Application
from .system.files import atomic_write_json


def run(project_root: Path, workspace: Path) -> dict:
    workspace = workspace.resolve()
    if "salty-disposable-e2e" not in workspace.name.casefold():
        raise ValueError("workspace is not an approved disposable fixture")
    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)
    application = Application(project_root, recover_operations=False)
    try:
        lineage = application.database.fetch_one(
            "SELECT * FROM dataset_lineages ORDER BY created_at DESC LIMIT 1"
        )
        trained = application.database.fetch_all(
            """
            SELECT v.*, vl.verification_attempt_id
            FROM version_lineage vl
            JOIN saved_versions v ON v.id = vl.saved_version_id
            WHERE vl.lineage_id = ? AND vl.role = 'trained'
            ORDER BY v.verified_at DESC
            """,
            (lineage["id"],),
        )
        if len(trained) != 2:
            raise RuntimeError("fixture must begin with exactly two retained trained versions")
        old_id = str(uuid.uuid4())
        old_path = workspace / "versions" / old_id
        shutil.copytree(Path(trained[-1]["checkpoint_path"]), old_path)
        old_bytes = sum(
            path.stat().st_size for path in old_path.rglob("*") if path.is_file()
        )
        with application.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO saved_versions(
                    id, prepared_dataset_id, label, checkpoint_path, checksum,
                    size_bytes, total_trained_steps, additional_steps,
                    architecture_revision, context_tokens, tokenizer_checksum,
                    integrity, imported, created_at, verified_at
                ) VALUES (?, ?, 'Older disposable trained version', ?, ?, ?,
                          NULL, 1, ?, ?, ?, 'verified', 0, ?, ?)
                """,
                (
                    old_id,
                    lineage["prepared_dataset_id"],
                    str(old_path),
                    trained[-1]["checksum"],
                    old_bytes,
                    trained[-1]["architecture_revision"],
                    trained[-1]["context_tokens"],
                    trained[-1]["tokenizer_checksum"],
                    "2020-01-01T00:00:00.000Z",
                    "2020-01-01T00:00:00.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO version_lineage(
                    saved_version_id, lineage_id, parent_version_id, role,
                    verification_attempt_id, recovered_after_restart,
                    user_protected, cleanup_status, created_at
                ) VALUES (?, ?, ?, 'trained', ?, 0, 0, 'retained', ?)
                """,
                (
                    old_id,
                    lineage["id"],
                    lineage["baseline_version_id"],
                    trained[-1]["verification_attempt_id"],
                    "2020-01-01T00:00:00.000Z",
                ),
            )
        application._rotate_versions()
        remaining = application.database.fetch_all(
            """
            SELECT vl.role, vl.saved_version_id
            FROM version_lineage vl
            JOIN saved_versions v ON v.id = vl.saved_version_id
            WHERE vl.lineage_id = ?
            ORDER BY CASE vl.role WHEN 'baseline' THEN 0 ELSE 1 END,
                     v.verified_at DESC
            """,
            (lineage["id"],),
        )
        journal = application.database.fetch_one(
            """
            SELECT state, expected_bytes, actual_bytes, paths_json
            FROM retention_journal WHERE saved_version_id = ?
            """,
            (old_id,),
        )
        timeout_unregistered = application.database.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM training_operations t JOIN operations o ON o.id = t.operation_id
            WHERE o.state IN ('failed','interrupted')
              AND t.result_version_id IS NULL
            """
        )
        evidence = {
            "lineage_id": lineage["id"],
            "baseline_version_id": lineage["baseline_version_id"],
            "remaining": remaining,
            "remaining_baseline_count": sum(
                row["role"] == "baseline" for row in remaining
            ),
            "remaining_trained_count": sum(
                row["role"] == "trained" for row in remaining
            ),
            "removed_version_id": old_id,
            "removed_path": str(old_path),
            "removed_path_exists": old_path.exists(),
            "journal": journal,
            "failed_or_interrupted_unregistered_runs": int(
                timeout_unregistered["count"]
            ),
        }
    finally:
        application.close()
    atomic_write_json(workspace / "packaged-retention-result.json", evidence)
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
