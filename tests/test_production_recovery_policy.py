from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.backend.application import Application
from app.backend.database.control import json_text, new_id, utc_now


def _project(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(source, tmp_path / "config")
    (tmp_path / "config" / "local.toml").write_text(
        """
[training]
device = "cpu"
precision = "fp32"

[server]
host = "127.0.0.1"
port = 0
""",
        encoding="utf-8",
    )
    return tmp_path


def _version(application: Application) -> str:
    identifier = new_id()
    root = application.paths.versions / identifier
    root.mkdir(parents=True)
    (root / "model.safetensors").write_bytes(b"forensic")
    now = utc_now()
    application.database.execute(
        """
        INSERT INTO saved_versions(
            id,label,checkpoint_path,checksum,size_bytes,total_trained_steps,
            additional_steps,architecture_revision,context_tokens,
            tokenizer_checksum,integrity,imported,created_at,verified_at
        ) VALUES (?,?,?,?,?,NULL,2000,2,8192,?,'verified',0,?,?)
        """,
        (
            identifier,
            "Malformed descendant",
            str(root.resolve()),
            "a" * 64,
            8,
            "b" * 64,
            now,
            now,
        ),
    )
    application.database.execute(
        """
        INSERT INTO saved_version_policies(
            saved_version_id,classification,friendly_name,activation_allowed,
            continuation_allowed,deletion_protected,recommended,rationale,
            evidence_json,created_at,updated_at
        ) VALUES (
            ?,'regressed_forensic','Malformed OASST2 +2,000 (forensic)',
            0,0,1,0,'Scientifically regressed.','{}',?,?
        )
        """,
        (identifier, now, now),
    )
    return identifier


def test_regressed_forensic_version_is_blocked_by_backend_actions(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        identifier = _version(application)

        record = application.list_versions()[0]
        assert record["id"] == identifier
        assert record["friendly_name"].endswith("(forensic)")
        assert record["available_actions"]["use_in_chat"]["enabled"] is False
        assert record["available_actions"]["continue_training"]["enabled"] is False
        assert record["available_actions"]["delete"]["enabled"] is False
        assert record["primary_action"] == {
            "key": "forensic_read_only",
            "label": "View evidence",
        }

        with pytest.raises(ValueError, match="cannot be used in Chat"):
            application.activate_version(identifier, "activate-forensic")
        with pytest.raises(ValueError, match="cannot be continued"):
            application.start_training(
                {"starting_version_id": identifier},
                "continue-forensic",
            )
        assert application.operations.list() == []
    finally:
        application.close()


def test_archived_training_is_history_not_current_status(tmp_path: Path) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        operation_id = new_id()
        training_id = new_id()
        now = utc_now()
        with application.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operations(
                    id,type,target_id,state,phase,current_progress,total_progress,
                    created_at,updated_at,finished_at,result_json
                ) VALUES (
                    ?, 'training', 'historical', 'completed', 'Completed',
                    1,1,?,?,?,'{}'
                )
                """,
                (operation_id, now, now, now),
            )
            connection.execute(
                """
                INSERT INTO training_operations(
                    id,operation_id,requested_steps,completed_steps,
                    settings_json,post_training_policy,post_training_state,
                    post_training_recovery_state,created_at,updated_at
                ) VALUES (
                    ?,?,20000,20000,?,'evaluate_and_activate',
                    'needs_attention','resume_pending',?,?
                )
                """,
                (training_id, operation_id, json_text({}), now, now),
            )
            connection.execute(
                """
                INSERT INTO training_operation_policies(
                    training_operation_id,classification,visibility,
                    action_policy,display_name,evidence_json,created_at,updated_at
                ) VALUES (
                    ?,'regressed_forensic','history','forensic_read_only',
                    'Malformed OASST2 historical run','{}',?,?
                )
                """,
                (training_id, now, now),
            )

        assert application.training_status() is None
        history = application.training_history()
        assert len(history) == 1
        assert history[0]["id"] == operation_id
        assert history[0]["action_policy"] == "forensic_read_only"
        with pytest.raises(ValueError, match="read-only forensic"):
            application.retry_training_finalisation(
                operation_id, "retry-forensic"
            )
    finally:
        application.close()

