from __future__ import annotations

import threading
import time

import pytest

from app.backend.chat.task_runtime import (
    COMPLETED,
    EXECUTING,
    PLANNING,
    QUEUED,
    STOPPED,
    STOPPING,
    CancellationToken,
    TaskCancelled,
    TaskContext,
    bind_operation_stop,
)


def test_a_tripped_token_never_untrips() -> None:
    token = CancellationToken()

    assert token.tripped is False
    assert token.trip("user_requested") is True
    assert token.tripped is True
    assert token.reason == "user_requested"
    # A second trip is a no-op, and the original reason survives.
    assert token.trip("something_else") is False
    assert token.reason == "user_requested"
    with pytest.raises(TaskCancelled, match="user_requested"):
        token.raise_if_tripped()


def test_tripping_releases_everything_the_task_holds() -> None:
    token = CancellationToken()
    released: list[str] = []
    token.on_trip(lambda: released.append("subprocess"))
    token.on_trip(lambda: released.append("worker"))

    token.trip()

    assert released == ["subprocess", "worker"]
    # Registering after the fact still releases: a resource acquired during
    # the race must not be left running.
    token.on_trip(lambda: released.append("late"))
    assert released == ["subprocess", "worker", "late"]


def test_a_failing_release_does_not_block_the_others() -> None:
    token = CancellationToken()
    released: list[str] = []

    def explode() -> None:
        raise RuntimeError("release failed")

    token.on_trip(explode)
    token.on_trip(lambda: released.append("second"))

    token.trip()

    assert released == ["second"]


def test_stop_enters_stopping_before_it_unwinds() -> None:
    seen: list[str] = []
    task = TaskContext(goal="long task", on_change=lambda item: seen.append(item.state))
    task.transition(PLANNING)
    task.transition(EXECUTING)

    assert task.request_stop() is True

    # The visible state changes first so the interface can respond at once.
    assert seen[-1] == STOPPING
    assert task.stop_requested is True
    task.finish_stopped()
    assert task.state == STOPPED
    assert task.metrics.cancellation_latency_seconds is not None


def test_a_terminal_task_cannot_be_dragged_back_to_life() -> None:
    task = TaskContext()
    task.request_stop()
    task.finish_stopped()

    # A late model reply must not resurrect a stopped task.
    assert task.transition(EXECUTING) == STOPPED
    assert task.state == STOPPED


def test_stopping_a_finished_task_is_refused() -> None:
    task = TaskContext()
    task.finish(COMPLETED)

    assert task.request_stop() is False
    assert task.state == COMPLETED


def test_metrics_count_each_tool_call_against_its_own_rung() -> None:
    task = TaskContext()
    task.note_tool_call("application.launch", "native")
    task.note_tool_call("screen.capture", "vision")
    task.note_tool_call("input.control", "raw_input")
    task.note_model_call(0.25)

    metrics = task.metrics.to_dict()
    assert metrics["native_calls"] == 1
    assert metrics["vision_calls"] == 1
    assert metrics["raw_input_calls"] == 1
    assert metrics["tool_calls"] == 3
    assert metrics["model_calls"] == 1
    assert metrics["model_seconds"] == 0.25
    # Time to first action is captured on the first tool call, not the last.
    assert metrics["first_action_seconds"] is not None


def test_the_event_log_is_ordered_and_bounded() -> None:
    task = TaskContext()
    for index in range(700):
        task.record_event("probe", index=index)

    events = task.events
    assert len(events) == 500
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )
    # The oldest entries are dropped, never the newest.
    assert events[-1].detail["index"] == 699


def test_snapshot_is_the_single_shape_every_surface_renders() -> None:
    task = TaskContext(goal="open youtube")
    task.transition(PLANNING)
    task.current_step = 2
    task.current_capability = "application.launch"

    snapshot = task.snapshot()

    assert snapshot["state"] == PLANNING
    assert snapshot["state_label"] == "Planning"
    assert snapshot["active"] is True
    assert snapshot["finished"] is False
    assert snapshot["current_step"] == 2
    assert snapshot["current_capability"] == "application.launch"
    # No fabricated total: a dynamic task has no known plan length.
    assert snapshot["planned_step_count"] is None
    assert snapshot["goal"] == "open youtube"


def test_a_declared_plan_reports_its_real_length() -> None:
    task = TaskContext()
    task.current_plan = [{"id": 1}, {"id": 2}, {"id": 3}]

    assert task.snapshot()["planned_step_count"] == 3


def test_the_operation_stop_flag_trips_the_task_token() -> None:
    task = TaskContext()
    requested = threading.Event()

    bind_operation_stop(task, requested.is_set)
    assert task.stop_requested is False

    requested.set()
    deadline = time.monotonic() + 3
    while not task.stop_requested and time.monotonic() < deadline:
        time.sleep(0.02)

    assert task.stop_requested is True
    assert task.state == STOPPING


def test_a_fresh_task_starts_queued_with_no_cost() -> None:
    task = TaskContext()

    assert task.state == QUEUED
    assert task.stop_requested is False
    assert task.metrics.model_calls == 0
    assert task.metrics.tool_calls == 0
    assert task.snapshot()["metrics"]["screenshots"] == 0


def test_live_planning_preview_reports_measured_counts_without_raw_text() -> None:
    task = TaskContext(goal="inspect Discord")
    task.configure_mission_budget(step_limit=8_192, duration_limit_seconds=28_800)
    task.begin_generation_preview("Selecting the next action.")
    task.note_generation_preview(
        {
            "token_count": 7,
            "character_count": 42,
            "tail_text": "private control json must not escape",
        }
    )

    snapshot = task.snapshot()
    assert snapshot["generation_preview"] == {
        "kind": "planning",
        "summary": "Selecting the next audited action from observed state.",
        "tail_text": "",
        "token_count": 7,
        "character_count": 42,
        "active": True,
    }
    assert snapshot["metrics"]["planning_output_tokens"] == 7
    assert snapshot["metrics"]["planning_output_characters"] == 42
    assert snapshot["mission_budget"]["step_limit"] == 8_192
    assert snapshot["mission_budget"]["duration_limit_seconds"] == 28_800

    # Cumulative metrics advance by the measured delta, not by every repeated
    # preview poll carrying the same count.
    task.note_generation_preview({"token_count": 7, "character_count": 42})
    assert task.snapshot()["metrics"]["planning_output_tokens"] == 7


def test_compound_tool_progress_keeps_output_counts_and_updates_public_summary() -> None:
    task = TaskContext(goal="inspect Discord")
    task.begin_generation_preview("Planning")
    task.note_generation_preview({"token_count": 40, "character_count": 160})
    task.finish_generation_preview()

    task.note_tool_progress(
        {
            "summary": "Indexed 12 of 51 Discord servers.",
            "completed": 12,
            "total": 51,
            "channels": 240,
        }
    )
    snapshot = task.snapshot()

    assert snapshot["metrics"]["planning_output_tokens"] == 40
    assert snapshot["generation_preview"] == {
        "kind": "tool",
        "summary": "Indexed 12 of 51 Discord servers.",
        "tail_text": "",
        "token_count": 40,
        "character_count": 160,
        "active": True,
        "completed": 12,
        "total": 51,
        "channels": 240,
    }
