"""Salty Steak Native Desktop AI Platform — canonical automation task runtime.

One object owns the truth about a running task: its state, its cancellation
token, its metrics, and its event stream.  Every surface that reports on a task
— the activity panel, the audit trail, diagnostics — reads from here rather
than deriving its own interpretation, because two independent readings of the
same task eventually disagree in front of the user.

Cancellation is a memory flag, not a query.  Asking a database whether the user
pressed Stop makes the answer as slow as the database, and Stop is the one
control that must never feel slow.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from ..automation.credentials import redact
from .world_state import WorldState

TASK_RUNTIME_SCHEMA = "salty-steak-task-runtime-v1"



QUEUED = "queued"
PLANNING = "planning"
EXECUTING = "executing"
OBSERVING = "observing"
VERIFYING = "verifying"
WAITING = "waiting"
COMPLETED = "completed"
STOPPING = "stopping"
STOPPED = "stopped"
FAILED = "failed"

TASK_STATES = (
    QUEUED,
    PLANNING,
    EXECUTING,
    OBSERVING,
    VERIFYING,
    WAITING,
    COMPLETED,
    STOPPING,
    STOPPED,
    FAILED,
)
TERMINAL_STATES = frozenset({COMPLETED, STOPPED, FAILED})
ACTIVE_STATES = frozenset(
    {QUEUED, PLANNING, EXECUTING, OBSERVING, VERIFYING, WAITING, STOPPING}
)



STATE_LABELS = {
    QUEUED: "Queued",
    PLANNING: "Planning",
    EXECUTING: "Executing",
    OBSERVING: "Observing",
    VERIFYING: "Verifying",
    WAITING: "Waiting",
    COMPLETED: "Completed",
    STOPPING: "Stopping",
    STOPPED: "Stopped",
    FAILED: "Failed",
}

MAX_RETAINED_EVENTS = 500


class TaskCancelled(RuntimeError):
    """Raised when a tripped cancellation token halts the task."""


class CancellationToken:
    """A one-way in-memory stop flag shared by everything a task owns.

    Once tripped it never untrips.  A task that was cancelled must not come
    back to life because a slow model reply finally arrived.
    """

    __slots__ = ("_event", "_reason", "_tripped_at", "_lock", "_callbacks")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._tripped_at: float | None = None
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def tripped(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def tripped_at(self) -> float | None:
        return self._tripped_at

    def trip(self, reason: str = "stop_requested") -> bool:
        """Trip the token. Returns False when it was already tripped."""

        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason)
            self._tripped_at = time.monotonic()
            callbacks = list(self._callbacks)
            self._event.set()


        for callback in callbacks:
            try:
                callback()
            except BaseException:

                continue
        return True

    def on_trip(self, callback: Callable[[], None]) -> None:
        """Register a release to run when the token trips.

        Registering after the token is already tripped runs the callback
        immediately, so a resource acquired during a race is still released.
        """

        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback()

    def raise_if_tripped(self) -> None:
        if self._event.is_set():
            raise TaskCancelled(self._reason or "stop_requested")

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass
class TaskMetrics:
    """What a task actually cost.

    Reported rather than estimated: every counter here is incremented at the
    point the work happens.
    """

    model_calls: int = 0
    model_seconds: float = 0.0
    vision_calls: int = 0
    vision_seconds: float = 0.0
    screenshots: int = 0
    native_calls: int = 0
    api_calls: int = 0
    terminal_calls: int = 0
    window_calls: int = 0
    ui_automation_calls: int = 0
    raw_input_calls: int = 0
    tool_calls: int = 0
    retries: int = 0
    escalations: int = 0
    replans: int = 0
    stagnation_breaks: int = 0
    parse_failures: int = 0
    first_action_seconds: float | None = None
    cancellation_latency_seconds: float | None = None
    total_seconds: float = 0.0

    def record_tier(self, tier_name: str) -> None:
        """Count a tool call against the rung of the ladder that served it."""

        self.tool_calls += 1
        counter = {
            "native": "native_calls",
            "api": "api_calls",
            "terminal": "terminal_calls",
            "window": "window_calls",
            "ui_automation": "ui_automation_calls",
            "vision": "vision_calls",
            "raw_input": "raw_input_calls",
        }.get(tier_name)
        if counter:
            setattr(self, counter, getattr(self, counter) + 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: (round(value, 4) if isinstance(value, float) else value)
            for key, value in asdict(self).items()
        }


@dataclass
class TaskEvent:
    """One entry in the task's own record of what happened."""

    sequence: int
    at_seconds: float
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "at_seconds": round(self.at_seconds, 4),
            "kind": self.kind,
            "detail": dict(self.detail),
        }


