from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.backend.application as application_module
from app.backend.application import Application
from app.backend.database.control import utc_now
from app.backend.operations.manager import OperationInterrupted


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


def _insert_version(
    application: Application,
    *,
    version_id: str,
    label: str,
    total_steps: int | None,
) -> Path:
    path = application.paths.versions / version_id
    path.mkdir(parents=True)
    (path / "model.safetensors").write_bytes(version_id.encode("ascii"))
    now = utc_now()
    application.database.execute(
        """
        INSERT INTO saved_versions(
            id, label, checkpoint_path, checksum, size_bytes,
            total_trained_steps, additional_steps, architecture_revision,
            context_tokens, tokenizer_checksum, integrity, imported,
            created_at, verified_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 2, 8192, ?, 'verified', 1, ?, ?)
        """,
        (
            version_id,
            label,
            str(path.resolve()),
            version_id[0] * 64,
            (path / "model.safetensors").stat().st_size,
            total_steps,
            "f" * 64,
            now,
            now,
        ),
    )
    return path


def test_opening_pages_and_restart_start_no_heavy_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    application = Application(project, recover_operations=False)
    try:
        assert application.list_datasets() == []
        assert application.list_versions() == []
        assert application.list_evaluations() == []
        assert application.chat_status()["readiness"] == "no_saved_version_selected"
        assert application.training_setup()["resume_state"] is None
        monkeypatch.setattr(
            application_module,
            "_directory_size",
            lambda *_args, **_kwargs: pytest.fail(
                "project page performed a recursive size scan"
            ),
        )
        assert application.project_state()["integrity_status"] == "not_verified"
        assert application.operations.list() == []
    finally:
        application.close()

    restarted = Application(project, recover_operations=True)
    try:
        assert restarted.operations.list() == []
        assert restarted.list_versions() == []
    finally:
        restarted.close()


