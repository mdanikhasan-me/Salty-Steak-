from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backend.application import Application
from app.backend.chat.actions import (
    FILE_TRASH_ACTION,
    HOST_ACTION_SCHEMA,
    build_file_trash_proposal,
    detect_host_action_intent,
    extract_file_trash_target,
    host_action_planning_prompt,
    normalise_host_action_response,
)


def test_detects_only_explicit_image_or_terminal_action_intent() -> None:
    assert detect_host_action_intent("Generate an image of a person on a plane") == "image.generate"
    assert detect_host_action_intent("Delete this exact file on my PC via terminal") == "terminal.execute"
    assert detect_host_action_intent("Explain how terminals work") is None
    assert detect_host_action_intent("Tell me about image compression") is None


def test_exact_quoted_windows_file_delete_is_a_direct_host_action() -> None:
    request = 'Please delete "C:\\Users\\anikh\\Desktop\\note.txt" on my computer'
    assert extract_file_trash_target(request) == "C:\\Users\\anikh\\Desktop\\note.txt"
    assert detect_host_action_intent(request) == FILE_TRASH_ACTION
    assert extract_file_trash_target("Explain how to delete a file") is None
    assert extract_file_trash_target('Delete "C:\\one.txt" and "C:\\two.txt"') is None


def test_file_trash_proposal_is_host_owned_and_snapshot_bound() -> None:
    result = build_file_trash_proposal(
        user_text='Delete "C:\\Temp\\exact.txt"',
        proposal_id="proposal-file",
        snapshot={
            "path": "C:\\Temp\\exact.txt",
            "size_bytes": 17,
            "modified_ns": 99,
        },
    )
    proposal = result["proposal"]
    assert proposal["kind"] == FILE_TRASH_ACTION
    assert proposal["state"] == "pending_review"
    assert proposal["planner_used"] is False
    assert proposal["execution_allowed"] is False
    assert proposal["arguments"] == {
        "path": "C:\\Temp\\exact.txt",
        "expected_size_bytes": 17,
        "expected_modified_ns": 99,
    }


def test_image_json_becomes_blocked_host_owned_proposal() -> None:
    result = normalise_host_action_response(
        intent="image.generate",
        user_text="Generate an image of a person on a plane",
        model_text=(
            '{"action":"generate_image","prompt":'
            '"A person sitting in a commercial airplane"}'
        ),
        proposal_id="proposal-image",
        image_runtime={
            "activation_allowed": False,
            "runtime_loaded": False,
            "runtime_reason": "Missing local image pipeline",
        },
    )
    assert result is not None
    assert "{" not in result["content"]
    proposal = result["proposal"]
    assert proposal["schema"] == HOST_ACTION_SCHEMA
    assert proposal["kind"] == "image.generate"
    assert proposal["state"] == "blocked_runtime_unavailable"
    assert proposal["execution_allowed"] is False
    assert proposal["arguments"]["prompt"] == "Generate an image of a person on a plane"
    assert proposal["runtime_reason"] == "Missing local image pipeline"


def test_ambiguous_terminal_advice_is_replaced_with_clarification() -> None:
    result = normalise_host_action_response(
        intent="terminal.execute",
        user_text="Delete a temp file on my PC via terminal",
        model_text="Use rm /tmp/file and sudo rm -rf /tmp/*",
        proposal_id="proposal-terminal",
    )
    assert result is not None
    assert "exact Windows" in result["content"]
    assert "rm -rf" not in result["content"]
    assert result["proposal"]["state"] == "needs_clarification"
    assert result["proposal"]["arguments"] is None
    assert result["proposal"]["execution_allowed"] is False


def test_valid_windows_argv_is_pending_review_and_never_executable() -> None:
    result = normalise_host_action_response(
        intent="terminal.execute",
        user_text="Run git status in my project terminal",
        model_text=(
            '{"schema":"salty-steak-host-action-proposal-v1",'
            '"action":"terminal.execute","arguments":'
            '{"argv":["git","status","--short"],"timeout_seconds":5}}'
        ),
        proposal_id="proposal-git",
    )
    assert result is not None
    proposal = result["proposal"]
    assert proposal["state"] == "pending_review"
    assert proposal["arguments"]["argv"] == ["git", "status", "--short"]
    assert proposal["execution_allowed"] is False
    assert proposal["requires_confirmation"] is True


