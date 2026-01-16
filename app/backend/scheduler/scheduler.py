"""Salty Steak Native Desktop AI Platform — scheduled and event-driven work.

Work that happens across time rather than in one sitting.

A scheduled workflow gets no more authority than an interactive one. That is
the whole safety story here: running while nobody is watching is a reason for
more caution, not less, so a background inbox scan reads freely and a
background send still waits for a person exactly as it would on screen.

Nothing polls tightly. Due work is computed from timestamps and the runner
sleeps until the next one is actually due, because a scheduler that wakes every
second to discover there is nothing to do is a background CPU cost the user
paid for nothing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEDULER_SCHEMA = "salty-steak-scheduler-v1"

TRIGGER_NOW = "run_now"
TRIGGER_AT = "at_time"
TRIGGER_RECURRING = "recurring"
TRIGGER_EVENT = "external_event"
TRIGGER_RESUME = "resume_after_user"

TRIGGERS = (TRIGGER_NOW, TRIGGER_AT, TRIGGER_RECURRING, TRIGGER_EVENT, TRIGGER_RESUME)



BACKOFF_SECONDS = (60, 300, 900, 3_600)
MAX_CONSECUTIVE_FAILURES = 5


class ScheduleError(ValueError):
    """Raised when a schedule could not be created or is unrunnable."""


@dataclass
class ScheduledTask:
    """One piece of work, and when it should happen."""

    task_id: str
    name: str
    goal: str
    trigger: str

    interval_seconds: float | None = None
    next_run_at: float | None = None
    event_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    last_run_at: float | None = None
    last_result: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.trigger not in TRIGGERS:
            raise ScheduleError(f"{self.trigger!r} is not a trigger.")
        if self.trigger == TRIGGER_RECURRING and not self.interval_seconds:
            raise ScheduleError("A recurring task needs an interval.")
        if self.trigger == TRIGGER_EVENT and not self.event_name:
            raise ScheduleError("An event-driven task needs an event name.")
        if self.trigger == TRIGGER_AT and self.next_run_at is None:
            raise ScheduleError("A timed task needs a time to run.")
        if self.trigger == TRIGGER_NOW and self.next_run_at is None:
            self.next_run_at = time.time()

    @property
    def exhausted(self) -> bool:
        return self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES

    def due(self, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if not self.enabled or self.exhausted:
            return False
        if self.trigger in {TRIGGER_EVENT, TRIGGER_RESUME}:

            return False
        return self.next_run_at is not None and self.next_run_at <= now

    def schedule_next(self, *, failed: bool = False, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if failed:


            index = min(self.consecutive_failures - 1, len(BACKOFF_SECONDS) - 1)
            self.next_run_at = now + BACKOFF_SECONDS[max(0, index)]
            return
        if self.trigger == TRIGGER_RECURRING and self.interval_seconds:


            self.next_run_at = now + float(self.interval_seconds)
        else:
            self.next_run_at = None
            self.enabled = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEDULER_SCHEMA,
            "task": self.task_id,
            "name": self.name,
            "goal": self.goal,
            "trigger": self.trigger,
            "interval_seconds": self.interval_seconds,
            "next_run_at": self.next_run_at,
            "event": self.event_name,
            "enabled": self.enabled,
            "exhausted": self.exhausted,
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }


class Scheduler:
    """Hold scheduled work, decide what is due, and run it out of band."""

    def __init__(self, *, path: str | Path | None = None) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._runner: threading.Thread | None = None
        self._stopping = threading.Event()
        self.path = Path(path) if path else None
        self.history: list[dict[str, Any]] = []
        if self.path is not None:
            self._load()



    def add(
        self,
        *,
        name: str,
        goal: str,
        trigger: str,
        interval_seconds: float | None = None,
        run_at: float | datetime | None = None,
        event_name: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ScheduledTask:
        if isinstance(run_at, datetime):
            run_at = run_at.timestamp()
        task = ScheduledTask(
            task_id=str(uuid.uuid4()),
            name=str(name),
            goal=str(goal),
            trigger=trigger,
            interval_seconds=interval_seconds,
            next_run_at=run_at
            if run_at is not None
            else (time.time() + float(interval_seconds) if interval_seconds else None),
            event_name=event_name,
            payload=dict(payload or {}),
        )
        with self._lock:
            self._tasks[task.task_id] = task
        self._save()
        self._wake.set()
        return task

    def remove(self, task_id: str) -> bool:
        with self._lock:
            removed = self._tasks.pop(task_id, None) is not None
        self._save()
        return removed

    def enable(self, task_id: str, enabled: bool = True) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.enabled = enabled
            if enabled:


                task.consecutive_failures = 0
                if task.next_run_at is None and task.interval_seconds:
                    task.next_run_at = time.time() + task.interval_seconds
        self._save()
        self._wake.set()
        return True

    def get(self, task_id: str) -> ScheduledTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [task.to_dict() for task in self._tasks.values()]



    def due(self, *, now: float | None = None) -> list[ScheduledTask]:
        with self._lock:
            return [task for task in self._tasks.values() if task.due(now=now)]

    def next_due_in(self, *, now: float | None = None) -> float | None:
        """Seconds until the next scheduled run, or None if nothing is waiting."""

        now = now if now is not None else time.time()
        with self._lock:
            times = [
                task.next_run_at
                for task in self._tasks.values()
                if task.enabled and not task.exhausted and task.next_run_at is not None
            ]
        return max(0.0, min(times) - now) if times else None

    def fire_event(self, event_name: str, payload: Mapping[str, Any] | None = None) -> list[ScheduledTask]:
        """Wake everything waiting on something that just happened."""

        with self._lock:
            woken = [
                task
                for task in self._tasks.values()
                if task.trigger in {TRIGGER_EVENT, TRIGGER_RESUME}
                and task.event_name == event_name
                and task.enabled
                and not task.exhausted
            ]
            for task in woken:
                task.next_run_at = time.time()
                task.payload.update(dict(payload or {}))

                task.trigger = TRIGGER_NOW
        self._wake.set()
        return woken



    def run_due(
        self,
        runner: Callable[[ScheduledTask], Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Run everything due once. The runner supplies the actual execution.

        Nothing here decides what a workflow may do — the runner goes through
        the same policy engine as an interactive task, so background work
        cannot acquire authority by being unattended.
        """

        results = []
        for task in self.due(now=now):
            started = time.time()
            task.last_run_at = started
            try:
                outcome = dict(runner(task) or {})
                state = str(outcome.get("state") or "completed")
                task.last_result = state
                task.last_error = None


                if state in {"failed"}:
                    task.consecutive_failures += 1
                    task.schedule_next(failed=True)
                else:
                    task.consecutive_failures = 0
                    task.schedule_next()
            except Exception as error:
                task.last_result = "failed"
                task.last_error = f"{type(error).__name__}: {error}"[:500]
                task.consecutive_failures += 1
                task.schedule_next(failed=True)
                outcome = {"state": "failed", "error": task.last_error}

            record = {
                "task": task.task_id,
                "name": task.name,
                "at": started,
                "seconds": round(time.time() - started, 3),
                "result": task.last_result,
                "state": outcome.get("state"),
            }
            self.history.append(record)
            del self.history[:-200]
            results.append(record)
        self._save()
        return results

    def start(
        self,
        runner: Callable[[ScheduledTask], Mapping[str, Any]],
        *,
        idle_seconds: float = 60.0,
    ) -> None:
        """Run due work in the background, sleeping until something is due."""

        if self._runner is not None:
            return
        self._stopping.clear()

        def loop() -> None:
            while not self._stopping.is_set():
                try:
                    self.run_due(runner)
                except Exception:

                    pass
                delay = self.next_due_in()


                self._wake.wait(min(delay, idle_seconds) if delay is not None else idle_seconds)
                self._wake.clear()

        self._runner = threading.Thread(
            target=loop, name="salty-scheduler", daemon=True
        )
        self._runner.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self._runner is not None:
            self._runner.join(timeout=timeout)
            self._runner = None



    def _save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = {
                "schema": SCHEDULER_SCHEMA,
                "tasks": [task.to_dict() | {"payload": task.payload} for task in self._tasks.values()],
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".partial")
            temporary.write_text(json.dumps(payload, default=str), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            return

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for entry in payload.get("tasks", []):
            try:
                task = ScheduledTask(
                    task_id=entry["task"],
                    name=entry.get("name", ""),
                    goal=entry.get("goal", ""),
                    trigger=entry.get("trigger", TRIGGER_NOW),
                    interval_seconds=entry.get("interval_seconds"),
                    next_run_at=entry.get("next_run_at"),
                    event_name=entry.get("event"),
                    payload=dict(entry.get("payload") or {}),
                )
            except ScheduleError:
                continue
            task.enabled = bool(entry.get("enabled", True))
            task.last_run_at = entry.get("last_run_at")
            task.last_result = entry.get("last_result")
            task.consecutive_failures = int(entry.get("consecutive_failures") or 0)
            self._tasks[task.task_id] = task


__all__ = [
    "BACKOFF_SECONDS",
    "MAX_CONSECUTIVE_FAILURES",
    "SCHEDULER_SCHEMA",
    "ScheduleError",
    "ScheduledTask",
    "Scheduler",
    "TRIGGERS",
    "TRIGGER_AT",
    "TRIGGER_EVENT",
    "TRIGGER_NOW",
    "TRIGGER_RECURRING",
    "TRIGGER_RESUME",
]
