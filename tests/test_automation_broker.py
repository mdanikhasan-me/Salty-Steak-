from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from app.backend.automation import AutomationBroker
from app.backend.automation.broker import _resolve_launch_target
from app.backend.database.control import SCHEMA_VERSION, Database


def _broker(
    tmp_path: Path,
    **overrides: object,
) -> tuple[AutomationBroker, Database, Path, Path]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    artifacts = project / "workspace" / "automation"
    database = Database(project / "control.db")
    options = {
        "project_root": project,
        "artifact_root": artifacts,
        "platform_name": "nt",
    }
    options.update(overrides)
    return AutomationBroker(database, **options), database, project, artifacts


def _grant(
    broker: AutomationBroker,
    capability: str,
    **extra: object,
) -> dict:
    return broker.grant(
        {
            "capabilities": [capability],
            "user_confirmed": True,
            **extra,
        }
    )


def _invoke(
    broker: AutomationBroker,
    request: dict,
    *,
    user_confirmed: bool = True,
) -> dict:
    return broker.invoke({**request, "user_confirmed": user_confirmed})


def test_structured_file_write_create_readback_and_guarded_overwrite(tmp_path):
    broker, database, project, _ = _broker(tmp_path)
    _grant(broker, "files.manage")
    target = project / "note.txt"
    first = _invoke(broker, {"capability":"files.manage", "arguments":{
        "operation":"write", "path":str(target), "content":"First line\nবাংলা", "overwrite":False}})
    assert first["status"] == "succeeded" and first["created"] and first["readback_verified"]
    read = _invoke(broker, {"capability":"files.manage", "arguments":{"operation":"read","path":str(target)}})
    assert read["content"] == "First line\nবাংলা" and read["sha256"] == first["sha256"]
    with pytest.raises(FileExistsError):
        _invoke(broker, {"capability":"files.manage","arguments":{"operation":"write","path":str(target),"content":"bad"}})
    with pytest.raises(ValueError, match="expected_sha256"):
        _invoke(broker, {"capability":"files.manage","arguments":{"operation":"write","path":str(target),"content":"bad","overwrite":True}})
    updated = _invoke(broker, {"capability":"files.manage","arguments":{"operation":"write","path":str(target),"content":"Second","overwrite":True,"expected_sha256":read["sha256"]}})
    assert updated["readback_verified"] and target.read_text(encoding="utf-8") == "Second"
    with pytest.raises(ValueError, match="File changed"):
        _invoke(broker, {"capability":"files.manage","arguments":{"operation":"write","path":str(target),"content":"stale","overwrite":True,"expected_sha256":read["sha256"]}})
    broker.close()


@pytest.mark.parametrize("extra", [
    {"pattern": "*.txt"}, {"recursive": True}, {"content": "x" * (1024 * 1024 + 1)},
    {"content": None}, {"overwrite": "true"},
])
def test_file_write_rejects_invalid_input_without_changing_files(tmp_path, extra):
    broker, _, project, _ = _broker(tmp_path)
    _grant(broker, "files.manage")
    target = project / "new.txt"
    try:
        with pytest.raises((ValueError, TypeError)):
            _invoke(broker, {"capability": "files.manage", "arguments": {
                "operation": "write", "path": str(target), "content": "hello", **extra,
            }})
        assert not target.exists()
    finally:
        broker.close()


def test_file_write_requires_existing_parent_and_preserves_directories(tmp_path):
    broker, _, project, _ = _broker(tmp_path)
    _grant(broker, "files.manage")
    try:
        for target in [project, project / "missing" / "new.txt"]:
            with pytest.raises(ValueError, match="existing parent directory"):
                _invoke(broker, {"capability": "files.manage", "arguments": {
                    "operation": "write", "path": str(target), "content": "hello",
                }})
        assert project.is_dir() and not (project / "missing").exists()
    finally:
        broker.close()


def test_regrant_repairs_stale_moved_installation_scopes(tmp_path):
    broker, database, project, artifacts = _broker(tmp_path)
    _grant(broker,"terminal.execute")
    _grant(broker,"screen.capture")
    database.execute("UPDATE automation_grants SET constraints_json=? WHERE capability='terminal.execute'", (json.dumps({"working_directory_root":str(tmp_path/'missing')}),))
    database.execute("UPDATE automation_grants SET constraints_json=? WHERE capability='screen.capture'", (json.dumps({"screen":"primary","output_root":str(tmp_path/'old/screens')}),))
    status=broker.status();before={x['capability']:x for x in status['capabilities']}
    assert not before['terminal.execute']['effective_enabled'] and not before['screen.capture']['effective_enabled']
    status=broker.grant({"capabilities":["terminal.execute","screen.capture"],"user_confirmed":True})
    after={x['capability']:x for x in status['capabilities']}
    assert after['terminal.execute']['effective_enabled'] and after['screen.capture']['effective_enabled']
    assert after['terminal.execute']['constraints']['working_directory_root']==str(project)
    assert after['screen.capture']['constraints']['output_root']==str(artifacts/'screenshots')
    broker.close()


def test_foreground_action_waits_after_fresh_human_input(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        [
            {"sequence": 100, "idle_seconds": 0.0},
            {"sequence": 101, "idle_seconds": 0.1},
            {"sequence": 101, "idle_seconds": 4.0},
            {"sequence": 101, "idle_seconds": 12.0},
        ]
    )
    waits: list[float] = []
    progress: list[dict] = []
    broker, database, _project, _artifacts = _broker(
        tmp_path,
        user_activity_probe=lambda: next(snapshots),
        user_activity_wait=waits.append,
        user_activity_resume_idle_seconds=12.0,
    )
    try:
        broker.begin_agent_task("task-1")
        broker.set_invocation_progress_callback(progress.append)

        result = broker._await_user_idle(
            "audit-1",
            "input.control",
            {"action": "mouse_click"},
        )

        assert result["state"] == "resumed_after_user_idle"
        assert waits == [0.25, 0.25]
        assert [row["state"] for row in progress] == [
            "waiting_for_user_idle",
            "resumed_after_user_idle",
        ]
        assert progress[0]["summary"] == "Paused while you use the computer."
    finally:
        broker.close()


