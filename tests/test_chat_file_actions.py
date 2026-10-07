from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.backend.application import Application
from app.backend.chat.actions import (
    TEMP_CLEANUP_CONFIRMATION,
    is_temp_cleanup_request,
)


def _application(tmp_path: Path) -> Application:
    source = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(source, tmp_path / "config")
    (tmp_path / "config" / "local.toml").write_text(
        '[training]\ndevice="cpu"\nprecision="fp32"\n'
        '[server]\nhost="127.0.0.1"\nport=0\n',
        encoding="utf-8",
    )
    app = Application(tmp_path, recover_operations=False)
    # Keep the contract deterministic and isolated in tests. Production uses
    # the Windows Shell Recycle Bin implementation.
    app.automation._file_recycler = lambda path: path.unlink()  # type: ignore[attr-defined]
    return app


def test_temp_cleanup_intent_requires_an_explicit_bulk_scope() -> None:
    assert is_temp_cleanup_request("Clean all temporary files on my computer") is True
    assert is_temp_cleanup_request("Clear %TEMP% and Prefetch") is True
    assert is_temp_cleanup_request("Delete a temp file on my PC via terminal") is False


def test_ask_temp_cleanup_requires_the_exact_irreversible_confirmation(
    tmp_path: Path,
) -> None:
    app = _application(tmp_path)
    roots = []
    for name in ("User Temp", "Windows Temp", "Prefetch"):
        root = tmp_path / f"ask-fixture-{name.replace(' ', '-')}"
        root.mkdir()
        (root / "preserve-until-confirmed.tmp").write_text(name, encoding="utf-8")
        roots.append({"name": name, "path": str(root), "exists": True})
    app.automation._temp_roots_provider = lambda: [  # type: ignore[attr-defined]
        dict(root) for root in roots
    ]
    try:
        conversation = app.create_conversation()
        prepared = app.send_message(
            conversation["id"],
            "Clean all temporary files on my computer",
            "prepare-ask-temp-cleanup",
            {"computer_authority_mode": "ask_every_time", "agent_mode": True},
        )
        app.operations.wait(prepared["id"], timeout=10)
        messages = app.get_conversation(conversation["id"])["messages"]
        proposal = messages[1]["technical_details"]["host_action_proposal"]

        with pytest.raises(PermissionError, match="exact destructive-action confirmation"):
            app.confirm_host_action(
                conversation["id"],
                proposal["id"],
                messages[1]["id"],
                "missing-temp-confirmation",
            )

        assert all(Path(root["path"]).joinpath("preserve-until-confirmed.tmp").is_file() for root in roots)
        assert app.database.fetch_one(
            "SELECT COUNT(*) AS count FROM automation_audit_records"
        )["count"] == 0

        operation = app.confirm_host_action(
            conversation["id"],
            proposal["id"],
            messages[1]["id"],
            "exact-temp-confirmation",
            confirmation_text=TEMP_CLEANUP_CONFIRMATION,
        )
        completed = app.operations.wait(operation["id"], timeout=10)
        assert completed["state"] == "completed"
        assert all(Path(root["path"]).is_dir() and not list(Path(root["path"]).iterdir()) for root in roots)
    finally:
        app.close()


def test_chat_file_action_requires_confirmation_then_records_real_result(tmp_path: Path) -> None:
    app = _application(tmp_path)
    target = tmp_path / "exact note.txt"
    target.write_text("owned fixture", encoding="utf-8")
    try:
        conversation = app.create_conversation()
        request = f'Delete "{target}" on my computer'
        prepared = app.send_message(
            conversation["id"], request, "prepare-file-action",
            {"agent_mode": True},
        )
        completed = app.operations.wait(prepared["id"], timeout=10)
        assert completed["state"] == "completed"
        assert target.is_file()
        messages = app.get_conversation(conversation["id"])["messages"]
        assert len(messages) == 2
        assert messages[0]["content"] == request
        proposal = messages[1]["technical_details"]["host_action_proposal"]
        assert proposal["kind"] == "filesystem.trash_file"
        assert proposal["state"] == "pending_review"
        assert proposal["planner_used"] is False
        assert app.database.fetch_one(
            "SELECT COUNT(*) AS count FROM automation_audit_records"
        )["count"] == 0

        operation = app.confirm_host_action(
            conversation["id"],
            proposal["id"],
            messages[1]["id"],
            "confirm-file-action",
        )
        executed = app.operations.wait(operation["id"], timeout=10)
        assert executed["state"] == "completed"
        assert not target.exists()
        final = app.get_conversation(conversation["id"])["messages"][1]
        final_proposal = final["technical_details"]["host_action_proposal"]
        assert final_proposal["state"] == "completed"
        assert final_proposal["result"]["recoverable"] is True
        assert "Windows Recycle Bin" in final["content"]
        audit = app.automation_audit(limit=1)[0]
        assert audit["outcome"] == "succeeded"
        assert audit["request"]["proposal_id"] == proposal["id"]
        assert audit["result"]["path"] == str(target.resolve())
    finally:
        app.close()


