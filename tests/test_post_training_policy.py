from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from app.backend.application import Application
from app.backend.database.control import Database, json_text, utc_now
from app.backend.runtime.manager import RuntimeIdentity
from app.backend.system.files import sha256_file
from app.backend.training.policy import normalise_post_training_policy


def test_explicit_and_legacy_policy_inputs_are_truthful() -> None:
    assert (
        normalise_post_training_policy(
            {"post_training_policy": "evaluate_and_activate"}
        )
        == "evaluate_and_activate"
    )
    assert (
        normalise_post_training_policy({"use_completed_version_in_chat": True})
        == "evaluate_and_activate"
    )
    assert (
        normalise_post_training_policy({"use_completed_version_in_chat": False})
        == "save_only"
    )
    with pytest.raises(ValueError, match="must be one of"):
        normalise_post_training_policy({"post_training_policy": "activate"})
    with pytest.raises(ValueError, match="must be a boolean"):
        normalise_post_training_policy(
            {"use_completed_version_in_chat": "false"}
        )


def _historical_training(
    database: Database,
    root: Path,
    *,
    stem: str,
    legacy_value: object,
) -> tuple[str, str]:
    prefix = stem * 8
    operation_id = f"{prefix}-0000-4000-8000-000000000001"
    training_id = f"{prefix}-0000-4000-8000-000000000002"
    version_id = f"{prefix}-0000-4000-8000-000000000003"
    now = utc_now()
    checkpoint = root / version_id
    checkpoint.mkdir(parents=True)
    database.execute(
        """
        INSERT INTO operations(
            id, type, state, phase, current_progress, total_progress,
            created_at, updated_at, finished_at, result_json
        ) VALUES (?, 'training', 'completed', 'Completed', 1, 1, ?, ?, ?, ?)
        """,
        (
            operation_id,
            now,
            now,
            now,
            json_text({"use_completed_version_in_chat": legacy_value}),
        ),
    )
    database.execute(
        """
        INSERT INTO training_operations(
            id, operation_id, requested_steps, completed_steps, settings_json,
            created_at, updated_at
        ) VALUES (?, ?, 10, 10, ?, ?, ?)
        """,
        (
            training_id,
            operation_id,
            json_text({"use_completed_version_in_chat": legacy_value}),
            now,
            now,
        ),
    )
    database.execute(
        """
        INSERT INTO saved_versions(
            id, training_operation_id, label, checkpoint_path, checksum,
            size_bytes, total_trained_steps, additional_steps,
            architecture_revision, context_tokens, tokenizer_checksum,
            integrity, imported, created_at, verified_at
        ) VALUES (?, ?, 'Historical version', ?, ?, 0, 10, 10, 2, 8192, ?,
                  'verified', 0, ?, ?)
        """,
        (
            version_id,
            training_id,
            str(checkpoint),
            stem * 64,
            "f" * 64,
            now,
            now,
        ),
    )
    database.execute(
        "UPDATE training_operations SET result_version_id = ? WHERE id = ?",
        (version_id, training_id),
    )
    return training_id, operation_id


def test_schema_migration_maps_only_proven_legacy_boolean_intent(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "control.db")
    true_id, _ = _historical_training(
        database,
        tmp_path / "versions",
        stem="1",
        legacy_value=True,
    )
    false_id, _ = _historical_training(
        database,
        tmp_path / "versions",
        stem="2",
        legacy_value=False,
    )
    unknown_id, _ = _historical_training(
        database,
        tmp_path / "versions",
        stem="3",
        legacy_value="false",
    )

    with database.transaction() as connection:
        Database._migrate_training_policy(connection)
        # Re-running the additive migration must not reinterpret rows.
        Database._migrate_training_policy(connection)

    true_row = database.fetch_one(
        "SELECT * FROM training_operations WHERE id = ?", (true_id,)
    )
    false_row = database.fetch_one(
        "SELECT * FROM training_operations WHERE id = ?", (false_id,)
    )
    unknown_row = database.fetch_one(
        "SELECT * FROM training_operations WHERE id = ?", (unknown_id,)
    )
    assert (
        true_row["post_training_policy"],
        true_row["post_training_state"],
        true_row["post_training_recovery_state"],
    ) == ("evaluate_and_activate", "needs_attention", "manual_only")
    assert (
        false_row["post_training_policy"],
        false_row["post_training_state"],
        false_row["post_training_recovery_state"],
    ) == ("save_only", "saved", "manual_only")
    assert (
        unknown_row["post_training_policy"],
        unknown_row["post_training_state"],
        unknown_row["post_training_recovery_state"],
    ) == ("legacy_unknown", "legacy_unknown", "legacy_unknown")


