from __future__ import annotations

from pathlib import Path

from app.backend.database.control import Database, new_id, utc_now
from tools.gpu_validation_guard import gpu_validation_preflight


def test_gpu_validation_is_blocked_by_a_live_user_image_operation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database = Database(workspace / "control" / "salty-potato.db")
    now = utc_now()
    operation_id = new_id()
    database.execute(
        """
        INSERT INTO operations(
            id, type, target_id, dedupe_key, state, phase,
            current_progress, created_at, updated_at, started_at
        ) VALUES (
            ?, 'chat_image_generation', 'conversation',
            'image:live', 'running', 'Denoising', 4, ?, ?, ?
        )
        """,
        (operation_id, now, now, now),
    )

    report = gpu_validation_preflight(workspace, include_external=False)

    assert report["idle"] is False
    assert report["active_operations"] == [
        {
            "id": operation_id,
            "type": "chat_image_generation",
            "state": "running",
            "phase": "Denoising",
            "created_at": now,
            "started_at": now,
            "heartbeat_at": None,
            "updated_at": now,
        }
    ]


def test_gpu_validation_ignores_completed_and_non_gpu_operations(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database = Database(workspace / "control" / "salty-potato.db")
    now = utc_now()
    for operation_id, operation_type, state, finished_at in (
        (new_id(), "chat_image_generation", "completed", now),
        (new_id(), "file_cleanup", "running", None),
    ):
        database.execute(
            """
            INSERT INTO operations(
                id, type, target_id, dedupe_key, state, phase,
                current_progress, created_at, updated_at, finished_at
            ) VALUES (?, ?, 'target', ?, ?, 'Working', 0, ?, ?, ?)
            """,
            (
                operation_id,
                operation_type,
                operation_id,
                state,
                now,
                now,
                finished_at,
            ),
        )

    assert gpu_validation_preflight(workspace, include_external=False)["idle"] is True


def test_gpu_validation_detects_external_python_model_work_without_leaking_arguments(
    tmp_path: Path,
) -> None:
    report = gpu_validation_preflight(
        tmp_path / "workspace",
        process_rows=[
            {
                "pid": 4242,
                "name": "python.exe",
                "exe": r"C:\Python314\python.exe",
                "cmdline": [
                    r"C:\Python314\python.exe",
                    "engine.py",
                    "--api-key",
                    "secret-value",
                    "--batch",
                    "3",
                ],
            }
        ],
    )

    assert report["idle"] is False
    assert report["active_operations"] == []
    assert report["external_python_workloads"] == [
        {
            "pid": 4242,
            "name": "python.exe",
            "executable": "python.exe",
            "workload": "engine.py",
        }
    ]
    assert "secret-value" not in str(report)
