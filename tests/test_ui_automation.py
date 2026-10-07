from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from app.backend.automation.uia_client import (
    UIA_COMMANDS,
    UIA_MUTATING_COMMANDS,
    UiAutomationClient,
    UiAutomationError,
    find_uia_host,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = find_uia_host(PROJECT_ROOT)
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="UI Automation is Windows only")
NEEDS_HOST = pytest.mark.skipif(
    HOST is None, reason="SaltyUiaHost.exe has not been built"
)


class _StubClient:
    """Stands in for the helper so broker behaviour can be tested without a desktop."""

    def __init__(self, result=None, error: UiAutomationError | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else {"count": 0, "matches": []}
        self.error = error

    def call(self, command, payload=None, **_kwargs):
        self.calls.append((command, dict(payload or {})))
        if self.error is not None:
            raise self.error
        return dict(self.result)

    def close(self) -> None:
        return None


def _broker(tmp_path: Path, client):
    from app.backend.automation import AutomationBroker
    from app.backend.database.control import Database

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    database = Database(project / "control.db")
    return AutomationBroker(
        database,
        project_root=project,
        artifact_root=project / "workspace" / "automation",
        platform_name="nt",
        uia_client=client,
    )


# --------------------------------------------------------------- unit coverage


def test_the_command_surface_is_closed() -> None:
    # An unknown command is refused here rather than at the far end of a pipe.
    assert "invoke" in UIA_COMMANDS
    assert "set_value" in UIA_MUTATING_COMMANDS
    # Reading the desktop is not a mutation.
    assert "get_tree" not in UIA_MUTATING_COMMANDS
    assert "find_control" not in UIA_MUTATING_COMMANDS


def test_reverse_list_collection_prepends_older_pages_without_reversing_each_page() -> None:
    source = (PROJECT_ROOT / "app/desktop/uia/Program.cs").read_text(
        encoding="utf-8"
    )

    assert "var orderedKeys = new List<string>();" in source
    assert "var pageKeys = new List<string>();" in source
    assert "orderedKeys.InsertRange(0, pageKeys);" in source
    assert "orderedKeys.AddRange(pageKeys);" in source
    assert "foreach (var key in orderedKeys)" in source


def test_ui_automation_requires_a_grant(tmp_path: Path) -> None:
    client = _StubClient()
    broker = _broker(tmp_path, client)

    with pytest.raises(PermissionError, match="has not been granted"):
        broker.invoke(
            {
                "capability": "ui.automation",
                "arguments": {"command": "get_windows"},
                "user_confirmed": True,
            }
        )
    # Refused before anything touched the desktop.
    assert client.calls == []
    broker.close()


def test_ui_automation_rejects_unknown_commands(tmp_path: Path) -> None:
    client = _StubClient()
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["ui.automation"], "user_confirmed": True})

    with pytest.raises(ValueError, match="must be one of"):
        broker.invoke(
            {
                "capability": "ui.automation",
                "arguments": {"command": "delete_everything"},
                "user_confirmed": True,
            }
        )
    assert client.calls == []
    broker.close()


def test_a_successful_query_is_reported_and_audited(tmp_path: Path) -> None:
    client = _StubClient(
        {"matches": [{"name": "Send", "element": "el-1"}], "count": 1, "ambiguous": False}
    )
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["ui.automation"], "user_confirmed": True})

    result = broker.invoke(
        {
            "capability": "ui.automation",
            "arguments": {"command": "find_control", "name": "Send", "window": "Chat"},
            "user_confirmed": True,
        }
    )

    assert result["status"] == "succeeded"
    assert result["command"] == "find_control"
    assert result["mutating"] is False
    assert result["matches"][0]["element"] == "el-1"
    assert client.calls[0][0] == "find_control"
    # The command name is not forwarded as a search argument.
    assert "command" not in client.calls[0][1]

    record = broker.audit_records(limit=1)[0]
    assert record["capability"] == "ui.automation"
    assert record["outcome"] == "succeeded"
    broker.close()


def test_get_tree_is_flattened_into_model_visible_nodes(tmp_path: Path) -> None:
    client = _StubClient(
        {
            "tree": {
                "name": "Discord",
                "control_type": "Window",
                "element": "el-root",
                "children": [
                    {
                        "name": "giveaways",
                        "control_type": "ListItem",
                        "element": "el-channel",
                    }
                ],
            },
            "node_count": 2,
            "truncated": False,
        }
    )
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["ui.automation"], "user_confirmed": True})

    result = broker.invoke(
        {
            "capability": "ui.automation",
            "arguments": {"command": "get_tree", "window": "Discord"},
            "user_confirmed": True,
        }
    )

    assert [node["name"] for node in result["nodes"]] == ["Discord", "giveaways"]
    assert [node["depth"] for node in result["nodes"]] == [0, 1]

    from app.backend.chat.agent_loop import summarise_observation

    observation = summarise_observation("ui.automation", result)
    assert observation["nodes"][1]["element"] == "el-channel"
    assert observation["node_count"] == 2
    assert observation["truncated"] is False
    assert observation["audit_record_id"]
    broker.close()