def test_fresh_schema_contains_durable_policy_and_activation_attempts(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "control.db")
    training_columns = {
        row["name"]
        for row in database.fetch_all("PRAGMA table_info(training_operations)")
    }
    assert {
        "post_training_policy",
        "post_training_state",
        "post_training_recovery_state",
        "required_evaluation_id",
        "activation_attempt_id",
        "confirmed_runtime_id",
        "post_training_error_json",
    } <= training_columns
    assert database.fetch_one(
        """
        SELECT 1 AS present FROM sqlite_master
        WHERE type = 'table' AND name = 'activation_attempts'
        """
    ) == {"present": 1}


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


class _RecoveryContext:
    operation_id = "44444444-4444-4444-8444-444444444444"

    def __init__(self) -> None:
        self.phases: list[str] = []

    def update(self, *, phase=None, **_details) -> None:
        if phase:
            self.phases.append(str(phase))


def test_save_only_recovery_never_evaluates_or_activates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        training_id, original_operation_id = _historical_training(
            application.database,
            application.paths.versions,
            stem="5",
            legacy_value=False,
        )
        application.database.execute(
            """
            UPDATE training_operations
            SET post_training_policy = 'save_only',
                post_training_state = 'needs_attention',
                post_training_recovery_state = 'resume_pending'
            WHERE id = ?
            """,
            (training_id,),
        )
        training = application.database.fetch_one(
            "SELECT * FROM training_operations WHERE id = ?", (training_id,)
        )

        def unexpected(*_args, **_kwargs):
            raise AssertionError("save-only recovery requested evaluation or activation")

        monkeypatch.setattr(application, "_valid_evaluation_evidence", unexpected)
        monkeypatch.setattr(application, "_evaluation_worker", unexpected)
        monkeypatch.setattr(application, "_activate_worker", unexpected)
        context = _RecoveryContext()
        result = application._post_training_recovery_worker(
            original_operation_id,
            training,
            context,
        )

        assert result["completion_outcome"] == "saved"
        assert result["active_in_chat"] is False
        assert "evaluation_id" not in result
        assert "Preparing evaluation" not in context.phases
        recovered = application.database.fetch_one(
            """
            SELECT post_training_state, post_training_recovery_state,
                   required_evaluation_id, confirmed_runtime_id
            FROM training_operations WHERE id = ?
            """,
            (training_id,),
        )
        assert recovered == {
            "post_training_state": "completed",
            "post_training_recovery_state": "complete",
            "required_evaluation_id": None,
            "confirmed_runtime_id": None,
        }
    finally:
        application.close()


def test_retention_attempt_is_durable_and_not_applied_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        _training_id, original_operation_id = _historical_training(
            application.database,
            application.paths.versions,
            stem="4",
            legacy_value=False,
        )
        calls = 0

        def rotate():
            nonlocal calls
            calls += 1
            return {
                "completed": True,
                "versions_deleted": [],
                "versions_deleted_count": 0,
            }

        monkeypatch.setattr(application, "_rotate_versions", rotate)
        context = _RecoveryContext()
        first = application._apply_post_training_retention(
            context,
            owner_operation_id=original_operation_id,
        )
        second = application._apply_post_training_retention(
            context,
            owner_operation_id=original_operation_id,
        )
        assert calls == 1
        assert first["completed"] is True
        assert second["reused_durable_attempt"] is True
        events = application.database.fetch_all(
            """
            SELECT event_type FROM operation_events
            WHERE operation_id = ?
              AND event_type LIKE 'post_training_retention_%'
            ORDER BY sequence
            """,
            (original_operation_id,),
        )
        assert [row["event_type"] for row in events] == [
            "post_training_retention_started",
            "post_training_retention_completed",
        ]
    finally:
        application.close()