def test_application_automation_scope_tracks_workspace_owner(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    application = Application(project, recover_operations=False)
    try:
        status = application.automation_status()
        terminal = next(
            item
            for item in status["capabilities"]
            if item["capability"] == "terminal.execute"
        )
        assert application.paths.workspace.parent == project.resolve()
        assert terminal["constraints"]["working_directory_root"] == str(
            project.resolve()
        )
        assert terminal["granted"] is False
        assert terminal["effective_enabled"] is False
    finally:
        application.close()


def test_model_bundle_chat_persists_bundle_runtime_provenance(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    source_sha256 = "8" * 64
    runtime_id = "91000000-0000-4000-8000-000000000001"

    class FakeBundleRuntime:
        profile = SimpleNamespace(profile_id="balanced_quality", context_limit=32768)
        loaded = True

        def describe(self) -> dict:
            return {
                "loaded": self.loaded,
                "runtime_id": runtime_id if self.loaded else None,
                "configured_context_limit": 32768,
                "source_sha256": source_sha256,
                "model_path": str(tmp_path / "Base-Steak-2.0-9B-Q5_K_M.gguf"),
            }

        def load(self) -> dict:
            self.loaded = True
            return self.describe()

        def warmup(self) -> dict:
            self.loaded = True
            return self.describe()

        def unload(self) -> None:
            self.loaded = False

        @staticmethod
        def generate(**kwargs):
            preview = kwargs.get("on_preview")
            if preview is not None:
                preview(
                    {
                        "kind": "reasoning",
                        "tail_text": "bounded trace",
                        "token_count": 4,
                    }
                )
            return SimpleNamespace(
                cancelled=False,
                text="native answer",
                token_ids=[1, 2],
                omitted_turns=0,
                finish_reason="end_of_generation",
                technical_details={
                    "finish_reason": "end_of_generation",
                    "decode_tokens_per_second": 1.9,
                },
            )

    fake_runtime = FakeBundleRuntime()
    application.model_bundle_runtime = fake_runtime  # type: ignore[assignment]
    application.chat.model_bundle_runtime = fake_runtime  # type: ignore[assignment]
    application.chat.model_bundle = {
        "id": "general-steak-27b",
        "display_name": "Salty Steak 27B",
        "checksum": source_sha256,
        "quantization": "Q4_K_M",
    }
    try:
        conversation = application.create_conversation()
        operation = application.chat.start_message(conversation["id"], "hello")
        completed = application.operations.wait(operation["id"], timeout=10)
        assert completed["state"] == "completed"
        assert completed["result"]["generation_preview"] == {
            "state": "discarded",
            "token_count": 2,
        }
        assert "bounded trace" not in str(completed["result"])
        second_operation = application.chat.start_message(conversation["id"], "again")
        second_completed = application.operations.wait(second_operation["id"], timeout=10)
        assert second_completed["state"] == "completed"

        messages = application.get_conversation(conversation["id"])["messages"]
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assistant = messages[-1]
        assert assistant["content"] == "native answer"
        assert assistant["target_kind"] == "model_bundle"
        assert assistant["target_id"] == "general-steak-27b"
        assert assistant["runtime_profile_id"] == "balanced_quality"
        assert assistant["runtime_instance_id"] == runtime_id
        assert assistant["source_sha256"] == source_sha256
        assert assistant["saved_version_id"] is None
        assert assistant["runtime_id"] is None
        assert assistant["technical_details"]["reasoning_mode_requested"] == "instant"
        assert assistant["technical_details"]["reasoning_mode_effective"] == "instant"
        assert assistant["technical_details"]["context_window_tokens_requested"] == 6144
        assert assistant["technical_details"]["context_window_tokens_effective"] == 6144
        assert assistant["technical_details"]["maximum_output_mode_effective"] == "automatic"
        assert assistant["technical_details"]["maximum_output_tokens_effective"] == 3072

        active = application.database.fetch_one(
            "SELECT * FROM active_chat_runtime WHERE singleton = 1"
        )
        assert active is not None
        assert active["runtime_id"] == runtime_id
        assert active["target_kind"] == "model_bundle"
        assert active["target_id"] == "general-steak-27b"
        states = application.database.fetch_all(
            "SELECT * FROM chat_runtime_states WHERE id = ?", (runtime_id,)
        )
        assert len(states) == 1
    finally:
        application.close()
    state = application.database.fetch_one(
        "SELECT state, unloaded_at FROM chat_runtime_states WHERE id = ?", (runtime_id,)
    )
    assert state is not None
    assert state["state"] == "unloaded"
    assert state["unloaded_at"] is not None


def test_failed_chat_generation_keeps_the_exact_user_turn_and_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    application = Application(project, recover_operations=False)

    class TargetRuntime:
        profile = SimpleNamespace(profile_id="failure-test", context_limit=32768)

        @staticmethod
        def describe() -> dict:
            return {"runtime_id": None}

        @staticmethod
        def unload() -> None:
            return None

    runtime = TargetRuntime()
    application.model_bundle_runtime = runtime  # type: ignore[assignment]
    application.chat.model_bundle_runtime = runtime  # type: ignore[assignment]
    application.chat.model_bundle = {
        "id": "base-steak-2-0-failure-test",
        "display_name": "Base Steak 2.0",
        "checksum": "7" * 64,
    }

    def fail_generation(**_kwargs):
        raise RuntimeError("deterministic generation failure")

    monkeypatch.setattr(application.chat, "_generate_turn", fail_generation)
    try:
        conversation = application.create_conversation()
        exact = "what is your name"
        operation = application.chat.start_message(conversation["id"], exact)
        completed = application.operations.wait(operation["id"], timeout=10)

        assert completed["state"] == "failed"
        reloaded = application.get_conversation(conversation["id"])
        assert reloaded["title"] == exact
        assert len(reloaded["messages"]) == 1
        user = reloaded["messages"][0]
        assert user["role"] == "user"
        assert user["content"] == exact
        assert user["technical_details"]["generation_state"] == "failed"
        assert user["technical_details"]["cancellation_state"] == "not_requested"
        assert user["technical_details"]["generation_error"] == {
            "type": "RuntimeError",
            "message": "deterministic generation failure",
        }
    finally:
        application.close()

    # This is the lifecycle regression that the installed r88 build violated:
    # a failed turn must still be present after the process and database are
    # reopened, so selecting its sidebar title cannot produce a blank chat.
    restarted = Application(project, recover_operations=False)
    try:
        restored = restarted.get_conversation(conversation["id"])
        assert restored["title"] == exact
        assert [(message["role"], message["content"]) for message in restored["messages"]] == [
            ("user", exact)
        ]
        assert restored["messages"][0]["technical_details"]["generation_state"] == (
            "failed"
        )
    finally:
        restarted.close()


def test_about_reports_truthful_missing_values_and_cached_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    executable = project / "candidate.exe"
    executable.write_bytes(b"native-candidate")
    monkeypatch.setenv("SALTY_POTATO_EXECUTABLE_PATH", str(executable))
    monkeypatch.setenv("SALTY_POTATO_BUILD_ID", "2.0.0+test.native-r2")
    application = Application(project, recover_operations=False)
    try:
        monkeypatch.setattr(
            application_module.subprocess,
            "run",
            lambda *_args, **_kwargs: pytest.fail(
                "About page launched an external hardware probe"
            ),
        )
        about = application.about_state()
        assert about["application"]["build_id"] == "2.0.0+test.native-r2"
        assert about["package"]["executable_path"] == str(executable.resolve())
        assert about["package"]["executable_sha256"] is None
        assert about["model"] is None
        assert application.operations.list() == []

        first = application.about_storage()
        second = application.about_storage()
        assert second == first
        assert first["workspace_total_bytes"] >= 0
        assert first["current_package_bytes"] >= executable.stat().st_size
        assert first["rollback_package_bytes"] is None
    finally:
        application.close()


def test_real_deletion_has_no_ancestry_rule_or_tombstone_and_preserves_other(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        first_path = _insert_version(
            application,
            version_id="11111111-1111-4111-8111-111111111111",
            label="Salty Steak at 10 total steps",
            total_steps=10,
        )
        second_path = _insert_version(
            application,
            version_id="22222222-2222-4222-8222-222222222222",
            label="Salty Steak at 20 total steps",
            total_steps=20,
        )
        preview = application.deletion_preview(
            "11111111-1111-4111-8111-111111111111"
        )
        assert preview["allowed"]
        operation = application.delete_version(
            "11111111-1111-4111-8111-111111111111",
            "delete-first-once",
        )
        completed = application.operations.wait(operation["id"], timeout=5)
        assert completed["state"] == "completed", completed
        assert not first_path.exists()
        assert application.database.fetch_one(
            "SELECT * FROM saved_versions WHERE id = ?",
            ("11111111-1111-4111-8111-111111111111",),
        ) is None
        remaining = application.get_version(
            "22222222-2222-4222-8222-222222222222"
        )
        assert second_path.is_dir()
        assert remaining["label"] == "Salty Steak at 20 total steps"
        assert remaining["total_trained_steps"] == 20
        assert application.deletion_preview(remaining["id"])["allowed"] is False
        journal = application.database.fetch_one(
            """
            SELECT state, expected_bytes, actual_bytes
            FROM retention_journal
            WHERE saved_version_id = ?
            """,
            ("11111111-1111-4111-8111-111111111111",),
        )
        assert journal is None
    finally:
        application.close()


def test_version_files_are_restored_when_database_deletion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        version_id = "99999999-9999-4999-8999-999999999991"
        retained_id = "99999999-9999-4999-8999-999999999992"
        version_path = _insert_version(
            application,
            version_id=version_id,
            label="Deletion rollback candidate",
            total_steps=10,
        )
        _insert_version(
            application,
            version_id=retained_id,
            label="Retained version",
            total_steps=20,
        )
        original_transaction = application.database.transaction

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, parameters=()):
                if "DELETE FROM saved_versions" in statement:
                    raise sqlite3.OperationalError("injected commit failure")
                return self.connection.execute(statement, parameters)

        @contextmanager
        def failing_transaction():
            with original_transaction() as connection:
                yield ConnectionProxy(connection)

        monkeypatch.setattr(
            application.database,
            "transaction",
            failing_transaction,
        )
        operation = application.delete_version(version_id, "rollback-delete")
        completed = application.operations.wait(operation["id"], timeout=5)

        assert completed["state"] == "failed"
        assert version_path.is_dir()
        assert (version_path / "model.safetensors").is_file()
        assert application.database.fetch_one(
            "SELECT id FROM saved_versions WHERE id = ?", (version_id,)
        )
        assert not list(application.paths.cache.glob("delete-*"))
    finally:
        application.close()


def test_total_trained_steps_are_database_immutable(tmp_path: Path) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        _insert_version(
            application,
            version_id="33333333-3333-4333-8333-333333333333",
            label="Salty Steak imported version 1",
            total_steps=None,
        )
        with pytest.raises(sqlite3.IntegrityError):
            application.database.execute(
                """
                UPDATE saved_versions SET total_trained_steps = 999
                WHERE id = ?
                """,
                ("33333333-3333-4333-8333-333333333333",),
            )
    finally:
        application.close()


def test_conversation_rename_and_hard_delete_cascades_messages(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        conversation = application.create_conversation()
        application.database.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, sequence, created_at
            ) VALUES (?, ?, 'user', 'hello', 0, ?)
            """,
            (
                "44444444-4444-4444-8444-444444444444",
                conversation["id"],
                utc_now(),
            ),
        )

        renamed = application.rename_conversation(
            conversation["id"], "  Research notes  "
        )
        assert renamed["title"] == "Research notes"
        assert renamed["messages"][0]["content"] == "hello"

        with pytest.raises(ValueError, match="cannot be empty"):
            application.rename_conversation(conversation["id"], "   ")
        with pytest.raises(ValueError, match="cannot exceed"):
            application.rename_conversation(conversation["id"], "x" * 81)
        with pytest.raises(ValueError, match="one line"):
            application.rename_conversation(conversation["id"], "First\nSecond")
        with pytest.raises(KeyError, match="does not exist"):
            application.rename_conversation("missing", "Title")

        result = application.delete_conversation(conversation["id"])
        assert result == {
            "deleted": True,
            "conversation_id": conversation["id"],
            "title": "Research notes",
        }
        assert application.database.fetch_one(
            "SELECT id FROM conversations WHERE id = ?", (conversation["id"],)
        ) is None
        assert application.database.fetch_one(
            "SELECT id FROM messages WHERE conversation_id = ?", (conversation["id"],)
        ) is None
        with pytest.raises(KeyError, match="does not exist"):
            application.delete_conversation(conversation["id"])
    finally:
        application.close()


def test_chat_retry_targets_existing_latest_turn_without_duplicating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        conversation = application.create_conversation()
        now = utc_now()
        rows = [
            ("50000000-0000-4000-8000-000000000001", "user", "first", 0, None),
            ("50000000-0000-4000-8000-000000000002", "assistant", "first reply", 1, "{}"),
            ("50000000-0000-4000-8000-000000000003", "user", "latest", 2, None),
            ("50000000-0000-4000-8000-000000000004", "assistant", "latest reply", 3, "{}"),
        ]
        for message_id, role, content, sequence, details in rows:
            application.database.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    technical_details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation["id"],
                    role,
                    content,
                    sequence,
                    details,
                    now,
                ),
            )

        captured: dict[str, object] = {}

        def submit(operation_type, worker, **kwargs):
            captured.update(
                {
                    "operation_type": operation_type,
                    "worker": worker,
                    "kwargs": kwargs,
                }
            )
            return {"id": "50000000-0000-4000-8000-000000000005"}

        monkeypatch.setattr(application.chat.operations, "submit", submit)
        before = application.get_conversation(conversation["id"])["messages"]
        operation = application.retry_message(
            conversation["id"],
            "50000000-0000-4000-8000-000000000003",
        )
        after = application.get_conversation(conversation["id"])["messages"]

        assert operation["id"] == "50000000-0000-4000-8000-000000000005"
        assert captured["operation_type"] == "chat_generation"
        assert captured["kwargs"]["dedupe_key"] == (
            "chat-generation:global"
        )
        assert [message["id"] for message in after] == [
            message["id"] for message in before
        ]
        # Ordinary turns persist a host-action audit record even when no action
        # was requested. Its presence alone must not disable model retries.
        application.database.execute(
            "UPDATE messages SET technical_details_json=? WHERE id=?",
            ('{"host_action":{"state":"not_requested","intent":null,"execution_requested":false,"execution_performed":false}}',
             "50000000-0000-4000-8000-000000000003"),
        )
        assert application.retry_message(
            conversation["id"], "50000000-0000-4000-8000-000000000003"
        )["id"] == operation["id"]
        application.database.execute(
            "UPDATE messages SET technical_details_json=? WHERE id=?",
            ('{"host_action":{"state":"requested","intent":"filesystem.trash_file","execution_requested":true}}',
             "50000000-0000-4000-8000-000000000003"),
        )
        with pytest.raises(ValueError, match="Computer actions cannot be retried"):
            application.retry_message(conversation["id"], "50000000-0000-4000-8000-000000000003")
        with pytest.raises(ValueError, match="latest user turn"):
            application.chat.start_retry(
                conversation["id"],
                "50000000-0000-4000-8000-000000000001",
            )
    finally:
        application.close()


def test_chat_new_request_and_delete_cancel_global_generation_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        first = application.create_conversation()
        second = application.create_conversation()
        started = threading.Event()

        def active_worker(context):
            started.set()
            while not context.stop_requested():
                time.sleep(0.005)
            raise OperationInterrupted("expected cancellation")

        active = application.operations.submit(
            "chat_generation",
            active_worker,
            target_id=first["id"],
            dedupe_key="chat-generation:global",
        )
        assert started.wait(2)
        captured: dict[str, object] = {}

        def replacement(conversation_id, worker, *, generation_settings=None):
            captured["conversation_id"] = conversation_id
            captured["worker"] = worker
            captured["generation_settings"] = generation_settings
            return {"id": "70000000-0000-4000-8000-000000000001"}

        monkeypatch.setattr(
            application.chat, "_submit_generation", replacement
        )
        replacement_operation = application.chat.start_message(
            second["id"], "new owner"
        )
        assert replacement_operation["id"].endswith("0001")
        assert captured["conversation_id"] == second["id"]
        assert captured["generation_settings"]["reasoning_mode"] == "instant"
        assert application.operations.wait(active["id"], timeout=2)[
            "state"
        ] == "interrupted"

        delete_started = threading.Event()

        def delete_worker(context):
            delete_started.set()
            while not context.stop_requested():
                time.sleep(0.005)
            raise OperationInterrupted("expected delete cancellation")

        deleting = application.operations.submit(
            "chat_generation",
            delete_worker,
            target_id=first["id"],
            dedupe_key="chat-generation:global",
        )
        assert delete_started.wait(2)
        deleted = application.delete_conversation(first["id"])
        assert deleted["deleted"] is True
        assert application.operations.wait(deleting["id"], timeout=2)[
            "state"
        ] == "interrupted"
        assert application.database.fetch_one(
            "SELECT 1 FROM conversations WHERE id = ?", (first["id"],)
        ) is None
    finally:
        application.close()


def test_chat_retry_failure_preserves_previous_response(tmp_path: Path) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        conversation = application.create_conversation()
        now = utc_now()
        application.database.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, sequence, created_at
            ) VALUES (?, ?, 'user', 'try this', 0, ?)
            """,
            (
                "60000000-0000-4000-8000-000000000001",
                conversation["id"],
                now,
            ),
        )
        application.database.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, sequence,
                technical_details_json, created_at
            ) VALUES (?, ?, 'assistant', 'kept response', 1, '{}', ?)
            """,
            (
                "60000000-0000-4000-8000-000000000002",
                conversation["id"],
                now,
            ),
        )

        class Context:
            operation_id = "60000000-0000-4000-8000-000000000003"

        with pytest.raises(ValueError, match="No saved version selected"):
            application.chat._generate_retry(
                conversation["id"],
                "60000000-0000-4000-8000-000000000001",
                "60000000-0000-4000-8000-000000000002",
                Context(),
                "60000000-0000-4000-8000-000000000004",
            )
        messages = application.get_conversation(conversation["id"])["messages"]
        assert [(message["role"], message["content"]) for message in messages] == [
            ("user", "try this"),
            ("assistant", "kept response"),
        ]
    finally:
        application.close()


def test_cancelled_generation_discards_uncommitted_assistant_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        conversation = application.create_conversation()
        user_message_id = "80000000-0000-4000-8000-000000000001"
        now = utc_now()
        application.database.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, sequence, created_at
            ) VALUES (?, ?, 'user', 'cancel this', 0, ?)
            """,
            (user_message_id, conversation["id"], now),
        )
        identity = SimpleNamespace(
            checkpoint_id="80000000-0000-4000-8000-000000000002",
            runtime_id="80000000-0000-4000-8000-000000000003",
        )
        application.runtime._loaded = SimpleNamespace(
            identity=identity,
            model=None,
            tokenizer=None,
        )
        monkeypatch.setattr(application.chat, "_ensure_runtime", lambda _id: None)
        monkeypatch.setattr(
            application.runtime,
            "generate",
            lambda **_kwargs: SimpleNamespace(
                cancelled=True,
                text="must never be committed",
                omitted_turns=0,
                technical_details={"finish_reason": "cancelled"},
            ),
        )

        class Context:
            operation_id = "80000000-0000-4000-8000-000000000004"

            @staticmethod
            def update(**_kwargs):
                return None

            @staticmethod
            def stop_requested() -> bool:
                return False

            @staticmethod
            def raise_if_stop_requested() -> None:
                return None

        with pytest.raises(OperationInterrupted, match="discarded"):
            application.chat._generate_turn(
                conversation_id=conversation["id"],
                user_message_id=user_message_id,
                history=[{"role": "user", "content": "cancel this"}],
                assistant_sequence=1,
                active_version_id=identity.checkpoint_id,
                context=Context(),
                cancellation_token=(
                    "80000000-0000-4000-8000-000000000005"
                ),
            )

        messages = application.get_conversation(conversation["id"])["messages"]
        assert [(message["role"], message["content"]) for message in messages] == [
            ("user", "cancel this")
        ]
        details = messages[0]["technical_details"]
        assert details["generation_state"] == "cancelled"
        assert details["cancellation_state"] == "acknowledged"
    finally:
        application.close()


