"""One persistent operation state machine for every long-running workflow."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from app.backend.database.control import (
    ACTIVE_OPERATION_STATES,
    OPERATION_STATES,
    TERMINAL_OPERATION_STATES,
    Database,
    StateConflict,
    json_text,
    new_id,
    parse_json,
    utc_now,
)
from app.backend.system.device import DeviceConfigurationError


class OperationInterrupted(RuntimeError):
    """Raised by a worker after observing a persisted safe-stop request."""


class OperationPhaseError(RuntimeError):
    """A plain operation failure with a precise persisted terminal phase."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        phase: str,
        technical_details: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.technical_details = technical_details


class OperationNeedsAttention(OperationPhaseError):
    """A durable partial result exists, but the requested workflow is incomplete."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        phase: str,
        technical_details: str,
        partial_result: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            phase=phase,
            technical_details=technical_details,
        )
        self.partial_result = dict(partial_result or {})


@dataclass(frozen=True, slots=True)
class Notification:
    kind: str
    title: str
    message: str
    duration_seconds: int | None = 10


class OperationContext:
    """The only interface a background task needs for progress and safe stop."""

    def __init__(self, manager: "OperationManager", operation_id: str) -> None:
        self.manager = manager
        self.operation_id = operation_id

    def record(self) -> dict[str, Any]:
        record = self.manager.get(self.operation_id)
        if record is None:
            raise KeyError(f"Operation does not exist: {self.operation_id}")
        return record

    def stop_requested(self) -> bool:
        return self.record()["state"] == "stop_requested"

    def raise_if_stop_requested(self) -> None:
        if self.stop_requested():
            raise OperationInterrupted("A safe stop was requested")

    def update(
        self,
        *,
        phase: str | None = None,
        current_progress: float | None = None,
        total_progress: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.manager.update_progress(
            self.operation_id,
            phase=phase,
            current_progress=current_progress,
            total_progress=total_progress,
            details=details,
        )

    def checkpoint(
        self,
        *,
        phase: str | None = None,
        current_progress: float | None = None,
        total_progress: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.update(
            phase=phase,
            current_progress=current_progress,
            total_progress=total_progress,
            details=details,
        )
        if record["state"] == "stop_requested":
            raise OperationInterrupted("A safe stop was requested")
        return record


Worker = Callable[[OperationContext], Any]
SuccessNotification = Notification | Callable[[Any], Notification | None]
TRAINING_TELEMETRY_EVENT_TYPE = "training_telemetry"
TRAINING_TELEMETRY_EVENT_LIMIT = 2000
STOPPABLE_OPERATION_TYPES = frozenset(
    {
        "training",
        "evaluation",
        "evaluation_activation",
        "post_training_recovery",
        "training_finalisation_retry",
        "dataset_validation",
        "dataset_preparation",
        "chat_generation",
        "chat_vision_analysis",
        "chat_image_generation",
    }
)


class OperationManager:
    """Runs short dispatches in threads while SQLite remains the source of truth."""

    def __init__(
        self,
        database: Database,
        *,
        max_workers: int = 2,
        recover_incomplete: bool = True,
        worker_start_timeout_seconds: float = 30.0,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if worker_start_timeout_seconds <= 0:
            raise ValueError("worker_start_timeout_seconds must be positive")
        self.database = database
        self._max_workers = max_workers
        self._worker_start_timeout_seconds = float(worker_start_timeout_seconds)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="salty-operation",
        )
        self._futures: dict[str, Future[Any]] = {}
        self._queue_timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._closed = False
        if recover_incomplete:
            self.recover_abandoned()

    def recover_abandoned(self) -> int:
        """Mark work that cannot survive an application process restart."""

        now = utc_now()
        error = json_text(
            {
                "code": "application_restarted",
                "message": "The application stopped before this operation finished.",
            }
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE operations
                SET state = 'interrupted',
                    phase = 'Interrupted',
                    error_json = ?,
                    finished_at = ?,
                    updated_at = ?,
                    heartbeat_at = ?
                WHERE state IN ('queued','running','stop_requested')
                """,
                (error, now, now, now),
            )
            return cursor.rowcount

    def submit(
        self,
        operation_type: str,
        worker: Worker,
        *,
        target_id: str | None = None,
        dedupe_key: str | None = None,
        initial_phase: str = "Waiting",
        total_progress: float | None = None,
        initial_details: Mapping[str, Any] | None = None,
        success_notification: SuccessNotification | None = None,
        failure_notification: Notification | None = None,
    ) -> dict[str, Any]:
        """Persist and enqueue work, returning without waiting for the work."""

        if self._closed:
            raise RuntimeError("Operation manager is closed")
        record, created = self.database.create_operation(
            operation_type,
            target_id=target_id,
            dedupe_key=dedupe_key,
            phase=initial_phase,
            total_progress=total_progress,
        )
        if not created:
            return record
        with self._lock:
            occupied_workers = sum(
                1 for future in self._futures.values() if not future.done()
            )
        queue_reason = (
            "Waiting for an available local worker."
            if occupied_workers >= self._max_workers
            else "The local worker is starting."
        )
        record = self.update_progress(
            record["id"],
            phase=initial_phase,
            details={
                "queue_reason": queue_reason,
                **dict(initial_details or {}),
            },
        )
        future = self._executor.submit(
            self._run,
            record["id"],
            worker,
            success_notification,
            failure_notification,
        )
        timer = threading.Timer(
            self._worker_start_timeout_seconds,
            self._expire_queued,
            args=(record["id"],),
        )
        timer.daemon = True
        with self._lock:
            self._futures[record["id"]] = future
            self._queue_timers[record["id"]] = timer
        future.add_done_callback(
            lambda _future, operation_id=record["id"]: self._forget(operation_id)
        )
        timer.start()
        return record

    def create(
        self,
        operation_type: str,
        *,
        target_id: str | None = None,
        dedupe_key: str | None = None,
        initial_phase: str = "Waiting",
        total_progress: float | None = None,
    ) -> dict[str, Any]:
        """Create a persisted operation for a separately managed worker process."""

        record, _created = self.database.create_operation(
            operation_type,
            target_id=target_id,
            dedupe_key=dedupe_key,
            phase=initial_phase,
            total_progress=total_progress,
        )
        return record

    def _forget(self, operation_id: str) -> None:
        with self._lock:
            self._futures.pop(operation_id, None)
            timer = self._queue_timers.pop(operation_id, None)
        if timer is not None:
            timer.cancel()

    def _expire_queued(self, operation_id: str) -> None:
        with self._lock:
            future = self._futures.get(operation_id)
        record = self.get(operation_id)
        if (
            future is None
            or record is None
            or record["state"] != "queued"
            or not future.cancel()
        ):
            return
        operation_name = (
            "Evaluation"
            if record["type"] == "evaluation"
            else str(record["type"]).replace("_", " ").capitalize()
        )
        self.fail(
            operation_id,
            error={
                "code": "worker_not_started",
                "message": (
                    f"{operation_name} could not start because no local worker "
                    "became available."
                ),
                "technical_details": (
                    "The operation remained queued for "
                    f"{self._worker_start_timeout_seconds:g} seconds."
                ),
            },
        )

    def _run(
        self,
        operation_id: str,
        worker: Worker,
        success_notification: SuccessNotification | None,
        failure_notification: Notification | None,
    ) -> None:
        context = OperationContext(self, operation_id)
        try:
            started = self.mark_running(operation_id, worker_pid=os.getpid())
            if started["state"] == "stop_requested":
                raise OperationInterrupted("Stopped before work began")
            result = worker(context)
            context.raise_if_stop_requested()
            resolved_notification = (
                success_notification(result)
                if callable(success_notification)
                else success_notification
            )
            self.complete(
                operation_id,
                result=result,
                notification=resolved_notification,
            )
        except OperationInterrupted as exc:
            self.interrupt(
                operation_id,
                error={"code": "stop_requested", "message": str(exc)},
            )
        except BaseException as exc:
            if isinstance(exc, OperationPhaseError):
                error = {
                    "code": exc.code,
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "failed_phase": exc.phase,
                    "technical_details": exc.technical_details,
                }
                failure_phase = exc.phase
                partial_result = (
                    exc.partial_result
                    if isinstance(exc, OperationNeedsAttention)
                    else None
                )
            else:
                raw = f"{type(exc).__name__}: {exc}"
                message = (
                    str(exc)
                    if isinstance(
                        exc,
                        (DeviceConfigurationError, ValueError, FileNotFoundError),
                    )
                    else "The operation could not complete."
                )
                error = {
                    "type": type(exc).__name__,
                    "message": message,
                    "technical_details": raw,
                }
                failure_phase = "Failed"
                partial_result = None
            self.fail(
                operation_id,
                error=error,
                result=partial_result,
                phase=failure_phase,
                notification=failure_notification,
            )

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self.database.get_operation(operation_id)

    def list(
        self,
        *,
        states: tuple[str, ...] | None = None,
        operation_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.database.list_operations(
            states=states,
            operation_type=operation_type,
            limit=limit,
        )

    def mark_running(
        self,
        operation_id: str,
        *,
        worker_pid: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        pid = worker_pid if worker_pid is not None else os.getpid()
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT state FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Operation does not exist: {operation_id}")
            if current["state"] == "stop_requested":
                row = connection.execute(
                    "SELECT * FROM operations WHERE id = ?", (operation_id,)
                ).fetchone()
                assert row is not None
                return self.database.operation_record(row)
            cursor = connection.execute(
                """
                UPDATE operations
                SET state = 'running',
                    worker_pid = ?,
                    started_at = COALESCE(started_at, ?),
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (pid, now, now, now, operation_id),
            )
            if cursor.rowcount != 1:
                raise StateConflict(f"Operation {operation_id} is not queued")
            self.database.append_operation_event(
                operation_id,
                "operation_started",
                state="running",
                phase="Running",
                evidence={"worker_pid": pid},
                connection=connection,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            assert row is not None
            return self.database.operation_record(row)

    def update_progress(
        self,
        operation_id: str,
        *,
        phase: str | None = None,
        current_progress: float | None = None,
        total_progress: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if current_progress is not None and current_progress < 0:
            raise ValueError("current_progress cannot be negative")
        if total_progress is not None and total_progress < 0:
            raise ValueError("total_progress cannot be negative")
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Operation does not exist: {operation_id}")
            if row["state"] not in ACTIVE_OPERATION_STATES:
                raise StateConflict(f"Operation {operation_id} is already terminal")
            new_current = (
                float(current_progress)
                if current_progress is not None
                else float(row["current_progress"])
            )
            new_total = (
                float(total_progress)
                if total_progress is not None
                else row["total_progress"]
            )
            if new_total is not None and new_current > float(new_total):
                raise ValueError("current_progress cannot exceed total_progress")
            existing_details = parse_json(row["result_json"], {})
            if not isinstance(existing_details, dict):
                existing_details = {}
            if details:
                existing_details.update(details)
            effective_phase = (
                "Stop requested"
                if row["state"] == "stop_requested"
                else (phase if phase is not None else row["phase"])
            )
            connection.execute(
                """
                UPDATE operations
                SET phase = ?,
                    current_progress = ?,
                    total_progress = ?,
                    result_json = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    effective_phase,
                    new_current,
                    new_total,
                    json_text(existing_details) if existing_details else None,
                    now,
                    now,
                    operation_id,
                ),
            )
            if effective_phase != row["phase"]:
                self.database.append_operation_event(
                    operation_id,
                    "phase_changed",
                    state=row["state"],
                    phase=effective_phase,
                    evidence={
                        "current_progress": new_current,
                        "total_progress": new_total,
                        "details": dict(details or {}),
                    },
                    connection=connection,
                    created_at=now,
                )
            if row["type"] == "training" and (
                phase is not None
                or current_progress is not None
                or total_progress is not None
                or details is not None
            ):
                self.database.append_operation_event(
                    operation_id,
                    TRAINING_TELEMETRY_EVENT_TYPE,
                    state=row["state"],
                    phase=effective_phase,
                    evidence={
                        "current_progress": new_current,
                        "total_progress": new_total,
                        "details": dict(details or {}),
                    },
                    connection=connection,
                    created_at=now,
                )




                cutoff = connection.execute(
                    """
                    SELECT sequence
                    FROM operation_events
                    WHERE operation_id = ? AND event_type = ?
                    ORDER BY sequence DESC
                    LIMIT 1 OFFSET ?
                    """,
                    (
                        operation_id,
                        TRAINING_TELEMETRY_EVENT_TYPE,
                        TRAINING_TELEMETRY_EVENT_LIMIT,
                    ),
                ).fetchone()
                if cutoff is not None:
                    connection.execute(
                        """
                        DELETE FROM operation_events
                        WHERE operation_id = ?
                          AND event_type = ?
                          AND sequence <= ?
                        """,
                        (
                            operation_id,
                            TRAINING_TELEMETRY_EVENT_TYPE,
                            int(cutoff["sequence"]),
                        ),
                    )
            updated = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            assert updated is not None
            return self.database.operation_record(updated)

    def request_stop(self, operation_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Operation does not exist: {operation_id}")
            if row["state"] in TERMINAL_OPERATION_STATES or row["state"] == "stop_requested":
                return self.database.operation_record(row)
            if row["type"] not in STOPPABLE_OPERATION_TYPES:


                return self.database.operation_record(row)
            if (
                row["total_progress"] is not None
                and float(row["current_progress"])
                >= float(row["total_progress"])
            ):



                return self.database.operation_record(row)
            connection.execute(
                """
                UPDATE operations
                SET state = 'stop_requested',
                    phase = 'Stop requested',
                    updated_at = ?,
                    heartbeat_at = ?
                WHERE id = ? AND state IN ('queued','running')
                """,
                (now, now, operation_id),
            )
            self.database.append_operation_event(
                operation_id,
                "stop_requested",
                state="stop_requested",
                phase="Stop requested",
                connection=connection,
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            assert updated is not None
            return self.database.operation_record(updated)

    def complete(
        self,
        operation_id: str,
        *,
        result: Any = None,
        notification: Notification | None = None,
    ) -> dict[str, Any]:
        return self._finish(
            operation_id,
            state="completed",
            phase="Completed",
            result=result,
            error=None,
            notification=notification,
        )

    def interrupt(
        self,
        operation_id: str,
        *,
        error: Any = None,
        notification: Notification | None = None,
    ) -> dict[str, Any]:
        return self._finish(
            operation_id,
            state="interrupted",
            phase="Interrupted",
            result=None,
            error=error,
            notification=notification,
        )

    def fail(
        self,
        operation_id: str,
        *,
        error: Any,
        result: Any = None,
        phase: str = "Failed",
        notification: Notification | None = None,
    ) -> dict[str, Any]:
        return self._finish(
            operation_id,
            state="failed",
            phase=phase,
            result=result,
            error=error,
            notification=notification,
        )

    def _finish(
        self,
        operation_id: str,
        *,
        state: str,
        phase: str,
        result: Any,
        error: Any,
        notification: Notification | None,
    ) -> dict[str, Any]:
        if state not in TERMINAL_OPERATION_STATES:
            raise ValueError(f"Not a terminal operation state: {state}")
        now = utc_now()
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Operation does not exist: {operation_id}")
            if current["state"] in TERMINAL_OPERATION_STATES:
                if current["state"] != state:
                    raise StateConflict(
                        f"Operation {operation_id} already finished as {current['state']}"
                    )
                return self.database.operation_record(current)
            if state == "completed" and current["state"] == "stop_requested":
                raise StateConflict(
                    f"Operation {operation_id} has a pending safe-stop request"
                )
            current_progress = current["current_progress"]
            if state == "completed" and current["total_progress"] is not None:
                current_progress = current["total_progress"]
            if result is None:
                persisted_result = current["result_json"]
            elif isinstance(result, Mapping):
                existing_result = parse_json(current["result_json"], {})
                if not isinstance(existing_result, dict):
                    existing_result = {}
                existing_result.pop("queue_reason", None)
                existing_result.update(result)
                persisted_result = json_text(existing_result)
            else:
                persisted_result = json_text(result)
            connection.execute(
                """
                UPDATE operations
                SET state = ?,
                    phase = ?,
                    current_progress = ?,
                    result_json = ?,
                    error_json = ?,
                    finished_at = ?,
                    heartbeat_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND state IN ('queued','running','stop_requested')
                """,
                (
                    state,
                    phase,
                    current_progress,
                    persisted_result,
                    json_text(error) if error is not None else None,
                    now,
                    now,
                    now,
                    operation_id,
                ),
            )
            self.database.append_operation_event(
                operation_id,
                (
                    "operation_completed"
                    if state == "completed"
                    else "operation_interrupted"
                    if state == "interrupted"
                    else "operation_failed"
                ),
                state=state,
                phase=phase,
                evidence={"result": result, "error": error},
                connection=connection,
                created_at=now,
            )
            if notification is not None:
                self._insert_notification(
                    connection,
                    operation_id,
                    state,
                    notification,
                    now,
                )
            updated = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            assert updated is not None
            return self.database.operation_record(updated)

    @staticmethod
    def _insert_notification(
        connection: sqlite3.Connection,
        operation_id: str,
        state: str,
        notification: Notification,
        now: str,
    ) -> None:
        if notification.kind not in {"success", "information", "warning", "error"}:
            raise ValueError(f"Invalid notification kind: {notification.kind}")
        connection.execute(
            """
            INSERT OR IGNORE INTO notifications(
                id, operation_id, kind, title, message, dedupe_key,
                duration_seconds, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                operation_id,
                notification.kind,
                notification.title,
                notification.message,
                f"operation:{operation_id}:{state}",
                notification.duration_seconds,
                now,
            ),
        )

    def wait(self, operation_id: str, *, timeout: float = 30) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            record = self.get(operation_id)
            if record is None:
                raise KeyError(f"Operation does not exist: {operation_id}")
            if record["state"] in TERMINAL_OPERATION_STATES:
                return record
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Operation {operation_id} did not finish in time")
            time.sleep(0.01)

    def shutdown(self, *, wait: bool = True, request_stop: bool = False) -> None:
        self._closed = True
        with self._lock:
            timers = list(self._queue_timers.values())
        for timer in timers:
            timer.cancel()
        if request_stop:
            for operation in self.list(states=tuple(ACTIVE_OPERATION_STATES), limit=1000):
                self.request_stop(operation["id"])
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def __enter__(self) -> "OperationManager":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.shutdown()
