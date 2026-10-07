from __future__ import annotations

import sqlite3
import threading
import time
import tomllib
import uuid
from pathlib import Path

import pytest

from app.backend.database.control import SCHEMA_VERSION, Database
from app.backend.operations.manager import Notification, OperationManager
from app.backend.system.config import load_config
from app.backend.system.files import (
    atomic_write_json,
    create_storage_layout,
    sha256_file,
)
from app.backend.system.timestamps import utc_now_timestamp


def test_default_chat_maximum_output_is_2048() -> None:
    defaults_path = Path(__file__).parents[1] / "config" / "defaults.toml"
    with defaults_path.open("rb") as handle:
        defaults = tomllib.load(handle)
    assert defaults["generation"]["maximum_output_tokens"] == 2048


def test_schema_v2_additive_migration_precedes_current_triggers(tmp_path: Path) -> None:
    """A genuine v2 shape must open without losing legacy intent."""

    database_path = tmp_path / "legacy-v2.db"
    database = Database(database_path)
    operation_id = str(uuid.uuid4())
    training_id = str(uuid.uuid4())
    database.create_operation("training", operation_id=operation_id)
    with database.transaction() as connection:
        now = utc_now_timestamp()
        connection.execute(
            """
            INSERT INTO training_operations(
                id, operation_id, requested_steps, completed_steps,
                settings_json, created_at, updated_at
            ) VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (
                training_id,
                operation_id,
                10,
                '{"use_completed_version_in_chat": true}',
                now,
                now,
            ),
        )
        # Make the fixture genuinely pre-policy: remove the current index,
        # triggers, and columns before lowering its user_version.
        connection.execute("DROP INDEX IF EXISTS ix_training_post_training")
        trigger_names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        ]
        for trigger_name in trigger_names:
            connection.execute(
                f'DROP TRIGGER "{trigger_name.replace(chr(34), chr(34) * 2)}"'
            )
        for column in (
            "post_training_policy",
            "post_training_state",
            "post_training_recovery_state",
            "required_evaluation_id",
            "activation_operation_id",
            "activation_attempt_id",
            "confirmed_runtime_id",
            "post_training_error_json",
        ):
            connection.execute(
                f'ALTER TABLE training_operations DROP COLUMN "{column}"'
            )
        connection.execute("PRAGMA user_version = 2")

    migrated = Database(database_path)
    with migrated.connection(readonly=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = connection.execute(
            """
            SELECT post_training_policy, post_training_state,
                   post_training_recovery_state
            FROM training_operations WHERE id = ?
            """,
            (training_id,),
        ).fetchone()
        assert tuple(row) == (
            "evaluate_and_activate",
            "not_started",
            "legacy_unknown",
        )


def test_schema_v5_has_general_chat_runtime_provenance(tmp_path: Path) -> None:
    database = Database(tmp_path / "chat-runtime.db")
    with database.connection(readonly=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"chat_runtime_states", "active_chat_runtime"} <= tables
        message_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(messages)")
        }
        assert {
            "target_kind",
            "target_id",
            "runtime_profile_id",
            "runtime_instance_id",
            "source_sha256",
        } <= message_columns


def test_schema_v6_connector_storage_migrates_to_dpapi_value(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-v6.db"
    database = Database(database_path)
    with database.transaction() as connection:
        connection.execute(
            "DROP TRIGGER IF EXISTS trg_plugin_connectors_immutable_identity"
        )
        connection.execute("DROP INDEX IF EXISTS ix_plugin_connectors_status")
        connection.execute("DROP TABLE plugin_connectors")
        connection.execute(
            """
            CREATE TABLE plugin_connectors (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL CHECK(provider IN (
                    'gmail','google_calendar','icloud_calendar','mcp'
                )),
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'disconnected' CHECK(status IN (
                    'disconnected','configured','connected','degraded','error','disabled'
                )),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                transport TEXT NOT NULL,
                permission_scopes_json TEXT NOT NULL DEFAULT '[]',
                granted_scopes_json TEXT NOT NULL DEFAULT '[]',
                configuration_json TEXT NOT NULL DEFAULT '{}',
                credential_storage TEXT NOT NULL DEFAULT 'none' CHECK(
                    credential_storage IN (
                        'none','windows_credential_manager','environment'
                    )
                ),
                credential_reference TEXT,
                last_error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO plugin_connectors(
                id, provider, display_name, transport,
                credential_storage, created_at, updated_at
            ) VALUES (
                'mcp', 'mcp', 'Model Context Protocol', 'mcp',
                'environment', '2026-08-11T00:00:00.000Z',
                '2026-08-11T00:00:00.000Z'
            )
            """
        )
        connection.execute("PRAGMA user_version = 6")

    migrated = Database(database_path)
    with migrated.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            SCHEMA_VERSION
        )
        connection.execute(
            """
            UPDATE plugin_connectors
            SET credential_storage = 'dpapi_protected_file'
            WHERE id = 'mcp'
            """
        )
        assert connection.execute(
            "SELECT credential_storage FROM plugin_connectors WHERE id = 'mcp'"
        ).fetchone()[0] == "dpapi_protected_file"


def _wait_for_state(
    manager: OperationManager,
    operation_id: str,
    state: str,
    timeout: float = 2,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(operation_id)
        if record and record["state"] == state:
            return record
        time.sleep(0.01)
    raise AssertionError(f"operation did not reach {state}")


def test_config_merges_local_and_resolves_exact_paths(tmp_path: Path) -> None:
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    defaults = config_directory / "defaults.toml"
    defaults.write_text(
        """
[paths]
workspace = "workspace"
database = "workspace/control/salty-potato.db"
datasets = "workspace/datasets"
prepared = "workspace/datasets/prepared"
tokenizer = "workspace/tokenizer"
versions = "workspace/versions"
training = "workspace/training"
evaluations = "workspace/evaluations"
runtime = "workspace/runtime"
conversations = "workspace/conversations"
cache = "workspace/cache"
logs = "workspace/logs"

[datasets]
training_split = 0.9
validation_split = 0.1

[training]
sequence_length = 512
device = "cpu"

[model]
architectural_context_tokens = 8192

[generation]
conversation_token_budget = 7680
reserved_output_tokens = 512
maximum_output_tokens = 256

[versions]
completed_retention = 2
recovery_retention = 1
""",
        encoding="utf-8",
    )
    local = config_directory / "local.toml"
    local.write_text(
        """
[training]
device = "cuda"

[paths]
cache = "machine/cache"
""",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.section("training")["device"] == "cuda"
    assert config.paths.database == (
        tmp_path / "workspace" / "control" / "salty-potato.db"
    ).resolve()
    assert config.paths.cache == (tmp_path / "machine" / "cache").resolve()
    create_storage_layout(config)
    assert config.paths.database.parent.is_dir()
    assert config.paths.prepared.is_dir()


def test_atomic_json_and_checksum(tmp_path: Path) -> None:
    path = tmp_path / "state" / "record.json"
    atomic_write_json(path, {"name": "Salty Steak", "ready": True})
    first_checksum = sha256_file(path)
    atomic_write_json(path, {"name": "Salty Steak", "ready": False})
    assert sha256_file(path) != first_checksum
    assert not list(path.parent.glob("*.tmp"))


def test_fresh_schema_has_all_control_records_and_foreign_keys(tmp_path: Path) -> None:
    database = Database(tmp_path / "control" / "salty-potato.db")
    tables = {
        row["name"]
        for row in database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "datasets",
        "prepared_datasets",
        "training_operations",
        "saved_versions",
        "evaluations",
        "runtime_states",
        "active_runtime",
        "conversations",
        "messages",
        "operations",
        "notifications",
        "configuration_references",
    } <= tables
    assert database.foreign_key_violations() == []

    now = "2026-01-01T00:00:00.000Z"
    with pytest.raises(sqlite3.IntegrityError):
        database.execute(
            """
            INSERT INTO prepared_datasets(
                id, dataset_id, source_checksum, path, settings_json,
                record_count, token_count, train_record_count,
                validation_record_count, artifact_checksum, verified,
                status, created_at
            ) VALUES (?, ?, ?, ?, '{}', 0, 0, 0, 0, ?, 0, 'preparing', ?)
            """,
            (
                "71943a91-41ce-43ac-8d68-bcd9ad80690a",
                "7d210e30-7aec-4c59-9ac1-54671fe82399",
                "a" * 64,
                "missing",
                "b" * 64,
                now,
            ),
        )


def test_operation_dedup_stop_and_restart_reconciliation(tmp_path: Path) -> None:
    database = Database(tmp_path / "salty-potato.db")
    started = threading.Event()
    release = threading.Event()

    with OperationManager(database, max_workers=1) as manager:
        def worker(context):
            started.set()
            release.wait(2)
            context.checkpoint(phase="Safe boundary", current_progress=1, total_progress=2)
            return {"should_not": "complete"}

        first = manager.submit(
            "training",
            worker,
            target_id="target",
            dedupe_key="same-target",
        )
        assert started.wait(1)
        duplicate = manager.submit(
            "training",
            worker,
            target_id="target",
            dedupe_key="same-target",
        )
        assert duplicate["id"] == first["id"]
        stopped = manager.request_stop(first["id"])
        assert stopped["state"] == "stop_requested"
        assert manager.request_stop(first["id"])["state"] == "stop_requested"
        release.set()
        final = manager.wait(first["id"])
        assert final["state"] == "interrupted"
        assert final["worker_pid"] is not None
        assert final["error"]["code"] == "stop_requested"

        abandoned = manager.create("evaluation", target_id="version")
        assert abandoned["state"] == "queued"

    with OperationManager(database, recover_incomplete=True) as restarted:
        recovered = restarted.get(abandoned["id"])
        assert recovered is not None
        assert recovered["state"] == "interrupted"
        assert recovered["error"]["code"] == "application_restarted"


def test_completion_notification_is_committed_once(tmp_path: Path) -> None:
    database = Database(tmp_path / "salty-potato.db")
    with OperationManager(database) as manager:
        operation = manager.submit(
            "dataset_validation",
            lambda context: {"valid": True},
            target_id="dataset",
            success_notification=Notification(
                "success", "Validated", "Validation completed."
            ),
        )
        completed = manager.wait(operation["id"])
        assert completed["state"] == "completed"
        assert completed["result"] == {"valid": True}
        manager.complete(
            operation["id"],
            result={"valid": True},
            notification=Notification("success", "Validated", "Validation completed."),
        )

    notifications = database.fetch_all(
        "SELECT * FROM notifications WHERE operation_id = ?",
        (operation["id"],),
    )
    assert len(notifications) == 1


def test_failed_training_releases_operation_lock_and_evaluation_can_start(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "salty-potato.db")
    with OperationManager(database, max_workers=1) as manager:
        failed = manager.submit(
            "training",
            lambda _context: (_ for _ in ()).throw(
                RuntimeError("failed before step 1")
            ),
            target_id="prepared-one",
            dedupe_key="training:single-active",
        )
        failed = manager.wait(failed["id"])
        assert failed["state"] == "failed"

        replacement = manager.submit(
            "training",
            lambda _context: {"ok": True},
            target_id="prepared-one",
            dedupe_key="training:single-active",
        )
        assert replacement["id"] != failed["id"]
        assert manager.wait(replacement["id"])["state"] == "completed"

        evaluation = manager.submit(
            "evaluation",
            lambda _context: {"records": 1},
            target_id="version-one",
            dedupe_key="evaluation:version-one",
            initial_phase="Queued",
        )
        assert manager.wait(evaluation["id"])["state"] == "completed"


def test_missing_evaluation_worker_fails_and_duplicate_reconnects(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "salty-potato.db")
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    with OperationManager(
        database,
        max_workers=1,
        worker_start_timeout_seconds=0.05,
    ) as manager:
        blocker = manager.submit(
            "dataset_validation",
            lambda _context: (
                blocker_started.set(),
                release_blocker.wait(1),
                {"ok": True},
            )[-1],
            target_id="dataset",
        )
        assert blocker_started.wait(1)
        first = manager.submit(
            "evaluation",
            lambda _context: {"records": 1},
            target_id="version-one",
            dedupe_key="evaluation:version-one",
            initial_phase="Queued",
        )
        duplicate = manager.submit(
            "evaluation",
            lambda _context: {"records": 2},
            target_id="version-one",
            dedupe_key="evaluation:version-one",
            initial_phase="Queued",
        )
        assert duplicate["id"] == first["id"]
        assert first["result"]["queue_reason"] == "Waiting for an available local worker."
        terminal = manager.wait(first["id"], timeout=1)
        assert terminal["state"] == "failed"
        assert terminal["error"]["code"] == "worker_not_started"
        release_blocker.set()
        assert manager.wait(blocker["id"])["state"] == "completed"


def test_only_web_addresses_can_be_opened_externally(tmp_path) -> None:
    # Clicking a source link is the user's own action, so it needs no
    # automation grant — which makes it all the more important that nothing
    # here can be talked into launching a file, a UNC path or a custom scheme.
    from app.backend.application import Application

    opened: list[str] = []
    application = Application.__new__(Application)

    def fake_startfile(address):
        opened.append(address)

    import app.backend.application as module

    original = getattr(module.os, "startfile", None)
    module.os.startfile = fake_startfile  # type: ignore[attr-defined]
    try:
        result = application.open_external({"url": "https://example.com/a"})
        assert result == {"opened": True, "url": "https://example.com/a"}
        assert opened == ["https://example.com/a"]

        for refused in (
            "file:///C:/Windows/System32/cmd.exe",
            r"\\server\share",
            "javascript:alert(1)",
            "ms-settings:privacy",
            "https://example.com/a\nhttps://evil.example",
            "",
        ):
            with pytest.raises(ValueError):
                application.open_external({"url": refused})
        # Nothing beyond the one good address was ever launched.
        assert opened == ["https://example.com/a"]
    finally:
        if original is not None:
            module.os.startfile = original  # type: ignore[attr-defined]