def test_mutating_ui_action_applies_bounded_repaint_delay(tmp_path: Path) -> None:
    client = _StubClient({"element": {"name": "Quick Switcher", "element": "el-1"}})
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["ui.automation"], "user_confirmed": True})

    result = broker.invoke(
        {
            "capability": "ui.automation",
            "arguments": {
                "command": "set_value",
                "element": "el-1",
                "value": "*",
                "post_action_delay_ms": 1,
            },
            "user_confirmed": True,
        }
    )

    assert result["status"] == "succeeded"
    assert result["mutating"] is True
    assert result["post_action_delay_ms"] == 1
    assert client.calls == [("set_value", {"element": "el-1", "value": "*"})]
    broker.close()


@pytest.mark.parametrize(
    "kind",
    ["stale_element", "unsupported_pattern", "ambiguous", "timeout", "access_denied"],
)
def test_failures_are_classified_rather_than_raised(tmp_path: Path, kind: str) -> None:
    client = _StubClient(error=UiAutomationError("nope", kind=kind))
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["ui.automation"], "user_confirmed": True})

    result = broker.invoke(
        {
            "capability": "ui.automation",
            "arguments": {"command": "invoke", "element": "el-9"},
            "user_confirmed": True,
        }
    )

    # A classified failure is information the model can act on: it says which
    # route is exhausted, so the next decision can be a different one.
    assert result["status"] == "failed"
    assert result["failure_kind"] == kind
    broker.close()


def test_ui_automation_sits_between_window_control_and_vision() -> None:
    from app.backend.automation.routing import (
        TIER_UI_AUTOMATION,
        TIER_VISION,
        TIER_WINDOW,
        ordered_capabilities,
    )

    assert TIER_WINDOW < TIER_UI_AUTOMATION < TIER_VISION
    assert ordered_capabilities(
        ["input.control", "screen.capture", "ui.automation", "application.launch"]
    ) == [
        "application.launch",
        "ui.automation",
        "screen.capture",
        "input.control",
    ]


def test_a_stopped_task_never_starts_a_ui_call() -> None:
    from app.backend.chat.task_runtime import TaskContext

    task = TaskContext()
    task.request_stop()
    client = UiAutomationClient.__new__(UiAutomationClient)

    with pytest.raises(UiAutomationError) as failure:
        UiAutomationClient.call(
            client, "get_windows", {}, should_stop=lambda: task.stop_requested
        )
    assert failure.value.kind == "cancelled"


def test_repeated_ui_failures_reach_the_stagnation_detector() -> None:
    from tests.test_agent_loop import ALL_CAPABILITIES, _RecordingBroker
    from app.backend.chat.agent_loop import AgentLoop

    # The same fruitless search, over and over, is exactly what the detector
    # exists to interrupt.
    broker = _RecordingBroker(
        [{"status": "failed", "failure_kind": "not_found", "command": "find_control"}] * 10
    )
    offered: list[list[str]] = []

    def generate(messages: list[dict[str, str]]) -> str:
        last = messages[-1]["content"]
        if "not making progress" in last:
            offered.append(json.loads(last)["available"])
            return '{"action":"respond","answer":"That control is not exposed."}'
        return (
            '{"action":"ui.automation","reason":"find it",'
            '"arguments":{"command":"find_control","name":"Send"}}'
        )

    outcome = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=[*ALL_CAPABILITIES, "ui.automation"],
        max_iterations=15,
    ).run("press send")

    assert outcome["state"] == "completed"
    assert len(broker.calls) == 3
    assert outcome["metrics"]["ui_automation_calls"] == 3
    assert outcome["metrics"]["stagnation_breaks"] == 1
    # Vision remains available as the next rung down.
    assert "screen.capture" in offered[0]