def _insert_evaluable_version(
    application: Application,
    *,
    stem: str,
    validation_records: int,
) -> tuple[str, str]:
    prefix = stem * 8
    dataset_id = f"{prefix}-1000-4000-8000-000000000001"
    prepared_id = f"{prefix}-1000-4000-8000-000000000002"
    version_id = f"{prefix}-1000-4000-8000-000000000003"
    now = utc_now()
    source = application.paths.datasets / f"{stem}.txt"
    source.write_text("fixed evaluation source", encoding="utf-8")
    prepared = application.paths.prepared / dataset_id / prepared_id
    prepared.mkdir(parents=True)
    checkpoint = application.paths.versions / version_id
    checkpoint.mkdir(parents=True)
    (checkpoint / "model.safetensors").write_bytes(stem.encode("ascii"))
    application.database.execute(
        """
        INSERT INTO datasets(
            id, name, source_filename, source_path, format, encoding,
            size_bytes, source_mtime_ns, source_checksum, language, purpose,
            mapping_json, validation_status, prepared_status, source_changed,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'txt', 'utf-8', ?, ?, ?, 'English',
                  'Evaluation fixture', '{}', 'valid', 'ready', 0, ?, ?)
        """,
        (
            dataset_id,
            f"Dataset {stem}",
            source.name,
            str(source),
            source.stat().st_size,
            source.stat().st_mtime_ns,
            sha256_file(source),
            now,
            now,
        ),
    )
    application.database.execute(
        """
        INSERT INTO prepared_datasets(
            id, dataset_id, source_checksum, path, settings_json,
            record_count, token_count, train_record_count,
            validation_record_count, artifact_checksum, verified, status,
            created_at, verified_at
        ) VALUES (?, ?, ?, ?, '{}', 10, 100, ?, ?, ?, 1, 'ready', ?, ?)
        """,
        (
            prepared_id,
            dataset_id,
            sha256_file(source),
            str(prepared),
            10 - validation_records,
            validation_records,
            stem * 64,
            now,
            now,
        ),
    )
    application.database.execute(
        """
        INSERT INTO saved_versions(
            id, prepared_dataset_id, label, checkpoint_path, checksum,
            size_bytes, total_trained_steps, additional_steps,
            architecture_revision, context_tokens, tokenizer_checksum,
            integrity, imported, created_at, verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, 10, 10, 2, 8192, ?,
                  'verified', 1, ?, ?)
        """,
        (
            version_id,
            prepared_id,
            f"Version {stem}",
            str(checkpoint),
            stem * 64,
            (checkpoint / "model.safetensors").stat().st_size,
            "f" * 64,
            now,
            now,
        ),
    )
    return version_id, prepared_id