def test_unix_command_is_blocked_on_windows() -> None:
    result = normalise_host_action_response(
        intent="terminal.execute",
        user_text="Delete C:\\Temp\\a.tmp through the terminal",
        model_text=(
            '{"action":"terminal_command","arguments":'
            '{"argv":["rm","/tmp/a.tmp"],"timeout_seconds":5}}'
        ),
        proposal_id="proposal-unix",
    )
    assert result is not None
    assert result["proposal"]["state"] == "blocked_platform_mismatch"
    assert result["proposal"]["arguments"] is None
    assert "Unix" in result["content"]


def test_planner_prompts_forbid_execution_claims_and_unix_commands() -> None:
    image = host_action_planning_prompt("image.generate")
    terminal = host_action_planning_prompt("terminal.execute")
    assert "Do not claim" in image
    assert "Do not claim execution" in terminal
    assert "Never use bash" in terminal


@pytest.mark.parametrize(
    ("user_text", "model_text", "expected_state"),
    [
        (
            "Generate an image of a person on a plane",
            '{"action":"generate_image","prompt":"A person on a plane"}',
            "blocked_runtime_unavailable",
        ),
        (
            "Delete a temp file on my PC via terminal",
            "Run rm /path/to/temp.tmp and sudo rm -rf /tmp/*",
            "needs_clarification",
        ),
    ],
)
def test_chat_persists_proposal_without_invoking_automation(
    tmp_path: Path,
    user_text: str,
    model_text: str,
    expected_state: str,
) -> None:
    source = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(source, tmp_path / "config")
    (tmp_path / "config" / "local.toml").write_text(
        '[training]\ndevice="cpu"\nprecision="fp32"\n[server]\nhost="127.0.0.1"\nport=0\n',
        encoding="utf-8",
    )
    app = Application(tmp_path, recover_operations=False)
    source_sha256 = "8" * 64
    runtime_id = "91000000-0000-4000-8000-000000000099"

    class FakeRuntime:
        profile = SimpleNamespace(profile_id="action-test", context_limit=32768)
        loaded = True

        def describe(self) -> dict:
            return {
                "loaded": self.loaded,
                "runtime_id": runtime_id if self.loaded else None,
                "configured_context_limit": 32768,
                "source_sha256": source_sha256,
                "model_path": str(tmp_path / "base.gguf"),
            }

        def load(self) -> dict:
            self.loaded = True
            return self.describe()

        warmup = load

        def unload(self) -> None:
            self.loaded = False

        @staticmethod
        def generate(**_kwargs):
            return SimpleNamespace(
                cancelled=False,
                text=model_text,
                token_ids=[1, 2],
                omitted_turns=0,
                finish_reason="end_of_generation",
                technical_details={"finish_reason": "end_of_generation"},
            )

    runtime = FakeRuntime()
    app.model_bundle_runtime = runtime  # type: ignore[assignment]
    app.chat.model_bundle_runtime = runtime  # type: ignore[assignment]
    app.chat.model_bundle = {
        "id": "base-steak-2-0-9b",
        "display_name": "Base Steak 2.0",
        "checksum": source_sha256,
    }
    app.chat.image_generation_model = {
        "activation_allowed": False,
        "runtime_loaded": False,
        "runtime_reason": "Missing local image pipeline",
    }
    try:
        conversation = app.create_conversation()
        # Host actions are computer control, so they only reach a proposal
        # under agent mode. Ordinary chat no longer answers a delete request
        # with "a Windows terminal action is prepared".
        operation = app.chat.start_message(
            conversation["id"], user_text, {"agent_mode": True}
        )
        completed = app.operations.wait(operation["id"], timeout=10)
        assert completed["state"] == "completed"
        messages = app.get_conversation(conversation["id"])["messages"]
        assert len(messages) == 2
        assert messages[0]["content"] == user_text
        assert model_text not in messages[1]["content"]
        proposal = messages[1]["technical_details"]["host_action_proposal"]
        assert proposal["state"] == expected_state
        assert proposal["execution_allowed"] is False
        assert app.database.fetch_one(
            "SELECT COUNT(*) AS count FROM automation_audit_records"
        )["count"] == 0
    finally:
        app.close()