def test_read_only_observation_never_waits_for_user_idle(tmp_path: Path) -> None:
    snapshots = iter(
        [
            {"sequence": 10, "idle_seconds": 0.0},
            {"sequence": 11, "idle_seconds": 0.0},
        ]
    )
    broker, database, _project, _artifacts = _broker(
        tmp_path,
        user_activity_probe=lambda: next(snapshots),
        user_activity_wait=lambda _seconds: pytest.fail("read-only call must not wait"),
    )
    try:
        broker.begin_agent_task("task-1")
        result = broker._await_user_idle(
            "audit-1",
            "window.control",
            {"action": "list"},
        )
        assert result == {"state": "not_required", "waited_seconds": 0.0}
    finally:
        broker.close()


def test_stop_cancels_foreground_action_while_waiting(tmp_path: Path) -> None:
    snapshots = iter(
        [
            {"sequence": 20, "idle_seconds": 0.0},
            {"sequence": 21, "idle_seconds": 0.0},
        ]
    )
    broker, database, _project, _artifacts = _broker(
        tmp_path,
        user_activity_probe=lambda: next(snapshots),
        user_activity_wait=lambda _seconds: pytest.fail("stop must preempt waiting"),
    )
    try:
        broker.begin_agent_task("task-1", should_stop=lambda: True)
        result = broker._await_user_idle(
            "audit-1",
            "discord.inspect",
            {"operation": "current_account"},
        )
        assert result["state"] == "cancelled_while_waiting"
    finally:
        broker.close()


def test_broker_dispatches_only_after_human_activity_guard_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter(
        [
            {"sequence": 30, "idle_seconds": 0.0},
            {"sequence": 31, "idle_seconds": 0.0},
            {"sequence": 31, "idle_seconds": 12.0},
            {"sequence": 31, "idle_seconds": 12.1},
        ]
    )
    dispatched: list[dict] = []
    broker, _database, _project, _artifacts = _broker(
        tmp_path,
        user_activity_probe=lambda: next(snapshots),
        user_activity_wait=lambda _seconds: None,
        user_activity_resume_idle_seconds=12.0,
    )
    try:
        _grant(broker, "window.control")
        broker.begin_agent_task("task-1")
        monkeypatch.setattr(
            broker,
            "_invoke_window_control",
            lambda audit_id, arguments: dispatched.append(dict(arguments))
            or {
                "schema": "test",
                "audit_record_id": audit_id,
                "capability": "window.control",
                "status": "succeeded",
                "action": "focus",
            },
        )

        result = _invoke(
            broker,
            {
                "capability": "window.control",
                "arguments": {"action": "focus", "title": "Notepad"},
                "authority_mode": "full_access",
            },
        )

        assert dispatched == [{"action": "focus", "title": "Notepad"}]
        assert result["status"] == "succeeded"
        assert result["human_activity_guard"]["state"] == (
            "resumed_after_user_idle"
        )
        assert broker.status()["human_activity_guard"] == {
            "enabled": True,
            "task_active": True,
            "active_task_id": "task-1",
            "resume_after_idle_seconds": 12.0,
            "behavior": "pause_foreground_actions_after_fresh_human_input",
            "read_only_observations_continue": True,
        }
    finally:
        broker.close()


def test_bounded_temp_cleanup_requires_authority_and_skips_locked_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = []
    for name in ("User Temp", "Windows Temp", "Prefetch"):
        root = tmp_path / name.replace(" ", "-")
        root.mkdir()
        roots.append({"name": name, "path": str(root), "exists": True})
    (Path(roots[0]["path"]) / "delete.tmp").write_bytes(b"delete me")
    locked = Path(roots[1]["path"]) / "locked.tmp"
    locked.write_bytes(b"in use")
    nested = Path(roots[2]["path"]) / "nested"
    nested.mkdir()
    (nested / "delete.cache").write_bytes(b"cache")
    broker, database, _project, _artifacts = _broker(
        tmp_path,
        temp_roots_provider=lambda: [dict(root) for root in roots],
    )
    request = {
        "proposal_id": "cleanup-1",
        "roots": roots,
        "authority_mode": "ask_every_time",
        "user_confirmed": False,
    }
    with pytest.raises(PermissionError, match="requires confirmation"):
        broker.invoke_confirmed_temp_cleanup(request)

    original_unlink = Path.unlink

    def guarded_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == locked:
            raise PermissionError("fixture is in use")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    result = broker.invoke_confirmed_temp_cleanup(
        {**request, "user_confirmed": True}
    )
    assert result["status"] == "completed_with_skips"
    assert result["deleted_files"] == 2
    assert result["deleted_directories"] == 1
    assert result["skipped_entries"] == 1
    assert result["root_directories_preserved"] is True
    assert locked.is_file()
    assert all(Path(root["path"]).is_dir() for root in roots)
    audit = database.fetch_one(
        "SELECT outcome FROM automation_audit_records WHERE id = ?",
        (result["audit_record_id"],),
    )
    assert audit["outcome"] == "succeeded"
    broker.close()