def test_chat_file_action_fails_closed_if_file_changes_after_review(tmp_path: Path) -> None:
    app = _application(tmp_path)
    target = tmp_path / "mutable.txt"
    target.write_text("before", encoding="utf-8")
    try:
        conversation = app.create_conversation()
        prepared = app.send_message(
            conversation["id"],
            f'Delete "{target}" on my computer',
            "prepare-mutable-file",
            {"agent_mode": True},
        )
        app.operations.wait(prepared["id"], timeout=10)
        messages = app.get_conversation(conversation["id"])["messages"]
        proposal = messages[1]["technical_details"]["host_action_proposal"]
        target.write_text("changed after review", encoding="utf-8")
        operation = app.confirm_host_action(
            conversation["id"],
            proposal["id"],
            messages[1]["id"],
            "confirm-stale-file",
        )
        failed = app.operations.wait(operation["id"], timeout=10)
        assert failed["state"] == "failed"
        assert target.is_file()
        final = app.get_conversation(conversation["id"])["messages"][1]
        assert final["technical_details"]["host_action_proposal"]["state"] == "failed"
        audit = app.automation_audit(limit=1)[0]
        assert audit["outcome"] == "failed"
    finally:
        app.close()


def test_file_action_user_turn_cannot_be_retried_as_model_generation(
    tmp_path: Path,
) -> None:
    app = _application(tmp_path)
    target = tmp_path / "retry-must-not-run.txt"
    target.write_text("preserve", encoding="utf-8")
    try:
        conversation = app.create_conversation()
        prepared = app.send_message(
            conversation["id"],
            f'Delete "{target}" on my computer',
            "prepare-retry-guard",
            {"agent_mode": True},
        )
        completed = app.operations.wait(prepared["id"], timeout=10)
        assert completed["state"] == "completed"
        persisted = app.get_conversation(conversation["id"])
        user_message = persisted["messages"][0]

        with pytest.raises(
            ValueError,
            match="Computer actions cannot be retried as model responses",
        ):
            app.retry_message(
                conversation["id"],
                user_message["id"],
                "retry-file-action-must-fail",
            )

        assert target.read_text(encoding="utf-8") == "preserve"
    finally:
        app.close()


def test_full_access_exact_file_request_uses_the_general_agent_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _application(tmp_path)
    target = tmp_path / "full-access-fixture.txt"
    target.write_text("recoverable", encoding="utf-8")
    captured = {}

    def submit(conversation_id, worker, *, generation_settings):
        captured.update(
            conversation_id=conversation_id,
            worker=worker,
            generation_settings=dict(generation_settings),
        )
        return {"id": "general-agent-generation"}

    monkeypatch.setattr(app.chat, "_submit_generation", submit)
    try:
        conversation = app.create_conversation()
        prepared = app.chat.start_message(
            conversation["id"],
            f'Delete "{target}" on my computer',
            {"computer_authority_mode": "full_access", "agent_mode": True},
        )
        assert prepared == {"id": "general-agent-generation"}
        assert captured["generation_settings"]["computer_authority_mode"] == (
            "full_access"
        )
        assert target.is_file()
    finally:
        app.close()


def test_full_access_temp_cleanup_request_uses_the_general_agent_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _application(tmp_path)
    captured = {}

    def submit(conversation_id, worker, *, generation_settings):
        captured.update(
            conversation_id=conversation_id,
            worker=worker,
            generation_settings=dict(generation_settings),
        )
        return {"id": "general-agent-generation"}

    monkeypatch.setattr(app.chat, "_submit_generation", submit)
    try:
        conversation = app.create_conversation()
        prepared = app.chat.start_message(
            conversation["id"],
            "Clean all temp data from my computer",
            {"computer_authority_mode": "full_access", "agent_mode": True},
        )
        assert prepared == {"id": "general-agent-generation"}
        assert captured["generation_settings"]["computer_authority_mode"] == (
            "full_access"
        )
    finally:
        app.close()


def test_a_delete_request_outside_agent_mode_never_becomes_a_proposal(
    tmp_path: Path,
) -> None:
    """Computer control needs Agent, including the file-trash proposal path."""

    app = _application(tmp_path)
    try:
        conversation = app.create_conversation()
        target = tmp_path / "scratch.txt"
        target.write_text("x", encoding="utf-8")

        prepared = app.send_message(
            conversation["id"],
            f'Delete "{target}" on my computer',
            "no-agent-file",
        )
        app.operations.wait(prepared["id"], timeout=20)

        messages = app.get_conversation(conversation["id"])["messages"]
        assistant = next(
            (item for item in messages if item["role"] == "assistant"), None
        )
        details = (assistant or {}).get("technical_details") or {}
        # No host-action proposal, and the file is untouched.
        assert "host_action_proposal" not in details
        assert target.exists()
    finally:
        app.close()
