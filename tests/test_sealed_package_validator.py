from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from app.backend.database.control import Database
from tools.validate_sealed_package import audit_disposable_v7_migration


PROJECT_ROOT = Path(__file__).parents[1]


def _make_v7_fixture(path: Path) -> None:
    database = Database(path)
    conversation_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO conversations(id, title, created_at, updated_at)
            VALUES (?, 'Preserved conversation', ?, ?)
            """,
            (
                conversation_id,
                "2026-08-12T00:00:00.000Z",
                "2026-08-12T00:00:00.000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, sequence, created_at
            ) VALUES (?, ?, 'user', 'preserve this exact record', 0, ?)
            """,
            (message_id, conversation_id, "2026-08-12T00:00:01.000Z"),
        )
        connection.execute("DROP TABLE automation_audit_records")
        connection.execute("DROP TABLE automation_grants")
        connection.execute(
            "UPDATE application_metadata SET value = '7' "
            "WHERE key = 'schema_version'"
        )
        connection.execute("PRAGMA user_version = 7")


def test_package_validator_exercises_v7_to_v9_and_v9_idempotence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-v7.db"
    _make_v7_fixture(source)
    neutral = tmp_path / "neutral"
    neutral.mkdir()

    result, issues = audit_disposable_v7_migration(
        PROJECT_ROOT,
        source,
        neutral_cwd=neutral,
    )

    assert issues == []
    assert result["valid"] is True
    assert result["schema_contract"] == {
        "required_input": [7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
        "required_output": 17,
        "idempotent_version": 17,
    }
    assert result["before_version"] == 7
    assert result["after_version"] == 17
    assert result["idempotent_version"] == 17
    assert result["schema_metadata_value"] == "17"
    assert result["changed_legacy_counts"] == {}
    assert result["changed_legacy_records"] == {}
    assert result["added_rows"] == {}
    assert result["protected_records_preserved"] is True
    assert result["idempotent_count_changes"] == {}
    assert result["idempotent_record_changes"] == {}
    assert result["idempotent_records_preserved"] is True
    assert result["before_health"]["integrity_ok"] is True
    assert result["after_health"]["integrity_ok"] is True
    assert result["idempotent_health"]["integrity_ok"] is True
    assert result["before_health"]["foreign_keys_clean"] is True
    assert result["after_health"]["foreign_keys_clean"] is True
    assert result["idempotent_health"]["foreign_keys_clean"] is True

    migrated = sqlite3.connect(Path(result["copy"]))
    try:
        assert migrated.execute(
            "SELECT content FROM messages WHERE role = 'user'"
        ).fetchone()[0] == "preserve this exact record"
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"automation_grants", "automation_audit_records"} <= tables
    finally:
        migrated.close()


def test_package_validator_exercises_live_v8_to_v9_migration(tmp_path: Path) -> None:
    source = tmp_path / "source-v8.db"
    database = Database(source)
    with database.transaction() as connection:
        connection.execute("DROP TABLE chat_artifacts")
        connection.execute(
            "UPDATE application_metadata SET value = '8' WHERE key = 'schema_version'"
        )
        connection.execute("PRAGMA user_version = 8")
    neutral = tmp_path / "neutral"
    neutral.mkdir()

    result, issues = audit_disposable_v7_migration(
        PROJECT_ROOT, source, neutral_cwd=neutral
    )

    assert issues == []
    assert result["valid"] is True
    assert result["before_version"] == 8
    assert result["after_version"] == 17
    migrated = sqlite3.connect(Path(result["copy"]))
    try:
        assert migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_artifacts'"
        ).fetchone() == ("chat_artifacts",)
    finally:
        migrated.close()


def test_package_validator_rejects_a_v9_only_input_as_migration_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-v9.db"
    Database(source)
    neutral = tmp_path / "neutral"
    neutral.mkdir()

    result, issues = audit_disposable_v7_migration(
        PROJECT_ROOT,
        source,
        neutral_cwd=neutral,
    )

    assert result["valid"] is False
    assert any(
        "required 7-or-8-or-9-or-10-or-11-or-12-or-13-or-14-or-15-or-16->17" in issue
        for issue in issues
    )