def _insert_evaluation(
    application: Application,
    *,
    version_id: str,
    prepared_id: str,
    status: str,
    sequence: int,
) -> str:
    operation_id = f"aaaaaaaa-{sequence:04d}-4000-8000-000000000001"
    evaluation_id = f"bbbbbbbb-{sequence:04d}-4000-8000-000000000002"
    created = f"2026-01-01T00:00:{sequence:02d}.000Z"
    terminal = status in {"completed", "failed", "interrupted"}
    application.database.execute(
        """
        INSERT INTO operations(
            id, type, target_id, state, phase, current_progress,
            total_progress, created_at, updated_at, finished_at
        ) VALUES (?, 'evaluation', ?, ?, ?, 1, 1, ?, ?, ?)
        """,
        (
            operation_id,
            version_id,
            status,
            status.title(),
            created,
            created,
            created if terminal else None,
        ),
    )
    metrics = None
    report_path = None
    report_checksum = None
    if status == "completed":
        version = application.get_version(version_id)
        prepared = application.database.fetch_one(
            "SELECT * FROM prepared_datasets WHERE id = ?", (prepared_id,)
        )
        evaluation_identity = application._evaluation_identity(
            prepared_dataset_id=prepared_id,
            prepared_artifact_checksum=prepared["artifact_checksum"],
            tokenizer_checksum=version["tokenizer_checksum"],
            context_tokens=version["context_tokens"],
        )
        metrics = {
            "format": "salty-potato-evaluation-v2",
            "loss": 2.0,
            "perplexity": 7.389056,
            "records_evaluated": 4,
            "tokens_evaluated": 40,
            "prepared_dataset_id": prepared_id,
            "evaluation_identity": evaluation_identity,
        }
        report = {
            "format": "salty-potato-evaluation-v2",
            "operation_id": operation_id,
            "saved_version_id": version_id,
            "checkpoint_id": version_id,
            "state": "completed",
            "weight_sha256": version["checksum"],
            "records_completed": 4,
            "total_records": 4,
            "tokens_evaluated": 40,
            "mean_loss": 2.0,
            "perplexity": 7.389056,
        }
        report_file = application.paths.evaluations / f"{evaluation_id}.json"
        report_file.write_text(json.dumps(report), encoding="utf-8")
        report_path = str(report_file)
        report_checksum = sha256_file(report_file)
    application.database.execute(
        """
        INSERT INTO evaluations(
            id, operation_id, saved_version_id, status, records_completed,
            total_records, metrics_json, result_path, result_checksum,
            created_at, updated_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evaluation_id,
            operation_id,
            version_id,
            status,
            4 if status == "completed" else 0,
            4,
            json_text(metrics) if metrics is not None else None,
            report_path,
            report_checksum,
            created,
            created,
            created if terminal else None,
        ),
    )
    return evaluation_id


def test_saved_version_primary_actions_follow_durable_evidence(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        version_id, prepared_id = _insert_evaluable_version(
            application,
            stem="6",
            validation_records=4,
        )
        unavailable_id, _ = _insert_evaluable_version(
            application,
            stem="7",
            validation_records=0,
        )

        records = {record["id"]: record for record in application.list_versions()}
        assert records[version_id]["lifecycle_status"] == "verified_unevaluated"
        assert records[version_id]["primary_action"] == {
            "key": "evaluate_and_use_in_chat",
            "label": "Evaluate and use in Chat",
        }
        assert records[unavailable_id]["lifecycle_status"] == "evaluation_unavailable"
        assert records[unavailable_id]["primary_action"]["label"] == (
            "Evaluation unavailable"
        )
        assert records[unavailable_id]["available_actions"]["use_in_chat"][
            "enabled"
        ] is False

        _insert_evaluation(
            application,
            version_id=version_id,
            prepared_id=prepared_id,
            status="failed",
            sequence=1,
        )
        failed = {
            record["id"]: record for record in application.list_versions()
        }[version_id]
        assert failed["lifecycle_status"] == "evaluation_failed"
        assert failed["primary_action"]["label"] == (
            "Retry evaluation and use in Chat"
        )

        evidence_id = _insert_evaluation(
            application,
            version_id=version_id,
            prepared_id=prepared_id,
            status="completed",
            sequence=2,
        )
        evaluated = {
            record["id"]: record for record in application.list_versions()
        }[version_id]
        assert evaluated["lifecycle_status"] == "evaluated_inactive"
        assert evaluated["primary_action"] == {
            "key": "use_in_chat",
            "label": "Use in Chat",
        }
        assert evaluated["evaluation_evidence"]["evaluation_id"] == evidence_id

        stored_evaluation = application.database.fetch_one(
            "SELECT metrics_json FROM evaluations WHERE id = ?", (evidence_id,)
        )
        mismatched_metrics = json.loads(stored_evaluation["metrics_json"])
        mismatched_metrics["loss"] = 9.0
        application.database.execute(
            "UPDATE evaluations SET metrics_json = ? WHERE id = ?",
            (json_text(mismatched_metrics), evidence_id),
        )
        invalid = {
            record["id"]: record for record in application.list_versions()
        }[version_id]
        assert invalid["evaluation_evidence"] is None
        assert invalid["primary_action"]["label"] == "Evaluate and use in Chat"
        application.database.execute(
            "UPDATE evaluations SET metrics_json = ? WHERE id = ?",
            (stored_evaluation["metrics_json"], evidence_id),
        )

        activation_operation_id = "cccccccc-0001-4000-8000-000000000001"
        runtime_id = "dddddddd-0001-4000-8000-000000000002"
        now = utc_now()
        runtime_root = application.paths.runtime / runtime_id
        runtime_root.mkdir(parents=True)
        runtime_file = runtime_root / "runtime.json"
        runtime_file.write_text("{}", encoding="utf-8")
        application.database.execute(
            """
            INSERT INTO operations(
                id, type, target_id, state, phase, current_progress,
                total_progress, created_at, updated_at, finished_at
            ) VALUES (?, 'version_activation', ?, 'completed', 'Completed',
                      1, 1, ?, ?, ?)
            """,
            (activation_operation_id, version_id, now, now, now),
        )
        application.database.execute(
            """
            INSERT INTO runtime_states(
                id, operation_id, saved_version_id, artifact_path,
                artifact_checksum, state, metadata_json, created_at,
                verified_at, loaded_at
            ) VALUES (?, ?, ?, ?, ?, 'loaded', '{}', ?, ?, ?)
            """,
            (
                runtime_id,
                activation_operation_id,
                version_id,
                str(runtime_root),
                sha256_file(runtime_file),
                now,
                now,
                now,
            ),
        )
        application.database.execute(
            """
            INSERT INTO active_runtime(
                singleton, saved_version_id, runtime_id,
                activated_by_operation_id, updated_at
            ) VALUES (1, ?, ?, ?, ?)
            """,
            (version_id, runtime_id, activation_operation_id, now),
        )
        selected = {
            record["id"]: record for record in application.list_versions()
        }[version_id]
        assert selected["lifecycle_status"] == "selected_not_loaded"
        assert selected["primary_action"]["label"] == "Retry activation"

        identity = RuntimeIdentity(
            runtime_id=runtime_id,
            checkpoint_id=version_id,
            saved_version_label="Version 6",
            model_name="Salty Steak",
            total_trained_steps=10,
            checkpoint_path=selected["checkpoint_path"],
            weight_sha256=selected["checksum"],
            context_limit=8192,
            device="cpu",
            precision="fp32",
            artifact_path=str(runtime_root),
        )
        application.runtime._loaded = SimpleNamespace(
            identity=identity,
            model=object(),
            tokenizer=object(),
        )
        active = {
            record["id"]: record for record in application.list_versions()
        }[version_id]
        assert active["lifecycle_status"] == "active_loaded"
        assert active["primary_action"] == {
            "key": "in_chat_now",
            "label": "In Chat now",
        }
    finally:
        application.close()
