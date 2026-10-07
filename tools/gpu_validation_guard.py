"""Keep developer validation from competing with a user's live GPU work."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

import psutil


GPU_OPERATION_TYPES = (
    "chat_generation",
    "chat_image_generation",
    "chat_vision_analysis",
    "training",
    "training_identity",
    "evaluation",
    "evaluation_activation",
    "post_training_recovery",
    "training_finalisation_retry",
)
ACTIVE_STATES = ("queued", "running", "stop_requested")
GPU_PYTHON_MARKERS = (
    "acceptance",
    "benchmark",
    "batch",
    "cuda",
    "engine",
    "generate",
    "inference",
    "model",
    "torch",
    "train",
    "worker",
)


def active_user_gpu_operations(workspace: str | Path) -> list[dict[str, Any]]:
    """Return durable user operations that may own the accelerator.

    Validation runs from a separate process and therefore cannot see the
    installed application's in-process lifecycle lock.  The production
    database is the cross-process source of truth: if it says a generation,
    image, vision, training, or evaluation operation is active, developer
    validation must not start another model worker.
    """

    root = Path(workspace).resolve()
    database = root / "control" / "salty-potato.db"
    if not database.is_file():
        return []
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=2.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()
        if exists is None:
            return []
        operation_placeholders = ",".join("?" for _ in GPU_OPERATION_TYPES)
        state_placeholders = ",".join("?" for _ in ACTIVE_STATES)
        rows = connection.execute(
            f"""
            SELECT id, type, state, phase, created_at, started_at,
                   heartbeat_at, updated_at
            FROM operations
            WHERE type IN ({operation_placeholders})
              AND state IN ({state_placeholders})
            ORDER BY created_at
            """,
            (*GPU_OPERATION_TYPES, *ACTIVE_STATES),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def external_gpu_python_workloads(
    process_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    current_pid: int | None = None,
) -> list[dict[str, Any]]:
    """Find other Python model/benchmark processes without exposing arguments.

    Windows WDDM often withholds per-process VRAM from ``nvidia-smi``. A live
    external Python inference/training process can therefore be invisible to
    the GPU process table while still crashing an 8 GB image render. We use a
    conservative command-shape check and report only executable/script names,
    never the full argument string where credentials could appear.
    """

    owner = int(current_pid or os.getpid())
    excluded_pids = {owner}
    if process_rows is None:
        try:
            excluded_pids.update(
                int(process.pid) for process in psutil.Process(owner).parents()
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
    if process_rows is None:
        rows: list[Mapping[str, Any]] = []
        for process in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                rows.append(dict(process.info))
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
    else:
        rows = list(process_rows)
    found: list[dict[str, Any]] = []
    for row in rows:
        try:
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in excluded_pids:
            continue
        name = str(row.get("name") or "").strip()
        executable = str(row.get("exe") or "").strip()
        if name.casefold() not in {"python", "python.exe", "pythonw.exe"}:
            continue
        raw_command = row.get("cmdline") or []
        command = (
            [str(value) for value in raw_command]
            if isinstance(raw_command, (list, tuple))
            else [str(raw_command)]
        )
        searchable = " ".join(command).casefold()
        if not any(marker in searchable for marker in GPU_PYTHON_MARKERS):
            continue
        script = next(
            (
                Path(value).name
                for value in command[1:]
                if value and not value.startswith("-")
            ),
            "python workload",
        )
        found.append(
            {
                "pid": pid,
                "name": name or "python",
                "executable": Path(executable).name if executable else "python",
                "workload": script[:160],
            }
        )
    return sorted(found, key=lambda row: int(row["pid"]))


def gpu_validation_preflight(
    workspace: str | Path,
    *,
    include_external: bool = True,
    process_rows: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    active = active_user_gpu_operations(workspace)
    external = (
        external_gpu_python_workloads(process_rows)
        if include_external
        else []
    )
    return {
        "schema": "salty-steak-gpu-validation-preflight-v1",
        "workspace": str(Path(workspace).resolve()),
        "idle": not active and not external,
        "active_operations": active,
        "external_python_workloads": external,
        "policy": "developer_validation_never_competes_with_live_user_gpu_work",
    }
