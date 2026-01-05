"""Durable checkpoint-finalisation attempts and restart-safe evidence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..database.control import Database, json_text, new_id, utc_now
from ..worker_bootstrap import package_build_id
from .checkpoint import IsolatedWorkerError, isolated_smoke_test


def _deadline(seconds: float) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=float(seconds))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pid_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class FinalisationCoordinator:
    """Own immutable attempt IDs and reject every late or stale result."""

    def __init__(
        self,
        database: Database,
        results_root: str | Path,
        *,
        bootstrap_timeout_seconds: float = 10,
        worker_start_timeout_seconds: float = 30,
        integrity_timeout_seconds: float = 180,
        model_load_timeout_seconds: float = 900,
        verification_timeout_seconds: float = 1080,
    ) -> None:
        self.database = database
        self.results_root = Path(results_root).resolve()
        self.bootstrap_timeout_seconds = float(bootstrap_timeout_seconds)
        self.worker_start_timeout_seconds = float(worker_start_timeout_seconds)
        self.integrity_timeout_seconds = float(integrity_timeout_seconds)
        self.model_load_timeout_seconds = float(model_load_timeout_seconds)
        self.verification_timeout_seconds = float(verification_timeout_seconds)

    def verify(
        self,
        *,
        operation_id: str,
        checkpoint_path: str | Path,
        context_limit: int,
        device: str = "cpu",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        checkpoint = Path(checkpoint_path).resolve()
        attempt_id = new_id()
        checkpoint_id = checkpoint.name
        build_id = package_build_id(Path(__file__).resolve().parents[3])
        result_path = (
            self.results_root / operation_id / f"{attempt_id}.json"
        ).resolve()
        started_at = utc_now()
        deadline_at = _deadline(self.verification_timeout_seconds)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO finalisation_attempts(
                    id, operation_id, checkpoint_id, checkpoint_path,
                    result_path, package_build_id, started_at, deadline_at,
                    heartbeat_at, phase, result_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'interpreter_bootstrap', 'running')
                """,
                (
                    attempt_id,
                    operation_id,
                    checkpoint_id,
                    str(checkpoint),
                    str(result_path),
                    build_id,
                    started_at,
                    deadline_at,
                    started_at,
                ),
            )
            self.database.append_operation_event(
                operation_id,
                "finalisation_attempt_started",
                state="running",
                phase="Interpreter bootstrap",
                evidence={
                    "verification_attempt_id": attempt_id,
                    "checkpoint_id": checkpoint_id,
                    "result_path": str(result_path),
                    "deadline_at": deadline_at,
                    "package_build_id": build_id,
                },
                connection=connection,
                created_at=started_at,
            )

        def update_phase(phase: str) -> None:
            now = utc_now()
            self.database.execute(
                """
                UPDATE finalisation_attempts
                SET phase = ?, heartbeat_at = ?
                WHERE id = ? AND result_status = 'running'
                  AND invalidated_at IS NULL
                """,
                (phase, now, attempt_id),
            )

        def record_pid(pid: int) -> None:
            self.database.execute(
                """
                UPDATE finalisation_attempts
                SET worker_pid = ?, heartbeat_at = ?, phase = 'checkpoint_verification'
                WHERE id = ? AND result_status = 'running'
                  AND invalidated_at IS NULL
                """,
                (pid, utc_now(), attempt_id),
            )

        def attempt_is_valid() -> bool:
            row = self.database.fetch_one(
                """
                SELECT result_status, invalidated_at
                FROM finalisation_attempts WHERE id = ?
                """,
                (attempt_id,),
            )
            return bool(
                row
                and row["result_status"] == "running"
                and row["invalidated_at"] is None
            )

        try:
            result = isolated_smoke_test(
                checkpoint,
                context_limit=context_limit,
                device=device,
                timeout_seconds=self.verification_timeout_seconds,
                bootstrap_timeout_seconds=self.bootstrap_timeout_seconds,
                worker_start_timeout_seconds=self.worker_start_timeout_seconds,
                integrity_timeout_seconds=self.integrity_timeout_seconds,
                model_load_timeout_seconds=self.model_load_timeout_seconds,
                verification_attempt_id=attempt_id,
                operation_id=operation_id,
                checkpoint_id=checkpoint_id,
                package_build_id_value=build_id,
                result_path=result_path,
                process_started=record_pid,
                phase_changed=update_phase,
                attempt_is_valid=attempt_is_valid,
            )
        except BaseException as error:
            timed_out_phase = None
            diagnostics: dict[str, Any] = {}
            if isinstance(error, IsolatedWorkerError):
                diagnostics = dict(error.diagnostics)
                timed_out_phase = diagnostics.get("timed_out_phase")
            now = utc_now()
            status = "timed_out" if timed_out_phase else "failed"
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE finalisation_attempts
                    SET result_status = ?, finished_at = ?, heartbeat_at = ?,
                        phase = ?,
                        invalidated_at = CASE WHEN ? = 'timed_out' THEN ? ELSE invalidated_at END,
                        invalidation_reason = CASE WHEN ? = 'timed_out'
                            THEN 'deadline exceeded; worker process tree terminated'
                            ELSE invalidation_reason END
                    WHERE id = ? AND result_status = 'running'
                    """,
                    (
                        status,
                        now,
                        now,
                        timed_out_phase or "verification_failed",
                        status,
                        now,
                        status,
                        attempt_id,
                    ),
                )
                self.database.append_operation_event(
                    operation_id,
                    "finalisation_timed_out" if timed_out_phase else "finalisation_failed",
                    state="failed",
                    phase=str(timed_out_phase or "Version verification failed"),
                    evidence={
                        "verification_attempt_id": attempt_id,
                        "checkpoint_id": checkpoint_id,
                        "result_path": str(result_path),
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                        **diagnostics,
                    },
                    connection=connection,
                    created_at=now,
                )
            raise

        finished_at = utc_now()
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM finalisation_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if (
                current is None
                or current["result_status"] != "running"
                or current["invalidated_at"] is not None
            ):
                raise RuntimeError(
                    "verification result belongs to an invalidated finalisation attempt"
                )
            connection.execute(
                """
                UPDATE finalisation_attempts
                SET result_status = 'passed', phase = 'verified',
                    finished_at = ?, heartbeat_at = ?
                WHERE id = ?
                """,
                (finished_at, finished_at, attempt_id),
            )
            self.database.append_operation_event(
                operation_id,
                "verification_passed",
                state="running",
                phase="Version verified",
                evidence={
                    "verification_attempt_id": attempt_id,
                    "checkpoint_id": checkpoint_id,
                    "result_path": str(result_path),
                    "worker_pid": result["worker_pid"],
                    "package_build_id": build_id,
                },
                connection=connection,
                created_at=finished_at,
            )
        attempt = self.database.fetch_one(
            "SELECT * FROM finalisation_attempts WHERE id = ?", (attempt_id,)
        )
        assert attempt is not None
        return result, attempt

    def require_passed(
        self,
        *,
        attempt_id: str,
        operation_id: str,
        checkpoint_path: str | Path,
    ) -> dict[str, Any]:
        attempt = self.database.fetch_one(
            """
            SELECT * FROM finalisation_attempts
            WHERE id = ? AND operation_id = ? AND checkpoint_path = ?
              AND result_status = 'passed' AND invalidated_at IS NULL
            """,
            (attempt_id, operation_id, str(Path(checkpoint_path).resolve())),
        )
        if not attempt:
            raise ValueError("registration requires the current passed verification attempt")
        result_path = Path(attempt["result_path"])
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("the verification result is missing or unreadable") from error
        expected = {
            "success": True,
            "verification_attempt_id": attempt_id,
            "operation_id": operation_id,
            "checkpoint_id": Path(checkpoint_path).resolve().name,
            "checkpoint_path": str(Path(checkpoint_path).resolve()),
            "package_build_id": attempt["package_build_id"],
            "manifest_valid": True,
            "weights_exist": True,
            "checksums_valid": True,
            "load_valid": True,
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise ValueError("the committed verification result identity is inconsistent")
        return {"attempt": attempt, "result": result}

    def invalidate_orphaned_attempts(self) -> int:
        rows = self.database.fetch_all(
            """
            SELECT id, operation_id, worker_pid, checkpoint_id
            FROM finalisation_attempts
            WHERE result_status = 'running' AND invalidated_at IS NULL
            """
        )
        invalidated = 0
        for row in rows:
            if _pid_is_alive(row.get("worker_pid")):
                continue
            now = utc_now()
            with self.database.transaction() as connection:
                changed = connection.execute(
                    """
                    UPDATE finalisation_attempts
                    SET result_status = 'invalidated', invalidated_at = ?,
                        invalidation_reason = 'application restarted without a live owner',
                        finished_at = ?, heartbeat_at = ?, phase = 'invalidated'
                    WHERE id = ? AND result_status = 'running'
                      AND invalidated_at IS NULL
                    """,
                    (now, now, now, row["id"]),
                ).rowcount
                if changed:
                    self.database.append_operation_event(
                        row["operation_id"],
                        "finalisation_attempt_invalidated",
                        state="failed",
                        phase="Finalisation requires attention",
                        evidence={
                            "verification_attempt_id": row["id"],
                            "checkpoint_id": row["checkpoint_id"],
                            "reason": "application restarted without a live owner",
                        },
                        connection=connection,
                        created_at=now,
                    )
                    invalidated += 1
        return invalidated