def test_bounded_temp_cleanup_reports_an_inaccessible_root_and_continues(
    tmp_path: Path,
) -> None:
    writable = tmp_path / "User-Temp"
    writable.mkdir()
    stale = writable / "stale.tmp"
    stale.write_bytes(b"delete me")
    blocked = tmp_path / "Prefetch"
    roots = [
        {
            "name": "User Temp",
            "path": str(writable),
            "exists": True,
            "accessible": True,
        },
        {
            "name": "Prefetch",
            "path": str(blocked),
            "exists": True,
            "accessible": False,
            "reason": "PermissionError: access denied",
        },
    ]
    broker, _database, _project, _artifacts = _broker(
        tmp_path,
        temp_roots_provider=lambda: [dict(root) for root in roots],
    )

    result = broker.invoke_confirmed_temp_cleanup(
        {
            "proposal_id": "cleanup-inaccessible",
            "roots": roots,
            "authority_mode": "ask_every_time",
            "user_confirmed": True,
        }
    )

    assert result["status"] == "completed_with_skips"
    assert result["deleted_files"] == 1
    assert result["skipped_entries"] == 1
    assert result["roots"][1]["status"] == "inaccessible"
    assert result["roots"][1]["reason"] == "PermissionError: access denied"
    assert writable.is_dir()
    assert not stale.exists()
    broker.close()


def test_automation_schema_and_capabilities_are_disabled_by_default(
    tmp_path: Path,
) -> None:
    broker, database, _project, _artifacts = _broker(tmp_path)

    status = broker.status()

    assert SCHEMA_VERSION == 17
    assert status["default_enabled"] is False
    assert status["explicit_persisted_user_grant_required"] is True
    assert status["starts_network_service"] is False
    assert status["external_service_required"] is False
    # Mouse/keyboard control, application launch and the filesystem join the
    # original two capabilities, and every one stays denied until granted.
    assert [item["capability"] for item in status["capabilities"]] == [
        "terminal.execute",
        "files.manage",
        "screen.capture",
        "input.control",
        "application.launch",
        "window.control",
        "ui.automation",
        "browser.control",
        "discord.inspect",
    ]
    assert all(item["granted"] is False for item in status["capabilities"])
    assert all(item["effective_enabled"] is False for item in status["capabilities"])
    assert status["risk_boundaries"]["argv_only"] is True
    assert status["risk_boundaries"]["subprocess_shell_enabled"] is False
    assert status["risk_boundaries"]["shell_executables_blocked"] is False
    assert status["risk_boundaries"]["filesystem_sandbox_enforced"] is False
    tables = {
        row["name"]
        for row in database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"automation_grants", "automation_audit_records"} <= tables

    with pytest.raises(PermissionError, match="has not been granted"):
        _invoke(broker,
            {
                "capability": "terminal.execute",
                "arguments": {"argv": [sys.executable, "-c", "print('blocked')"]},
            }
        )
    denied = broker.audit_records(limit=1)[0]
    assert denied["event"] == "invoke"
    assert denied["outcome"] == "denied"
    assert denied["result"] is None
    assert denied["error"]["type"] == "PermissionError"


def test_missing_helper_never_advertises_an_effective_specialized_grant(
    tmp_path: Path,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    try:
        _grant(broker, "discord.inspect")
        discord = next(
            item
            for item in broker.status()["capabilities"]
            if item["capability"] == "discord.inspect"
        )
        assert discord["granted"] is True
        assert discord["runtime_available"] is False
        assert discord["effective_enabled"] is False
    finally:
        broker.close()


def test_schema_v7_database_adds_automation_tables_without_touching_existing_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v7.db"
    database = Database(path)
    database.execute(
        "INSERT INTO application_metadata(key, value) VALUES (?, ?)",
        ("legacy-proof", "preserved"),
    )
    with database.transaction() as connection:
        connection.execute(
            "DROP TRIGGER IF EXISTS trg_automation_grants_immutable_identity"
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS trg_automation_audit_records_immutable_identity"
        )
        connection.execute("DROP INDEX IF EXISTS ix_automation_grants_enabled")
        connection.execute("DROP INDEX IF EXISTS ix_automation_audit_created")
        connection.execute("DROP TABLE automation_audit_records")
        connection.execute("DROP TABLE automation_grants")
        connection.execute("PRAGMA user_version = 7")

    migrated = Database(path)

    assert migrated.fetch_one(
        "SELECT value FROM application_metadata WHERE key = 'legacy-proof'"
    ) == {"value": "preserved"}
    tables = {
        row["name"]
        for row in migrated.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"automation_grants", "automation_audit_records"} <= tables


def test_grant_requires_confirmation_persists_and_revoke_is_immediate(
    tmp_path: Path,
) -> None:
    broker, database, project, artifacts = _broker(tmp_path)
    with pytest.raises(PermissionError, match="user_confirmed=true"):
        broker.grant(
            {
                "capabilities": ["terminal.execute"],
                "user_confirmed": False,
            }
        )

    _grant(broker, "terminal.execute")
    broker.close()
    restarted = AutomationBroker(
        database,
        project_root=project,
        artifact_root=artifacts,
        platform_name="nt",
    )
    terminal = next(
        item
        for item in restarted.status()["capabilities"]
        if item["capability"] == "terminal.execute"
    )
    assert terminal["granted"] is True
    assert terminal["effective_enabled"] is True

    revoked = restarted.revoke({"capabilities": ["terminal.execute"]})
    terminal = next(
        item
        for item in revoked["capabilities"]
        if item["capability"] == "terminal.execute"
    )
    assert terminal["granted"] is False
    assert terminal["effective_enabled"] is False
    assert terminal["revoked_at"] is not None
    with pytest.raises(PermissionError, match="has not been granted"):
        _invoke(restarted,
            {
                "capability": "terminal.execute",
                "arguments": {"argv": [sys.executable, "-c", "print('blocked')"]},
            }
        )


def test_discord_inventory_cache_persists_and_new_task_keeps_it_for_validation(
    tmp_path: Path,
) -> None:
    broker, database, project, artifacts = _broker(tmp_path)
    broker._discord_inventory_cache = {
        "schema": "salty-steak-discord-inventory-cache-v2",
        "complete": True,
        "created_at": time.time(),
        "account": {"label": "Test User", "handle": "test.user", "observed": True},
        "servers": [{"name": "Test Guild, Server", "server": "Test Guild"}],
        "channels": [
            {
                "name": "prizes, Text Channel, Test Guild",
                "channel": "prizes",
                "server": "Test Guild",
            }
        ],
        "coverage": {"channels": {"scroll_boundary_reached": True}},
    }
    broker._persist_discord_inventory_cache()
    broker.close()

    restarted = AutomationBroker(
        database,
        project_root=project,
        artifact_root=artifacts,
        platform_name="nt",
    )
    try:
        restarted.begin_agent_task("new-task")
        assert restarted._discord_inventory_cache["complete"] is True
        assert restarted._discord_inventory_cache["owner_task_id"] == "new-task"
        assert restarted._discord_inventory_cache["channels"][0]["channel"] == (
            "prizes"
        )
        assert (artifacts / "discord-inventory.json").is_file()
    finally:
        restarted.close()


def test_discord_v1_inventory_cache_is_rejected_after_identity_key_upgrade(
    tmp_path: Path,
) -> None:
    broker, database, project, artifacts = _broker(tmp_path)
    broker.close()
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "discord-inventory.json").write_text(
        json.dumps(
            {
                "schema": "salty-steak-discord-inventory-cache-v1",
                "complete": True,
                "account": {
                    "label": "Test User",
                    "handle": "test.user",
                    "observed": True,
                },
                "servers": [{"name": "Test Guild, Server"}],
                "channels": [{"channel": "giveaways", "server": "Test Guild"}],
            }
        ),
        encoding="utf-8",
    )

    restarted = AutomationBroker(
        database,
        project_root=project,
        artifact_root=artifacts,
        platform_name="nt",
    )
    try:
        assert restarted._discord_inventory_cache == {}
    finally:
        restarted.close()