def test_cancel_before_runtime_generation_marks_user_request_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        version_id = "81000000-0000-4000-8000-000000000001"
        runtime_id = "81000000-0000-4000-8000-000000000002"
        operation_id = "81000000-0000-4000-8000-000000000003"
        _insert_version(
            application,
            version_id=version_id,
            label="Cancellation fixture",
            total_steps=1,
        )
        operation, _created = application.database.create_operation(
            "version_activation",
            target_id=version_id,
            dedupe_key="cancel-fixture-activation",
        )
        now = utc_now()
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
                operation["id"],
                version_id,
                str(application.paths.runtime / runtime_id),
                "d" * 64,
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
            (version_id, runtime_id, operation["id"], now),
        )
        conversation = application.create_conversation()
        monkeypatch.setattr(
            application.chat,
            "_generate_turn",
            lambda **_kwargs: (_ for _ in ()).throw(
                OperationInterrupted("cancelled before generation")
            ),
        )

        class Context:
            pass

        Context.operation_id = operation_id

        with pytest.raises(OperationInterrupted):
            application.chat._generate_message(
                conversation["id"],
                "keep the user request",
                Context(),
                "81000000-0000-4000-8000-000000000004",
                {"agent_mode": True},
            )

        messages = application.get_conversation(conversation["id"])["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["technical_details"]["generation_state"] == "cancelled"
        assert (
            messages[0]["technical_details"]["cancellation_state"]
            == "acknowledged"
        )
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == (
            "Stopped before completion. No final result was produced."
        )
        assert messages[1]["technical_details"]["turn_completion"] == "stopped"
        assert messages[1]["technical_details"]["generation_state"] == "cancelled"
    finally:
        application.close()
