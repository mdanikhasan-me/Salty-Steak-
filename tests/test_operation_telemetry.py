from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.backend.operations.manager as manager_module
from app.backend.application import Application
from app.backend.database.control import Database
from app.backend.operations.manager import (
    TRAINING_TELEMETRY_EVENT_TYPE,
    OperationManager,
)


def test_training_progress_persists_structured_bounded_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry_limit = 5
    monkeypatch.setattr(
        manager_module,
        "TRAINING_TELEMETRY_EVENT_LIMIT",
        telemetry_limit,
    )
    database = Database(tmp_path / "salty-potato.db")
    manager = OperationManager(database, recover_incomplete=False)
    try:
        operation = manager.create(
            "training",
            target_id="prepared-dataset",
            initial_phase="Waiting",
            total_progress=3000,
        )
        for step in range(telemetry_limit + 3):
            manager.update_progress(
                operation["id"],
                phase="Training",
                current_progress=step,
                total_progress=3000,
                details={
                    "current_training_step": step,
                    "training_loss": 3.0 - (step / 10000),
                    "tokens_per_second": 1250.5,
                },
            )

        rows = database.fetch_all(
            """
            SELECT sequence, evidence_json
            FROM operation_events
            WHERE operation_id = ? AND event_type = ?
            ORDER BY sequence
            """,
            (operation["id"], TRAINING_TELEMETRY_EVENT_TYPE),
        )
        assert len(rows) == telemetry_limit
        assert rows[0]["sequence"] > 1

        application = Application.__new__(Application)
        application.database = database
        application.operations = SimpleNamespace(get=manager.get)
        first_page = application.operation_events(
            operation["id"],
            after_sequence=0,
            limit=3,
        )
        assert first_page["has_more"] is True
        assert len(first_page["events"]) == 3
        assert first_page["events"][0]["event_type"] == "operation_queued"

        telemetry_page = application.operation_events(
            operation["id"],
            after_sequence=rows[-2]["sequence"],
            limit=10,
        )
        assert telemetry_page["has_more"] is False
        assert telemetry_page["next_after_sequence"] == rows[-1]["sequence"]
        assert telemetry_page["events"][0]["evidence"]["details"][
            "current_training_step"
        ] == telemetry_limit + 2
    finally:
        manager.shutdown(wait=True, request_stop=False)


def test_operation_events_rejects_unbounded_or_unknown_requests(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "salty-potato.db")
    manager = OperationManager(database, recover_incomplete=False)
    application = Application.__new__(Application)
    application.database = database
    application.operations = SimpleNamespace(get=manager.get)
    try:
        with pytest.raises(ValueError, match="after_sequence"):
            application.operation_events("missing", after_sequence=-1)
        with pytest.raises(ValueError, match="limit"):
            application.operation_events("missing", limit=1001)
        with pytest.raises(KeyError, match="Operation does not exist"):
            application.operation_events(
                "00000000-0000-0000-0000-000000000000"
            )
    finally:
        manager.shutdown(wait=True, request_stop=False)