def test_terminal_uses_argv_without_shell_and_records_exact_bounded_output(
    tmp_path: Path,
) -> None:
    broker, database, project, _artifacts = _broker(
        tmp_path,
        max_output_bytes=128,
    )
    working = project / "work"
    working.mkdir()
    _grant(
        broker,
        "terminal.execute",
        working_directory_root=str(working),
    )
    literal = "literal & echo not-a-second-command"
    script = (
        "import sys; "
        "sys.stdout.write(sys.argv[1] + '\\n' + ('x' * 5000)); "
        "sys.stderr.write('bounded-error')"
    )

    result = _invoke(broker,
        {
            "capability": "terminal.execute",
            "arguments": {
                "argv": [sys.executable, "-c", script, literal],
                "working_directory": str(working),
                "timeout_seconds": 5,
            },
        }
    )

    assert result["status"] == "succeeded"
    assert result["shell"] is False
    assert result["requested_argv"][-1] == literal
    assert result["stdout"]["text"].startswith(literal)
    assert result["stdout"]["bytes_total"] == (
        len((literal + os.linesep).encode()) + 5000
    )
    assert result["stdout"]["bytes_retained"] == 128
    assert result["stdout"]["truncated"] is True
    expected_stdout = (literal + os.linesep + "x" * 5000).encode()
    assert result["stdout"]["sha256"] == hashlib.sha256(expected_stdout).hexdigest()
    assert result["stderr"]["text"] == "bounded-error"
    raw = database.fetch_one(
        "SELECT * FROM automation_audit_records WHERE id = ?",
        (result["audit_record_id"],),
    )
    assert raw is not None
    assert raw["outcome"] == "succeeded"
    assert json.loads(raw["result_json"]) == result
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        database.execute(
            "UPDATE automation_audit_records SET request_json = '{}' WHERE id = ?",
            (result["audit_record_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        database.execute(
            "DELETE FROM automation_audit_records WHERE id = ?",
            (result["audit_record_id"],),
        )


def test_invocation_requires_a_fresh_explicit_confirmation_even_after_grant(
    tmp_path: Path,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    _grant(broker, "terminal.execute")

    with pytest.raises(PermissionError, match="user_confirmed=true"):
        _invoke(
            broker,
            {
                "capability": "terminal.execute",
                "arguments": {"argv": [sys.executable, "-c", "print('blocked')"]},
            },
            user_confirmed=False,
        )

    denied = broker.audit_records(limit=1)[0]
    assert denied["event"] == "invoke"
    assert denied["outcome"] == "denied"
    assert denied["request"]["user_confirmed"] is False


def test_terminal_confines_python_bytecode_to_workspace_environment(
    tmp_path: Path,
) -> None:
    broker, _database, project, artifacts = _broker(tmp_path)
    working = project / "work"
    working.mkdir()
    _grant(
        broker,
        "terminal.execute",
        working_directory_root=str(working),
    )

    result = _invoke(broker,
        {
            "capability": "terminal.execute",
            "arguments": {
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import json, os; "
                        "print(json.dumps({"
                        "'dont_write': os.environ.get('PYTHONDONTWRITEBYTECODE'), "
                        "'cache_prefix': os.environ.get('PYTHONPYCACHEPREFIX')}))"
                    ),
                ],
                "working_directory": str(working),
            },
        }
    )

    environment = json.loads(result["stdout"]["text"])
    assert environment["dont_write"] == "1"
    assert Path(environment["cache_prefix"]).resolve() == (
        artifacts / "pycache"
    ).resolve()


def test_terminal_rejects_shell_strings_and_working_directory_escape(
    tmp_path: Path,
) -> None:
    broker, _database, project, _artifacts = _broker(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _grant(broker, "terminal.execute")

    with pytest.raises(ValueError, match="JSON array, not a shell string"):
        _invoke(broker,
            {
                "capability": "terminal.execute",
                "arguments": {"argv": "echo unsafe"},
            }
        )
    with pytest.raises(ValueError, match="outside"):
        _invoke(broker,
            {
                "capability": "terminal.execute",
                "arguments": {
                    "argv": [sys.executable, "-c", "print('no')"],
                    "working_directory": str(outside),
                },
            }
        )
    assert project not in outside.parents
    assert [record["outcome"] for record in broker.audit_records(limit=2)] == [
        "failed",
        "failed",
    ]


def test_terminal_timeout_is_bounded_and_audited(tmp_path: Path) -> None:
    broker, _database, _project, _artifacts = _broker(
        tmp_path,
        max_timeout_seconds=0.2,
    )
    _grant(broker, "terminal.execute")

    result = _invoke(broker,
        {
            "capability": "terminal.execute",
            "arguments": {
                "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                "timeout_seconds": 0.05,
            },
        }
    )

    assert result["status"] == "timed_out"
    assert result["timed_out"] is True
    assert broker.audit_records(limit=1)[0]["outcome"] == "timed_out"


def test_revoke_stops_an_active_terminal_invocation(tmp_path: Path) -> None:
    broker, _database, _project, _artifacts = _broker(
        tmp_path,
        max_timeout_seconds=10,
    )
    _grant(broker, "terminal.execute")
    result: dict[str, object] = {}

    def invoke() -> None:
        result["value"] = _invoke(broker,
            {
                "capability": "terminal.execute",
                "arguments": {
                    "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 10,
                },
            }
        )

    thread = threading.Thread(target=invoke)
    thread.start()
    deadline = time.monotonic() + 5
    while not broker.status()["active_invocations"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert broker.status()["active_invocations"]

    broker.revoke({"capabilities": ["terminal.execute"]})
    thread.join(timeout=5)

    assert thread.is_alive() is False
    assert isinstance(result["value"], dict)
    assert result["value"]["status"] == "revoked"  # type: ignore[index]
    assert next(
        item
        for item in broker.status()["capabilities"]
        if item["capability"] == "terminal.execute"
    )["effective_enabled"] is False


def test_primary_screen_capture_uses_app_owned_artifact_and_never_external_service(
    tmp_path: Path,
) -> None:
    def capture(destination: Path) -> dict[str, int]:
        destination.write_bytes(b"BM-local-screen")
        return {"width": 2, "height": 1}

    broker, _database, _project, artifacts = _broker(
        tmp_path,
        screen_capturer=capture,
    )
    _grant(broker, "screen.capture")

    result = _invoke(broker,
        {
            "capability": "screen.capture",
            "arguments": {"screen": "primary"},
        }
    )

    assert result["status"] == "succeeded"
    artifact = result["artifact"]
    path = Path(artifact["path"])
    assert path.is_file()
    assert path.parent == artifacts / "screenshots"
    assert artifact["format"] == "BMP"
    assert artifact["width"] == 2
    assert artifact["height"] == 1
    assert artifact["sha256"] == hashlib.sha256(b"BM-local-screen").hexdigest()
    assert broker.status()["external_service_required"] is False
    with pytest.raises(ValueError, match="primary"):
        _invoke(broker,
            {
                "capability": "screen.capture",
                "arguments": {"screen": "all"},
            }
        )


def test_input_control_requires_a_grant_and_records_every_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    sent: list[list[object]] = []
    monkeypatch.setattr(
        "app.backend.automation.broker._send_input_events",
        lambda events: sent.append(list(events)),
    )
    monkeypatch.setattr(
        "app.backend.automation.broker._primary_screen_size",
        lambda: (1920, 1080),
    )

    with pytest.raises(PermissionError, match="has not been granted"):
        _invoke(broker,
            {
                "capability": "input.control",
                "arguments": {"action": "mouse_move", "x": 10, "y": 10},
            }
        )
    assert sent == []

    _grant(broker, "input.control")
    result = _invoke(broker,
        {
            "capability": "input.control",
            "arguments": {
                "action": "mouse_click",
                "x": 960,
                "y": 540,
                "button": "left",
                "post_action_delay_ms": 0,
            },
        }
    )
    assert result["status"] == "succeeded"
    assert result["capability"] == "input.control"
    # Move to the point, then press and release exactly once.
    assert len(sent[0]) == 3
    assert result["normalized"] == [32767, 32767]

    record = broker.audit_records(limit=1)[0]
    assert record["capability"] == "input.control"
    assert record["outcome"] == "succeeded"
    assert record["request"]["arguments"]["action"] == "mouse_click"
    broker.close()


def test_input_control_maps_screen_pixels_onto_the_absolute_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    monkeypatch.setattr(
        "app.backend.automation.broker._send_input_events", lambda events: None
    )
    monkeypatch.setattr(
        "app.backend.automation.broker._primary_screen_size", lambda: (1920, 1080)
    )
    _grant(broker, "input.control")

    origin = _invoke(broker,
        {
            "capability": "input.control",
            "arguments": {
                "action": "mouse_move",
                "x": 0,
                "y": 0,
                "post_action_delay_ms": 0,
            },
        }
    )
    assert origin["normalized"] == [0, 0]

    # Coordinates beyond the screen clamp to the last addressable pixel rather
    # than wrapping around to the opposite edge.
    clamped = _invoke(broker,
        {
            "capability": "input.control",
            "arguments": {
                "action": "mouse_move",
                "x": 9_000,
                "y": 9_000,
                "post_action_delay_ms": 0,
            },
        }
    )
    assert clamped["normalized"] == [65500, 65474]
    broker.close()


def test_input_control_releases_combination_modifiers_in_reverse_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    sent: list = []
    monkeypatch.setattr(
        "app.backend.automation.broker._send_input_events",
        lambda events: sent.extend(events),
    )
    _grant(broker, "input.control")

    result = _invoke(broker,
        {
            "capability": "input.control",
            "arguments": {
                "action": "key_combo",
                "combo": "Ctrl+Shift+T",
                "post_action_delay_ms": 0,
            },
        }
    )
    assert result["combo"] == "ctrl+shift+t"
    # ctrl down, shift down, T down, T up, shift up, ctrl up.
    assert [event.ki.wVk for event in sent] == [0x11, 0x10, 0x54, 0x54, 0x10, 0x11]
    assert [bool(event.ki.dwFlags & 0x0002) for event in sent] == [
        False,
        False,
        False,
        True,
        True,
        True,
    ]
    broker.close()


def test_input_control_types_unicode_without_a_keyboard_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    sent: list = []
    monkeypatch.setattr(
        "app.backend.automation.broker._send_input_events",
        lambda events: sent.extend(events),
    )
    _grant(broker, "input.control")

    result = _invoke(broker,
        {
            "capability": "input.control",
            "arguments": {
                "action": "type_text",
                "text": "h\u00e9\n",
                "post_action_delay_ms": 0,
            },
        }
    )
    assert result["character_count"] == 3
    # Printable characters become Unicode scan events; the newline becomes a
    # real Return key because most controls ignore a Unicode carriage return.
    assert [event.ki.wScan for event in sent[:4]] == [
        ord("h"),
        ord("h"),
        ord("\u00e9"),
        ord("\u00e9"),
    ]
    assert all(event.ki.dwFlags & 0x0004 for event in sent[:4])
    assert [event.ki.wVk for event in sent[4:]] == [0x0D, 0x0D]
    broker.close()


def test_input_control_rejects_unknown_actions_and_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    monkeypatch.setattr(
        "app.backend.automation.broker._send_input_events", lambda events: None
    )
    _grant(broker, "input.control")

    for arguments, expected in (
        ({"action": "explode"}, "Unsupported input action"),
        ({"action": "key_press", "key": "banana"}, "Unsupported key name"),
        ({"action": "key_combo", "combo": "Ctrl"}, "at least one modifier"),
        ({"action": "mouse_move", "x": "10", "y": 10}, "must be a whole number"),
        ({"action": "type_text", "text": ""}, "must be non-empty"),
        ({"action": "mouse_scroll", "x": 1, "y": 1, "clicks": 0}, "non-zero"),
    ):
        with pytest.raises(ValueError, match=expected):
            _invoke(broker, {"capability": "input.control", "arguments": arguments})
    failed = broker.audit_records(limit=1)[0]
    assert failed["outcome"] == "failed"
    broker.close()


def test_application_launch_opens_targets_without_waiting_for_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    launched: list[tuple[str, object]] = []

    def _fake_shell_execute(target: str, arguments: object) -> int:
        launched.append((target, arguments))
        return 42

    monkeypatch.setattr(
        "app.backend.automation.broker._shell_execute", _fake_shell_execute
    )
    _grant(broker, "application.launch")

    result = _invoke(broker,
        {
            "capability": "application.launch",
            "arguments": {"target": "https://example.com/docs"},
        }
    )
    assert result["status"] == "succeeded"
    assert result["target_resolution"] == "url"
    assert result["waited_for_exit"] is False
    assert result["shell_execute_result"] == 42
    assert launched == [("https://example.com/docs", None)]

    record = broker.audit_records(limit=1)[0]
    assert record["capability"] == "application.launch"
    assert record["outcome"] == "succeeded"
    broker.close()


def test_application_launch_resolves_exact_start_menu_shortcuts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_data = tmp_path / "AppData" / "Roaming"
    programs = app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    nested = programs / "Discord Inc"
    nested.mkdir(parents=True)
    shortcut = nested / "Discord.lnk"
    shortcut.write_bytes(b"shortcut")
    monkeypatch.setenv("APPDATA", str(app_data))
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))

    resolved, source = _resolve_launch_target("discord")

    assert Path(resolved) == shortcut.resolve()
    assert source == "start_menu_shortcut"


def test_application_launch_rejects_unsafe_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    monkeypatch.setattr(
        "app.backend.automation.broker._shell_execute",
        lambda target, arguments: 42,
    )
    _grant(broker, "application.launch")

    for target, expected in (
        ("file:///C:/Windows/System32", "Only http and https"),
        ("ms-settings:privacy", "Only http and https"),
        ("\\\\server\\share\\tool.exe", "Network share"),
        ("relative\\tool.exe", "Relative launch paths"),
        ("definitely-not-installed-anywhere", "No installed application"),
    ):
        with pytest.raises((ValueError, FileNotFoundError), match=expected):
            _invoke(broker,
                {
                    "capability": "application.launch",
                    "arguments": {"target": target},
                }
            )
    broker.close()


def test_application_launch_surfaces_shell_execute_failure_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    monkeypatch.setattr(
        "app.backend.automation.broker._shell_execute",
        lambda target, arguments: 31,
    )
    _grant(broker, "application.launch")

    with pytest.raises(RuntimeError, match="No application is associated"):
        _invoke(broker,
            {
                "capability": "application.launch",
                "arguments": {"target": "https://example.com"},
            }
        )
    record = broker.audit_records(limit=1)[0]
    assert record["outcome"] == "failed"
    broker.close()


def test_full_access_lifts_the_working_directory_confinement(
    tmp_path: Path,
) -> None:
    broker, _database, project, _artifacts = _broker(tmp_path)
    _grant(broker, "terminal.execute")
    outside = tmp_path / "outside-the-project"
    outside.mkdir()

    # The default authority keeps every command inside the granted project root.
    with pytest.raises(ValueError, match="is outside"):
        _invoke(broker,
            {
                "capability": "terminal.execute",
                "arguments": {
                    "argv": [sys.executable, "-c", "print('hi')"],
                    "working_directory": str(outside),
                },
            }
        )

    confined = broker.audit_records(limit=1)[0]
    assert confined["outcome"] == "failed"
    assert confined["request"]["authority_mode"] == "ask_every_time"

    result = broker.invoke(
        {
            "capability": "terminal.execute",
            "arguments": {
                "argv": [sys.executable, "-c", "print('hi')"],
                "working_directory": str(outside),
            },
            "user_confirmed": True,
            "authority_mode": "full_access",
        }
    )
    assert result["status"] == "succeeded"
    assert Path(result["working_directory"]) == outside.resolve()
    assert project.resolve() not in Path(result["working_directory"]).parents

    record = broker.audit_records(limit=1)[0]
    assert record["request"]["authority_mode"] == "full_access"
    broker.close()


def test_elevated_terminal_requires_full_access_and_returns_authenticated_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, project, artifacts = _broker(
        tmp_path,
        package_root=Path.cwd(),
    )
    _grant(broker, "terminal.execute")
    request = {
        "capability": "terminal.execute",
        "arguments": {
            "argv": [sys.executable, "-c", "print('administrator')"],
            "working_directory": str(project),
            "elevated": True,
        },
        "user_confirmed": True,
    }

    with pytest.raises(PermissionError, match="requires Full access"):
        broker.invoke(request)

    launches: list[dict[str, object]] = []

    class FakeElevatedProcess:
        pid = 4242
        shell_execute_result = 42

        def wait(self, timeout: float | None = None) -> int:
            coordination = artifacts / "elevated-terminal"
            request_path = next(coordination.glob("*.request.json"))
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            raw = b"administrator\n"
            empty_digest = hashlib.sha256(b"").hexdigest()
            Path(payload["result_path"]).write_text(
                json.dumps(
                    {
                        "protocol": "salty-steak-elevated-terminal-v1",
                        "nonce": payload["nonce"],
                        "status": "succeeded",
                        "exit_code": 0,
                        "timed_out": False,
                        "process_elevated": True,
                        "duration_ms": 12.5,
                        "stdout": {
                            "text": raw.decode(),
                            "retained_base64": "YWRtaW5pc3RyYXRvcgo=",
                            "encoding": "utf-8_with_replacement",
                            "bytes_total": len(raw),
                            "bytes_retained": len(raw),
                            "truncated": False,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        },
                        "stderr": {
                            "text": "",
                            "retained_base64": "",
                            "encoding": "utf-8_with_replacement",
                            "bytes_total": 0,
                            "bytes_retained": 0,
                            "truncated": False,
                            "sha256": empty_digest,
                        },
                    }
                ),
                encoding="utf-8",
            )
            launches.append({"timeout": timeout, "request": payload})
            return 0

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            raise AssertionError("successful worker should not be cancelled")

        def force_kill(self) -> None:
            raise AssertionError("successful worker should not be terminated")

        def close(self) -> None:
            return None

    def fake_shell_execute_process(
        target: str,
        arguments: str,
        working_directory: str,
        *,
        verb: str,
        cancel_path: Path,
    ) -> FakeElevatedProcess:
        launches.append(
            {
                "target": target,
                "arguments": arguments,
                "working_directory": working_directory,
                "verb": verb,
                "cancel_path": cancel_path,
            }
        )
        return FakeElevatedProcess()

    monkeypatch.setattr(
        "app.backend.automation.broker._shell_execute_process",
        fake_shell_execute_process,
    )
    result = broker.invoke({**request, "authority_mode": "full_access"})

    assert result["status"] == "succeeded"
    assert result["process_elevated"] is True
    assert result["elevation_requested"] is True
    assert result["elevation_backend"] == "windows_runas_worker"
    assert result["stdout"]["text"] == "administrator\n"
    assert launches[0]["verb"] == "runas"
    assert launches[1]["request"]["argv"][0] == str(Path(sys.executable).resolve())
    assert list((artifacts / "elevated-terminal").iterdir()) == []

    record = broker.audit_records(limit=1)[0]
    assert record["request"]["authority_mode"] == "full_access"
    assert record["result"]["process_elevated"] is True
    broker.close()


def test_elevated_application_launch_requires_full_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    _grant(broker, "application.launch")
    launched: list[tuple[str, object]] = []

    def fake_elevated(target: str, arguments: object) -> dict[str, int]:
        launched.append((target, arguments))
        return {"shell_execute_result": 42, "process_id": 9123}

    monkeypatch.setattr(
        "app.backend.automation.broker._shell_execute_elevated",
        fake_elevated,
    )
    request = {
        "capability": "application.launch",
        "arguments": {"target": sys.executable, "elevate": True},
        "user_confirmed": True,
    }
    with pytest.raises(PermissionError, match="requires Full access"):
        broker.invoke(request)

    result = broker.invoke({**request, "authority_mode": "full_access"})
    assert result["status"] == "succeeded"
    assert result["process_id"] == 9123
    assert result["elevation_requested"] is True
    assert result["elevation_backend"] == "windows_runas"
    assert launched == [(str(Path(sys.executable).resolve()), None)]
    broker.close()


def test_status_does_not_claim_unconditional_directory_confinement(
    tmp_path: Path,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    boundaries = broker.status()["risk_boundaries"]

    # Reporting a flat True here would be untrue once full access exists.
    assert boundaries["working_directory_confined"] == "ask_every_time_only"
    assert boundaries["full_access_lifts_working_directory_confinement"] is True
    assert boundaries["full_access_can_request_process_elevation"] is True
    assert boundaries["elevation_requires_windows_uac"] is True
    assert boundaries["desktop_ui_process_is_elevated"] is False
    broker.close()


def test_invocation_rejects_an_unknown_authority_mode(tmp_path: Path) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    _grant(broker, "terminal.execute")

    with pytest.raises(ValueError, match="Invalid computer authority mode"):
        broker.invoke(
            {
                "capability": "terminal.execute",
                "arguments": {"argv": [sys.executable, "-c", "print(1)"]},
                "user_confirmed": True,
                "authority_mode": "unlimited",
            }
        )
    broker.close()


def test_window_control_lists_focuses_and_refuses_ambiguous_titles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    windows = [
        {"handle": 11, "title": "Notepad", "process_id": 100},
        {"handle": 22, "title": "Report - Notepad", "process_id": 101},
        {"handle": 33, "title": "Calculator", "process_id": 102},
    ]
    focused: list[int] = []
    monkeypatch.setattr(
        "app.backend.automation.broker._enumerate_windows",
        lambda **_kwargs: [dict(item) for item in windows],
    )
    monkeypatch.setattr(
        "app.backend.automation.broker._focus_window",
        lambda handle: focused.append(handle) or {"foreground_after": True},
    )
    _grant(broker, "window.control")

    listed = _invoke(broker, {"capability": "window.control", "arguments": {"action": "list"}})
    assert listed["status"] == "succeeded"
    assert listed["window_count"] == 3

    # An exact title wins over the substring match that also contains it.
    exact = _invoke(broker,
        {
            "capability": "window.control",
            "arguments": {"action": "focus", "title": "Notepad"},
        }
    )
    assert exact["window"]["handle"] == 11
    assert focused == [11]
    assert exact["focus_evidence"]["foreground_after"] is True

    # A fragment matching several windows is reported, never guessed.
    with pytest.raises(ValueError, match="2 windows match"):
        _invoke(broker,
            {
                "capability": "window.control",
                "arguments": {"action": "focus", "title": "note"},
            }
        )
    with pytest.raises(ValueError, match="No open window title contains"):
        _invoke(broker,
            {
                "capability": "window.control",
                "arguments": {"action": "focus", "title": "nothing here"},
            }
        )
    record = broker.audit_records(limit=1)[0]
    assert record["capability"] == "window.control"
    broker.close()


def test_window_control_focus_can_restore_a_hidden_tray_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    calls: list[bool] = []
    focused: list[int] = []

    def enumerate_windows(*, include_hidden: bool = False):
        calls.append(include_hidden)
        return [
            {
                "handle": 44,
                "title": "Discord Overlay Input Trap",
                "process_id": 200,
                "visible": False,
            },
            {
                "handle": 55,
                "title": "Friends - Discord",
                "process_id": 201,
                "visible": False,
            },
        ]

    monkeypatch.setattr(
        "app.backend.automation.broker._enumerate_windows", enumerate_windows
    )
    monkeypatch.setattr(
        "app.backend.automation.broker._focus_window",
        lambda handle: focused.append(handle) or {"foreground_after": True},
    )
    _grant(broker, "window.control")

    result = _invoke(
        broker,
        {
            "capability": "window.control",
            "arguments": {"action": "focus", "title": "Discord"},
        },
    )

    assert calls == [True]
    assert focused == [55]
    assert result["window"]["title"] == "Friends - Discord"
    assert result["window"]["visible"] is False
    broker.close()


def test_window_control_requires_a_grant_like_every_other_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    enumerated: list[bool] = []
    monkeypatch.setattr(
        "app.backend.automation.broker._enumerate_windows",
        lambda **_kwargs: enumerated.append(True) or [],
    )

    with pytest.raises(PermissionError, match="has not been granted"):
        _invoke(broker, {"capability": "window.control", "arguments": {"action": "list"}})
    # The refusal happens before anything reads the desktop.
    assert enumerated == []
    broker.close()


def test_capture_dimensions_reduce_a_four_k_screen_into_the_size_budget() -> None:
    from app.backend.automation.broker import (
        MAX_SCREENSHOT_BYTES,
        _capture_dimensions,
    )

    # A 1080p capture already fits the budget and must not be resampled.
    assert _capture_dimensions(1920, 1080) == (1920, 1080, 1)

    width, height, divisor = _capture_dimensions(3840, 2160)
    assert divisor == 2
    assert (width, height) == (1920, 1080)
    assert width * height * 4 <= MAX_SCREENSHOT_BYTES


def test_helpers_are_found_in_the_package_not_in_the_workspace(tmp_path: Path) -> None:
    # The installed build keeps the user-owned workspace outside the package, so
    # automation scope and helper discovery are two different directories. When
    # both were read from `project_root` the shipped browser and interface
    # helpers were reported as "not part of this build" on a real install.
    package = tmp_path / "Program Files" / "Salty Steak"
    (package / "browser").mkdir(parents=True)
    (package / "uia").mkdir(parents=True)
    (package / "browser" / "SaltyBrowserHost.exe").write_bytes(b"MZ")
    (package / "uia" / "SaltyUiaHost.exe").write_bytes(b"MZ")

    workspace_owner = tmp_path / "AppData" / "Salty Steak"
    (workspace_owner / "Workspace").mkdir(parents=True)
    database = Database(workspace_owner / "control.db")

    broker = AutomationBroker(
        database,
        project_root=workspace_owner,
        artifact_root=workspace_owner / "Workspace" / "automation",
        package_root=package,
        platform_name="nt",
    )
    try:
        available = {
            item["capability"]: item["runtime_available"]
            for item in broker.status()["capabilities"]
        }
        assert available["browser.control"] is True
        assert available["ui.automation"] is True
        assert available["discord.inspect"] is True
        # Scope is unchanged: confinement still follows the workspace owner.
        assert broker.project_root == workspace_owner.resolve()
    finally:
        broker.close()
