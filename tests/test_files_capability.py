"""Files are a capability, not a service.

Asked to delete some logs and keep the notes, the model had no filesystem
capability to name, so it put the folder path into the connector slot and the
plan was refused for naming "the C:\\Users\\...\\salty-acceptance service,
which is not connected". A path is a resource, and it needed a capability that
takes one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.automation.policy import PolicyEngine
from app.backend.chat.agent_loop import AgentLoop
from app.backend.chat.task_runtime import COMPLETED, TaskContext
from app.backend.workflow import Executor, Plan, PlanNode, WorkflowEngine
from tests.test_automation_broker import _broker

FILES = "files.manage"


def _granted(tmp_path: Path):
    broker, database, project, artifacts = _broker(tmp_path)
    broker.grant({"capabilities": [FILES], "user_confirmed": True})
    return broker


def _folder(tmp_path: Path) -> Path:
    root = tmp_path / "acceptance"
    root.mkdir()
    for name in ("run-a.log", "run-b.log", "run-c.log"):
        (root / name).write_text("disposable", encoding="utf-8")
    for name in ("notes.txt", "plan.md"):
        (root / name).write_text("keep me", encoding="utf-8")
    return root


def _call(broker, **arguments):
    return broker.invoke(
        {
            "capability": FILES,
            "arguments": arguments,
            "user_confirmed": True,
            "authority_mode": "full_access",
        }
    )


def test_a_folder_can_be_listed(tmp_path: Path) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)

    result = _call(broker, operation="list", path=str(root))

    assert result["status"] == "succeeded"
    assert result["mutating"] is False
    assert len(result["matched_paths"]) == 5
    assert {entry["name"] for entry in result["entries"]} == {
        "run-a.log",
        "run-b.log",
        "run-c.log",
        "notes.txt",
        "plan.md",
    }


def test_a_pattern_selects_inside_a_folder(tmp_path: Path) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)

    result = _call(broker, operation="search", path=str(root), pattern="*.log")

    assert sorted(Path(item).name for item in result["matched_paths"]) == [
        "run-a.log",
        "run-b.log",
        "run-c.log",
    ]
    # What was deliberately left alone is reported, so "keep the notes" is
    # verifiable rather than hopeful.
    assert sorted(Path(item).name for item in result["preserved_paths"]) == [
        "notes.txt",
        "plan.md",
    ]


def test_deleting_by_pattern_leaves_everything_else(tmp_path: Path) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)

    result = _call(
        broker,
        operation="delete",
        path=str(root),
        pattern="*.log",
        permanent=True,
    )

    assert result["status"] == "succeeded"
    assert len(result["affected_paths"]) == 3
    assert result["failed_paths"] == []
    # Read back rather than trusting the calls that were just made.
    assert result["after_state"]["still_present"] == []
    assert sorted(Path(item).name for item in result["after_state"]["preserved_present"]) == [
        "notes.txt",
        "plan.md",
    ]
    assert sorted(item.name for item in root.iterdir()) == ["notes.txt", "plan.md"]


def test_noop_delete_is_failed_when_file_remains(tmp_path: Path, monkeypatch) -> None:
    broker = _granted(tmp_path)
    target = _folder(tmp_path) / "run-a.log"
    monkeypatch.setattr("app.backend.automation.broker._remove_path", lambda *args, **kwargs: None)
    result = _call(broker, operation="delete", path=str(target), permanent=True)
    assert result["status"] == "failed"
    assert result["affected_paths"] == []
    assert result["after_state"]["still_present"] == [str(target)]
    assert result["failed_paths"][0]["path"] == str(target)
    assert target.read_text() == "disposable"
    audit = broker.database.fetch_one(
        "SELECT outcome FROM automation_audit_records WHERE id = ?",
        (result["audit_record_id"],),
    )
    assert audit["outcome"] == "failed"


def test_partial_delete_only_reports_observed_removed_files(tmp_path: Path, monkeypatch) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)
    def remove(item, **kwargs):
        if item.name == "run-a.log":
            item.unlink()
    monkeypatch.setattr("app.backend.automation.broker._remove_path", remove)
    result = _call(broker, operation="delete", path=str(root), pattern="*.log", permanent=True)
    assert result["status"] == "failed"
    assert result["affected_paths"] == [str(root / "run-a.log")]
    assert len(result["failed_paths"]) == 2
    assert sorted(Path(path).name for path in result["after_state"]["still_present"]) == ["run-b.log", "run-c.log"]


def test_delete_readback_denied_is_unknown_not_success(tmp_path: Path, monkeypatch) -> None:
    broker = _granted(tmp_path)
    target = _folder(tmp_path) / "run-a.log"
    original_lstat = Path.lstat
    verification = False
    def remove(item, **kwargs):
        nonlocal verification
        verification = True
    def lstat(path, *args, **kwargs):
        if verification and path == target:
            raise PermissionError("readback denied")
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr("app.backend.automation.broker._remove_path", remove)
    monkeypatch.setattr(Path, "lstat", lstat)
    result = _call(broker, operation="delete", path=str(target), permanent=True)
    assert result["status"] == "failed"
    assert result["affected_paths"] == []
    assert result["after_state"]["unverified_paths"] == [str(target)]
    assert "Could not verify deletion" in result["failed_paths"][0]["error"]


def test_an_observed_path_set_is_the_exact_batch_that_changes(tmp_path: Path) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)
    observed = _call(broker, operation="search", path=str(root), pattern="*.log")
    selected = observed["matched_paths"][:2]

    result = _call(
        broker,
        operation="delete",
        path=str(root),
        paths=selected,
        permanent=True,
    )

    assert result["status"] == "succeeded"
    assert result["matched_paths"] == selected
    assert sorted(item.name for item in root.iterdir()) == [
        "notes.txt",
        "plan.md",
        "run-c.log",
    ]


def test_an_explicit_path_set_cannot_escape_its_declared_root(tmp_path: Path) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("keep me", encoding="utf-8")

    with pytest.raises(ValueError, match="inside the declared path"):
        _call(
            broker,
            operation="delete",
            path=str(root),
            paths=[str(root / "run-a.log"), str(outside)],
            permanent=True,
        )

    assert outside.is_file()
    assert (root / "run-a.log").is_file()


def test_a_plan_can_act_on_the_exact_paths_its_previous_node_observed(
    tmp_path: Path,
) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)
    task = TaskContext(goal="remove the observed logs")
    executor = Executor(
        broker=broker,
        task=task,
        policy=PolicyEngine(authority_mode="full_access"),
        authority_mode="full_access",
        granted=[FILES],
    )
    plan = Plan(
        goal=task.goal,
        nodes=[
            PlanNode(
                node_id="find",
                objective="observe matches",
                capability=FILES,
                arguments={
                    "operation": "search",
                    "path": str(root),
                    "pattern": "*.log",
                },
            ),
            PlanNode(
                node_id="remove",
                objective="remove exactly the observed matches",
                capability=FILES,
                arguments={
                    "operation": "delete",
                    "path": str(root),
                    "paths": {"$ref": "find.output.matched_paths"},
                    "permanent": True,
                },
                depends_on=("find",),
            ),
        ],
    )

    result = WorkflowEngine(executor=executor, task=task).run(plan)

    assert result.state == COMPLETED
    assert plan.node("remove").result["affected_paths"] == plan.node("find").result[
        "matched_paths"
    ]
    assert sorted(item.name for item in root.iterdir()) == ["notes.txt", "plan.md"]
    assert task.metrics.model_calls == 0


def test_agent_selects_an_exact_file_batch_from_the_runtime_observation(
    tmp_path: Path,
) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)
    turns = 0

    def generate(messages):
        nonlocal turns
        turns += 1
        if turns == 1:
            return json.dumps(
                {
                    "action": FILES,
                    "reason": "observe candidates",
                    "arguments": {
                        "operation": "search",
                        "path": str(root),
                        "pattern": "*.log",
                    },
                }
            )
        if turns == 2:
            observed = json.loads(messages[-1]["content"])
            matched = observed["matched_paths"]
            assert matched
            return json.dumps(
                {
                    "action": FILES,
                    "reason": "remove the observed candidates",
                    "arguments": {
                        "operation": "delete",
                        "path": str(root),
                        "paths": matched,
                        "permanent": True,
                    },
                }
            )
        return '{"action":"respond","answer":"The observed logs are gone."}'

    result = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=[FILES],
        authority_mode="full_access",
    ).run("Remove the log files in this folder but keep everything else.")

    assert result["state"] == "completed"
    assert sorted(item.name for item in root.iterdir()) == ["notes.txt", "plan.md"]
    assert [step["action"] for step in result["steps"]] == [FILES, FILES, "respond"]


def test_deleting_a_folder_without_saying_which_files_is_refused(
    tmp_path: Path,
) -> None:
    """The worst thing this capability has done.

    A real run of "delete the temporary log files but leave the notes" issued
    a delete with the folder and no pattern. Every child matched, and the two
    files the user had explicitly asked to keep were removed. An absent
    selection is a missing argument, never "everything".
    """

    broker = _granted(tmp_path)
    root = _folder(tmp_path)

    with pytest.raises(ValueError, match="needs a pattern"):
        _call(broker, operation="delete", path=str(root), permanent=True)

    assert sorted(item.name for item in root.iterdir()) == [
        "notes.txt",
        "plan.md",
        "run-a.log",
        "run-b.log",
        "run-c.log",
    ]


def test_deleting_one_named_file_still_works(tmp_path: Path) -> None:
    # The guard is about an unstated selection inside a folder, not about
    # deletion. A file named outright is unambiguous.
    broker = _granted(tmp_path)
    root = _folder(tmp_path)

    result = _call(
        broker, operation="delete", path=str(root / "run-a.log"), permanent=True
    )

    assert result["status"] == "succeeded"
    assert not (root / "run-a.log").exists()
    assert (root / "notes.txt").is_file()


def test_copy_and_move_keep_the_name_when_the_destination_is_a_folder(
    tmp_path: Path,
) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)
    destination = tmp_path / "archive"
    destination.mkdir()

    _call(
        broker,
        operation="copy",
        path=str(root),
        pattern="notes.txt",
        destination=str(destination),
    )
    assert (destination / "notes.txt").is_file()
    assert (root / "notes.txt").is_file()

    _call(
        broker,
        operation="move",
        path=str(root),
        pattern="plan.md",
        destination=str(destination),
    )
    assert (destination / "plan.md").is_file()
    assert not (root / "plan.md").exists()


def test_a_relative_or_malformed_path_is_refused(tmp_path: Path) -> None:
    broker = _granted(tmp_path)

    for bad in ("relative/path", "", "C:\\with\nnewline"):
        with pytest.raises(ValueError):
            _call(broker, operation="list", path=bad)


def test_an_unknown_operation_is_refused(tmp_path: Path) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)

    with pytest.raises(ValueError):
        _call(broker, operation="obliterate", path=str(root))


def test_the_capability_is_denied_until_granted(tmp_path: Path) -> None:
    broker, _database, _project, _artifacts = _broker(tmp_path)
    root = _folder(tmp_path)

    with pytest.raises(PermissionError):
        _call(broker, operation="list", path=str(root))


def test_every_invocation_is_audited(tmp_path: Path) -> None:
    broker = _granted(tmp_path)
    root = _folder(tmp_path)

    result = _call(broker, operation="list", path=str(root))

    assert result["audit_record_id"]
    rows = broker.database.fetch_all(
        "SELECT capability, outcome FROM automation_audit_records "
        "WHERE event = 'invoke'"
    )
    assert any(
        row["capability"] == FILES and row["outcome"] == "succeeded" for row in rows
    )
