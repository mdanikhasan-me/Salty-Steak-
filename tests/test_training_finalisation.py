from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.backend.application as application_module
from app.backend.application import Application
from app.backend.database.control import Database, json_text, new_id, utc_now
from app.backend.operations.manager import (
    OperationManager,
    OperationPhaseError,
)
from app.backend.system.files import atomic_write_json
from app.backend.system.timestamps import utc_timestamp
from app.backend.versions.checkpoint import (
    IsolatedWorkerError,
    _private_worker_interpreter,
    _windows_hidden_process_options,
    isolated_smoke_test,
)
from app.backend.versions.model import ModelConfig


def _result_path(command: list[str]) -> Path:
    return Path(command[command.index("--result-file") + 1])


def _command_value(command: list[str], name: str, default: str) -> str:
    return command[command.index(name) + 1] if name in command else default


def _completed_worker_payload(
    checkpoint: Path,
    *,
    verification_attempt_id: str = "standalone",
    operation_id: str = "standalone",
    package_build_id: str = "development",
) -> dict:
    return {
        "success": True,
        "verification_attempt_id": verification_attempt_id,
        "operation_id": operation_id,
        "checkpoint_id": checkpoint.name,
        "checkpoint_path": str(checkpoint.resolve()),
        "manifest_valid": True,
        "weights_exist": True,
        "checksums_valid": True,
        "load_valid": True,
        "parameter_count": 95_177_472,
        "context_limit": 128,
        "worker_pid": 1234,
        "package_build_id": package_build_id,
        "started_at": "2026-07-25T12:00:00Z",
        "finished_at": "2026-07-25T12:00:01Z",
        "error": None,
        "generated_token_ids": [7],
        "digest": "a" * 64,
        "finite": True,
        "device": "cpu",
    }