class TaskContext:
    """The single authoritative object for one running automation task."""

    def __init__(
        self,
        *,
        task_id: str | None = None,
        goal: str = "",
        on_change: Callable[["TaskContext"], None] | None = None,
    ) -> None:
        self.task_id = task_id or str(uuid.uuid4())
        self.goal = str(goal)
        self.cancellation = CancellationToken()
        self.metrics = TaskMetrics()
        self.world_state = WorldState()
        self.current_step: int = 0
        self.current_capability: str | None = None
        self.current_plan: list[dict[str, Any]] = []


        self.waiting_for: str | None = None
        self.waiting_detail: dict[str, Any] = {}
        self.failure: str | None = None
        self._state = QUEUED
        self._events: list[TaskEvent] = []
        self._sequence = 0
        self._started = time.monotonic()
        self._lock = threading.RLock()
        self._on_change = on_change



    @property
    def state(self) -> str:
        return self._state

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    @property
    def finished(self) -> bool:
        return self._state in TERMINAL_STATES

    def transition(self, state: str, **detail: Any) -> str:
        """Move to a new state and record the move.

        A terminal state is final: a late arrival cannot drag a stopped task
        back into an active state.
        """

        if state not in TASK_STATES:
            raise ValueError(f"Unknown task state: {state!r}")
        with self._lock:
            if self._state in TERMINAL_STATES and state not in TERMINAL_STATES:
                return self._state
            if self._state == state:
                return self._state
            previous, self._state = self._state, state
            if state == WAITING:
                self.waiting_for = str(detail.get("waiting_for") or "user_answer")
                self.waiting_detail = redact(dict(detail))
            else:


                self.waiting_for = None
                self.waiting_detail = {}
        self.record_event("state", previous=previous, state=state, **detail)
        self._notify()
        return state



    def request_stop(self, reason: str = "user_requested") -> bool:
        """Accept a stop. Enters STOPPING immediately, before any unwinding."""

        if self.finished:
            return False


        self.transition(STOPPING, reason=reason)
        tripped = self.cancellation.trip(reason)
        if tripped:
            self.record_event("cancellation_requested", reason=reason)
        return tripped

    @property
    def stop_requested(self) -> bool:
        return self.cancellation.tripped

    def raise_if_stopped(self) -> None:
        self.cancellation.raise_if_tripped()

    def finish_stopped(self) -> None:
        """Complete the stop and record how long the whole path took."""

        tripped_at = self.cancellation.tripped_at
        if tripped_at is not None and self.metrics.cancellation_latency_seconds is None:
            self.metrics.cancellation_latency_seconds = time.monotonic() - tripped_at
        self.metrics.total_seconds = self.elapsed_seconds
        self.transition(STOPPED)

    def finish(self, state: str, *, failure: str | None = None) -> None:
        if failure:
            self.failure = str(failure)
        self.metrics.total_seconds = self.elapsed_seconds
        self.transition(state, failure=self.failure)



    def record_event(self, kind: str, **detail: Any) -> TaskEvent:



        safe_detail = redact({key: value for key, value in detail.items()})
        with self._lock:
            self._sequence += 1
            event = TaskEvent(
                sequence=self._sequence,
                at_seconds=self.elapsed_seconds,
                kind=str(kind),
                detail=safe_detail,
            )
            self._events.append(event)

            if len(self._events) > MAX_RETAINED_EVENTS:
                del self._events[: len(self._events) - MAX_RETAINED_EVENTS]
        return event

    @property
    def events(self) -> list[TaskEvent]:
        with self._lock:
            return list(self._events)



    def note_model_call(self, seconds: float) -> None:
        self.metrics.model_calls += 1
        self.metrics.model_seconds += max(0.0, float(seconds))

    def note_tool_call(self, capability: str, tier_name: str) -> None:
        self.metrics.record_tier(tier_name)
        if self.metrics.first_action_seconds is None:
            self.metrics.first_action_seconds = self.elapsed_seconds

    def note_vision_call(self, seconds: float) -> None:
        self.metrics.vision_seconds += max(0.0, float(seconds))

    def note_screenshot(self) -> None:
        self.metrics.screenshots += 1

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self)
        except BaseException:

            return



    def snapshot(self, *, include_events: bool = False) -> dict[str, Any]:
        """The single shape every surface renders a task from."""

        payload: dict[str, Any] = {
            "schema": TASK_RUNTIME_SCHEMA,
            "task_id": self.task_id,
            "state": self._state,
            "state_label": STATE_LABELS[self._state],
            "active": self._state in ACTIVE_STATES,
            "finished": self.finished,
            "goal": self.goal,
            "current_step": self.current_step,
            "current_capability": self.current_capability,
            "planned_step_count": len(self.current_plan) or None,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "stop_requested": self.cancellation.tripped,
            "failure": self.failure,
            "metrics": self.metrics.to_dict(),
            "world_state": self.world_state.snapshot(),


            "plan": list(self.current_plan),
            "waiting_for": self.waiting_for,
            "waiting_detail": dict(self.waiting_detail),
        }
        if include_events:
            payload["events"] = [event.to_dict() for event in self.events]
        return payload


def bind_operation_stop(context: TaskContext, stop_requested: Callable[[], bool]) -> None:
    """Bridge the operation record's stop flag onto the task's own token.

    The operation table is how the user's Stop reaches the backend, but polling
    it on every checkpoint would put a database read in the task's hot path.
    A single watcher samples it and trips the in-memory token once.
    """

    def watch() -> None:
        while not context.cancellation.tripped and not context.finished:
            try:
                if stop_requested():
                    context.request_stop("user_requested")
                    return
            except BaseException:
                return

            if context.cancellation.wait(0.1):
                return

    thread = threading.Thread(
        target=watch, name=f"salty-task-stop-{context.task_id[:8]}", daemon=True
    )
    thread.start()


def merge_world_state(
    context: TaskContext, updates: Mapping[str, Any], *, source: str = "observation"
) -> None:
    """Record structured machine state the task has already discovered."""

    for slot, value in dict(updates).items():
        context.world_state.record(slot, redact(value), source=source)
