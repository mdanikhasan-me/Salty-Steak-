"""A correction has to land on the evidence, not only on the next sentence.

Research stored "Hiksemi 2TB ৳13,200, verified". The user said it was the
512GB price. Until now the next answer changed and the wrong observation stayed
standing as trusted evidence, so the question after that was answered from it
again.
"""

from __future__ import annotations

from pathlib import Path

from app.backend.chat import task_state
from app.backend.chat.service import ChatService
from app.backend.database.control import Database, json_text, new_id, utc_now

OBSERVATIONS = [
    {
        "product": "Hiksemi FUTURE 2TB",
        "price": 13200,
        "currency_code": "BDT",
        "price_display": "৳13,200",
        "seller": "shop.example.com",
        "stock": "in_stock",
        "url": "https://shop.example.com/hiksemi",
    },
    {
        "product": "Lexar NM790 2TB",
        "price": 40999,
        "currency_code": "BDT",
        "price_display": "৳40,999",
        "seller": "www.ryans.com",
        "stock": "in_stock",
        "url": "https://www.ryans.com/lexar",
    },
]


def _service(tmp_path: Path):
    database = Database(tmp_path / "control" / "salty-potato.db")
    service = ChatService.__new__(ChatService)
    service.database = database
    conversation_id = new_id()
    now = utc_now()
    database.execute(
        "INSERT INTO conversations(id,title,created_at,updated_at) VALUES (?, 'R', ?, ?)",
        (conversation_id, now, now),
    )
    database.execute(
        """
        INSERT INTO messages(
            id, conversation_id, role, content, sequence,
            technical_details_json, created_at
        ) VALUES (?, ?, 'assistant', 'answer', 0, ?, ?)
        """,
        (
            new_id(),
            conversation_id,
            json_text({"orchestration": {"kind": "research", "observations": OBSERVATIONS}}),
            now,
        ),
    )
    return service, conversation_id


def test_a_disproven_observation_is_superseded_not_deleted(tmp_path: Path) -> None:
    service, conversation_id = _service(tmp_path)

    changed = service.supersede_observations(
        conversation_id,
        refs=["e1"],
        because="the displayed price belongs to the 512GB variant",
    )
    assert changed == 1

    state = service.conversation_task_state(conversation_id)
    first, second = state["entities"]
    assert first["status"] == "superseded"
    assert "512GB" in first["superseded_because"]
    # The record survives, with its address, so the correction can be shown.
    assert first["url"] == "https://shop.example.com/hiksemi"
    # Untouched evidence stays exactly as it was.
    assert "status" not in second


def test_the_model_is_told_not_to_use_superseded_evidence(tmp_path: Path) -> None:
    service, conversation_id = _service(tmp_path)
    service.supersede_observations(
        conversation_id, refs=["e1"], because="wrong variant"
    )

    note = task_state.project(service.conversation_task_state(conversation_id))
    assert "superseded" in note
    assert "do not count it when comparing" in note


def test_an_unknown_reference_changes_nothing(tmp_path: Path) -> None:
    service, conversation_id = _service(tmp_path)
    assert service.supersede_observations(conversation_id, refs=["e9"], because="x") == 0
    assert service.supersede_observations(conversation_id, refs=[], because="x") == 0