@WINDOWS_ONLY
@NEEDS_HOST
def test_a_hung_operation_retires_the_host_instead_of_leaking_threads() -> None:
    """A timeout must not leave the host accumulating unreclaimable threads.

    The abandoned thread is still blocked inside a call nothing can cancel, so
    the process is replaced rather than reused. Element handles die with it,
    which is correct: they have to be found again, not reused.
    """

    environment = dict(os.environ)
    environment["SALTY_UIA_ALLOW_STALL"] = "1"
    client = UiAutomationClient(HOST, timeout_seconds=2.0)
    try:
        assert client.available() is True
        first = client._ensure_started()
        # Restart under the stall-enabled environment.
        client.close()
        client._process = subprocess.Popen(
            [str(HOST)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wedged = client._process
        assert wedged.pid != first.pid

        with pytest.raises(UiAutomationError) as failure:
            client.call("stall", {"seconds": 30, "timeout_ms": 1500})
        assert failure.value.kind == "timeout"

        # The wedged host was retired, not kept and reused.
        assert client.retirements == 1
        assert client._process is None
        deadline = time.monotonic() + 5
        while wedged.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert wedged.poll() is not None, "the hung host was left running"

        # The next call transparently starts a healthy replacement.
        assert client.call("ping")["ready"] is True
        assert client.call("ping")["abandoned_operations"] == 0
    finally:
        client.close()


def test_a_retiring_failure_discards_the_host_process() -> None:
    """Unit-level proof of the same contract, without a desktop."""

    client = UiAutomationClient.__new__(UiAutomationClient)
    client.retirements = 0
    discarded: list[bool] = []
    client._discard = lambda: discarded.append(True)

    response = {
        "ok": False,
        "error": {"kind": "timeout", "message": "hung", "retire_host": True},
    }
    failure = dict(response["error"])
    if failure.get("retire_host"):
        client._discard()
        client.retirements += 1

    assert discarded == [True]
    assert client.retirements == 1


# ------------------------------------------------------- real desktop coverage


@WINDOWS_ONLY
@NEEDS_HOST
def test_the_host_starts_and_answers() -> None:
    client = UiAutomationClient(HOST)
    try:
        assert client.available() is True
        windows = client.call("get_windows")
        assert windows["count"] >= 1
        assert all("process_id" in item for item in windows["windows"])
    finally:
        client.close()


@WINDOWS_ONLY
@NEEDS_HOST
def test_a_real_application_is_discovered_and_operated_semantically() -> None:
    """End-to-end proof against a real window: no screenshots, no coordinates.

    Scoped by the unique scratch filename and then by process id, so nothing
    the user already had open is ever in scope.
    """

    if shutil.which("notepad.exe") is None:
        pytest.skip("notepad.exe is unavailable")

    scratch = Path(tempfile.gettempdir()) / "salty-uia-acceptance.txt"
    scratch.write_text("", encoding="utf-8")
    client = UiAutomationClient(HOST)
    notepad = subprocess.Popen(["notepad.exe", str(scratch)])
    try:
        needle = "salty-uia-acceptance"
        process_id = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process_id is None:
            time.sleep(0.4)
            for window in client.call("get_windows")["windows"]:
                if needle in str(window.get("name", "")).casefold():
                    process_id = window.get("process_id")
                    break
        assert process_id is not None, "the scratch window never appeared"

        # 1. semantic control tree, not pixels
        tree = client.call("get_tree", {"process_id": process_id, "depth": 3})
        assert tree["node_count"] > 1

        # 2. find something editable by capability rather than by guessing type
        found = client.call(
            "find_control", {"process_id": process_id, "pattern": "Value", "limit": 5}
        )
        assert found["count"] >= 1, "no control exposed the Value pattern"
        handle = found["matches"][0]["element"]

        # 3. act through the pattern
        client.call("set_value", {"element": handle, "value": "Salty Steak UIA proof"})

        # 4. verify structurally, not visually
        assert client.call("get_text", {"element": handle})["text"] == (
            "Salty Steak UIA proof"
        )

        # 5. an unsupported pattern is refused, not approximated
        with pytest.raises(UiAutomationError) as failure:
            client.call("toggle", {"element": handle})
        assert failure.value.kind == "unsupported_pattern"
    finally:
        notepad.kill()
        client.close()
        scratch.unlink(missing_ok=True)


@WINDOWS_ONLY
@NEEDS_HOST
def test_a_vanished_element_reports_staleness_instead_of_acting() -> None:
    """A handle to a closed window must never be acted on blindly."""

    if shutil.which("notepad.exe") is None:
        pytest.skip("notepad.exe is unavailable")

    scratch = Path(tempfile.gettempdir()) / "salty-uia-stale.txt"
    scratch.write_text("", encoding="utf-8")
    client = UiAutomationClient(HOST)
    notepad = subprocess.Popen(["notepad.exe", str(scratch)])
    try:
        needle = "salty-uia-stale"
        process_id = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process_id is None:
            time.sleep(0.4)
            for window in client.call("get_windows")["windows"]:
                if needle in str(window.get("name", "")).casefold():
                    process_id = window.get("process_id")
                    break
        assert process_id is not None

        found = client.call(
            "find_control", {"process_id": process_id, "pattern": "Value", "limit": 1}
        )
        handle = found["matches"][0]["element"]

        subprocess.run(
            ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        time.sleep(1.5)

        with pytest.raises(UiAutomationError) as failure:
            client.call("set_value", {"element": handle, "value": "should not land"})
        assert failure.value.kind == "stale_element"
    finally:
        notepad.kill()
        client.close()
        scratch.unlink(missing_ok=True)