def test_isolated_worker_uses_atomic_result_when_stdout_is_empty(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    def runner(command, **_kwargs):
        atomic_write_json(
            _result_path(command),
            _completed_worker_payload(
                checkpoint,
                verification_attempt_id=_command_value(
                    command, "--verification-attempt-id", "standalone"
                ),
                operation_id=_command_value(command, "--operation-id", "standalone"),
                package_build_id=_command_value(
                    command, "--package-build-id", "development"
                ),
            ),
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = isolated_smoke_test(
        checkpoint,
        context_limit=128,
        runner=runner,
    )
    assert result["success"] is True


def test_worker_command_uses_project_private_python_runtime(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    observed: list[str] = []

    def runner(command, **_kwargs):
        observed.extend(command)
        atomic_write_json(
            _result_path(command),
            _completed_worker_payload(
                checkpoint,
                verification_attempt_id=_command_value(
                    command, "--verification-attempt-id", "standalone"
                ),
                operation_id=_command_value(command, "--operation-id", "standalone"),
                package_build_id=_command_value(
                    command, "--package-build-id", "development"
                ),
            ),
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    isolated_smoke_test(checkpoint, context_limit=128, runner=runner)
    project = Path(__file__).resolve().parents[1]
    expected = project / ".venv" / "Scripts" / "python.exe"
    assert Path(observed[0]).resolve() == expected.resolve()
    assert observed[1] == "-I"
    assert observed[2] == "-B"
    assert Path(observed[3]).resolve() == (
        project / "app" / "backend" / "worker_launcher.py"
    ).resolve()
    assert observed[4] == "app.backend.versions.checkpoint"


def test_private_worker_interpreter_cannot_be_the_desktop_host() -> None:
    project = Path(__file__).resolve().parents[1]
    virtual_environment, interpreter = _private_worker_interpreter(project)
    assert interpreter.parent.parent == virtual_environment
    assert interpreter.resolve() != (project / "Salty Steak.exe").resolve()


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows launch flags")
def test_windows_worker_launch_requests_no_console() -> None:
    import subprocess

    options = _windows_hidden_process_options()
    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW


def test_worker_result_checkpoint_identity_and_exit_code_must_agree(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    def runner(command, **_kwargs):
        payload = _completed_worker_payload(
            checkpoint,
            verification_attempt_id=_command_value(
                command, "--verification-attempt-id", "standalone"
            ),
            operation_id=_command_value(command, "--operation-id", "standalone"),
            package_build_id=_command_value(
                command, "--package-build-id", "development"
            ),
        )
        payload["checkpoint_path"] = str(tmp_path / "other")
        atomic_write_json(_result_path(command), payload)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(IsolatedWorkerError, match="result contract"):
        isolated_smoke_test(checkpoint, context_limit=128, runner=runner)


def test_non_json_stdout_is_preserved_and_reported_clearly(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="loading checkpoint\nhuman log only\n",
            stderr="",
        )

    with pytest.raises(
        IsolatedWorkerError,
        match="produced no valid result",
    ) as caught:
        isolated_smoke_test(
            checkpoint,
            context_limit=128,
            runner=runner,
        )
    assert caught.value.stdout.startswith("loading checkpoint")
    assert "FileNotFoundError" in str(caught.value.result_error)


def test_worker_stderr_is_preserved_separately(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    def runner(_command, **_kwargs):
        return SimpleNamespace(
            returncode=9,
            stdout="worker log",
            stderr="worker failure",
        )

    with pytest.raises(IsolatedWorkerError) as caught:
        isolated_smoke_test(
            checkpoint,
            context_limit=128,
            runner=runner,
        )
    assert caught.value.return_code == 9
    assert caught.value.stdout == "worker log"
    assert caught.value.stderr == "worker failure"


def test_partial_result_file_is_never_parsed(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    def runner(command, **_kwargs):
        result_path = _result_path(command)
        result_path.with_suffix(".json.tmp").write_text('{"ok":', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(IsolatedWorkerError, match="produced no valid result"):
        isolated_smoke_test(
            checkpoint,
            context_limit=128,
            runner=runner,
        )


def test_atomic_json_state_never_leaves_a_partial_file(tmp_path: Path) -> None:
    destination = tmp_path / "operation-state.json"
    atomic_write_json(destination, {"state": "running", "step": 9})
    atomic_write_json(destination, {"state": "failed", "step": 10})
    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "state": "failed",
        "step": 10,
    }
    assert not list(tmp_path.glob("*.tmp"))


def test_legacy_recovery_timestamps_normalise_at_the_backend_boundary() -> None:
    assert utc_timestamp(1_784_000_000) == "2026-07-14T03:33:20Z"
    assert utc_timestamp(1_784_000_000_000) == "2026-07-14T03:33:20Z"
    assert utc_timestamp(None) is None
    assert utc_timestamp(0) is None


def test_target_steps_do_not_complete_before_finalisation_and_metrics_survive(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "salty-potato.db")
    with OperationManager(database) as manager:
        def worker(context):
            context.update(
                phase="Training",
                current_progress=10,
                total_progress=10,
                details={
                    "current_training_step": 10,
                    "target_steps": 10,
                    "training_loss": 4.9875,
                    "learning_rate": 0.00003,
                    "tokens_per_second": 812.5,
                },
            )
            context.update(phase="Verifying version")
            raise OperationPhaseError(
                "Training steps finished, but the saved version could not be verified.",
                code="version_verification_failed",
                phase="Version verification failed",
                technical_details='{"stdout":"","stderr":"verification failed"}',
            )

        operation = manager.submit("training", worker)
        terminal = manager.wait(operation["id"])
    assert terminal["state"] == "failed"
    assert terminal["phase"] == "Version verification failed"
    assert terminal["current_progress"] == 10
    assert terminal["result"]["training_loss"] == 4.9875
    assert terminal["result"]["tokens_per_second"] == 812.5
    assert terminal["error"]["code"] == "version_verification_failed"


def _insert_training(database: Database) -> str:
    operation, _created = database.create_operation("training")
    now = utc_now()
    database.execute(
        """
        UPDATE operations SET state = 'failed', phase = 'Version registration failed',
            finished_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, now, operation["id"]),
    )
    database.execute(
        """
        INSERT INTO training_operations(
            id, operation_id, requested_steps, completed_steps,
            settings_json, created_at, updated_at
        ) VALUES (?, ?, 10, 10, ?, ?, ?)
        """,
        (new_id(), operation["id"], json_text({}), now, now),
    )
    return operation["id"]


def _manifest(operation_id: str) -> dict:
    return {
        "format": "salty-potato-saved-version-v2",
        "model_type": "salty_potato",
        "operation_id": operation_id,
        "prepared_dataset_id": None,
        "starting_version_id": None,
        "total_steps": 10,
        "additional_steps": 10,
        "weights_sha256": "b" * 64,
        "tokenizer_fingerprint": "a" * 64,
        "parameter_count": 1,
        "context_tokens": 128,
    }


def _tiny_config() -> ModelConfig:
    return ModelConfig(
        architecture_version=2,
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=128,
        use_gradient_checkpointing=False,
    )


def test_database_registration_failure_is_atomic_and_creates_no_duplicate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "salty-potato.db")
    operation_id = _insert_training(database)
    checkpoint = tmp_path / "versions" / str(uuid.uuid4())
    checkpoint.mkdir(parents=True)
    (checkpoint / "model.safetensors").write_bytes(b"complete")
    app = Application.__new__(Application)
    app.database = database
    results = tmp_path / "finalisation-results"
    app.finalisation = application_module.FinalisationCoordinator(database, results)
    attempt_id = new_id()
    result_path = results / operation_id / f"{attempt_id}.json"
    payload = _completed_worker_payload(
        checkpoint,
        verification_attempt_id=attempt_id,
        operation_id=operation_id,
    )
    atomic_write_json(result_path, payload)
    now = utc_now()
    database.execute(
        """
        INSERT INTO finalisation_attempts(
            id, operation_id, checkpoint_id, checkpoint_path, result_path,
            package_build_id, started_at, deadline_at, heartbeat_at, finished_at,
            phase, result_status
        ) VALUES (?, ?, ?, ?, ?, 'development', ?, ?, ?, ?, 'verified', 'passed')
        """,
        (
            attempt_id,
            operation_id,
            checkpoint.name,
            str(checkpoint.resolve()),
            str(result_path.resolve()),
            now,
            now,
            now,
            now,
        ),
    )
    database.execute(
        """
        CREATE TRIGGER reject_saved_version
        BEFORE INSERT ON saved_versions
        BEGIN SELECT RAISE(ABORT, 'registration rejected'); END
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="registration rejected"):
        app._register_verified_training_checkpoint(
            version_id=checkpoint.name,
            operation_id=operation_id,
            prepared_dataset_id=None,
            checkpoint_path=checkpoint,
            total_steps=10,
            additional_steps=10,
            config=_tiny_config(),
            manifest=_manifest(operation_id),
            weight_sha256="b" * 64,
            verification_attempt_id=attempt_id,
        )
    assert database.fetch_one("SELECT COUNT(*) AS count FROM saved_versions")["count"] == 0
    assert database.fetch_one(
        "SELECT result_version_id FROM training_operations WHERE operation_id = ?",
        (operation_id,),
    )["result_version_id"] is None


def test_unproved_orphan_never_registers_and_is_marked_needs_attention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "salty-potato.db")
    versions = tmp_path / "versions"
    versions.mkdir()
    valid_operation = _insert_training(database)
    valid = versions / str(uuid.uuid4())
    valid.mkdir()
    _tiny_config().to_file(valid / "config.json")
    (valid / "model.safetensors").write_bytes(b"complete")
    atomic_write_json(valid / "manifest.json", _manifest(valid_operation))

    partial_operation = _insert_training(database)
    partial = versions / str(uuid.uuid4())
    partial.mkdir()
    atomic_write_json(partial / "manifest.json", _manifest(partial_operation))

    app = Application.__new__(Application)
    app.database = database
    app.paths = SimpleNamespace(versions=versions)
    app.finalisation = application_module.FinalisationCoordinator(
        database, tmp_path / "finalisation-results"
    )

    app._reconcile_completed_checkpoints()
    app._reconcile_completed_checkpoints()

    rows = database.fetch_all("SELECT * FROM saved_versions")
    assert rows == []
    valid_record = database.get_operation(valid_operation)
    assert valid_record["state"] == "failed"
    assert valid_record["phase"] == "Finalisation requires attention"
    assert database.get_operation(partial_operation)["state"] == "failed"
    assert database.fetch_one(
        """
        SELECT COUNT(*) AS count FROM operation_events
        WHERE operation_id = ? AND event_type = 'finalisation_needs_attention'
        """,
        (valid_operation,),
    )["count"] == 1
