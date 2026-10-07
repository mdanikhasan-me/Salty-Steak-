"""Pinning and labels, proved across a restart.

Every assertion here goes through the real store on a real database file, and
the "restart" is a second Database opened on the same path. A pin that only
exists in a live object is not a pin, so an in-memory fixture would prove
nothing this feature is for.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.backend.chat.service import ChatService
from app.backend.database.control import Database


def _bare(path: Path) -> ChatService:
    """A ChatService with only the persistence it needs for these tests.

    The full constructor wants a runtime, an operation manager and a model
    bundle; conversation organisation touches none of them.
    """

    service = ChatService.__new__(ChatService)
    service.database = Database(path)
    # Deletion cancels any generation for the conversation first; there is no
    # runtime here, so the lock is all that part of the path needs.
    service._generation_lock = threading.RLock()
    service._cancel_active_generation = lambda **_: None
    service.vision_inputs = None
    return service


def _reopened(path: Path) -> ChatService:
    """What the user gets after closing and starting the application again."""

    return _bare(path)


@pytest.fixture()
def store(tmp_path: Path) -> ChatService:
    return _bare(tmp_path / "control.db")


def _conversation(service: ChatService) -> str:
    return service.create_conversation()["id"]


# ------------------------------------------------------------------ pinning


def test_a_pin_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    service = _bare(path)
    conversation_id = _conversation(service)

    service.set_conversation_pinned(conversation_id, True)

    after_restart = _reopened(path)
    listed = {row["id"]: row for row in after_restart.list_conversations()}
    assert listed[conversation_id]["pinned"] is True
    assert listed[conversation_id]["pinned_at"]


def test_unpinning_returns_a_conversation_to_recency(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    service = _bare(path)
    first = _conversation(service)
    second = _conversation(service)

    service.set_conversation_pinned(first, True)
    order = [row["id"] for row in service.list_conversations()]
    assert order[0] == first

    service.set_conversation_pinned(first, False)
    assert _reopened(path).conversation_summary(first)["pinned"] is False
    # And the list no longer puts it first for being pinned.
    assert all(not row["pinned"] for row in _reopened(path).list_conversations())


def test_a_pinned_conversation_appears_exactly_once(store: ChatService) -> None:
    conversation_id = _conversation(store)
    store.set_conversation_pinned(conversation_id, True)

    listed = [row for row in store.list_conversations() if row["id"] == conversation_id]

    assert len(listed) == 1


def test_pinning_does_not_count_as_activity(store: ChatService) -> None:
    # Organising history must not reorder it: bumping updated_at would move the
    # conversation to the top of Recents every time it was pinned.
    conversation_id = _conversation(store)
    before = store.conversation_summary(conversation_id)["updated_at"]

    store.set_conversation_pinned(conversation_id, True)

    assert store.conversation_summary(conversation_id)["updated_at"] == before


def test_pinning_a_conversation_that_does_not_exist_is_refused(
    store: ChatService,
) -> None:
    with pytest.raises(KeyError):
        store.set_conversation_pinned("11111111-1111-1111-1111-111111111111", True)


# ------------------------------------------------------------------- labels


def test_a_label_and_its_association_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    service = _bare(path)
    conversation_id = _conversation(service)
    label = service.create_label("Hardware", "blue")

    service.set_conversation_label(conversation_id, label["id"], True)

    after_restart = _reopened(path)
    summary = after_restart.conversation_summary(conversation_id)
    assert [item["name"] for item in summary["labels"]] == ["Hardware"]
    assert summary["labels"][0]["tone"] == "blue"


def test_a_conversation_can_carry_more_than_one_label(store: ChatService) -> None:
    conversation_id = _conversation(store)
    for name in ("Salty Steak", "Hardware"):
        store.set_conversation_label(
            conversation_id, store.create_label(name)["id"], True
        )

    names = [item["name"] for item in store.conversation_summary(conversation_id)["labels"]]

    assert names == ["Hardware", "Salty Steak"]


def test_renaming_a_label_keeps_its_conversations(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    service = _bare(path)
    conversation_id = _conversation(service)
    label = service.create_label("Uni")
    service.set_conversation_label(conversation_id, label["id"], True)

    service.update_label(label["id"], name="University")

    after_restart = _reopened(path)
    assert [item["name"] for item in after_restart.conversation_summary(conversation_id)["labels"]] == [
        "University"
    ]


def test_deleting_a_label_never_deletes_its_conversations(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    service = _bare(path)
    conversation_id = _conversation(service)
    label = service.create_label("Temporary")
    service.set_conversation_label(conversation_id, label["id"], True)

    service.delete_label(label["id"])

    after_restart = _reopened(path)
    summary = after_restart.conversation_summary(conversation_id)
    assert summary["id"] == conversation_id
    assert summary["labels"] == []
    assert after_restart.list_labels() == []


def test_deleting_a_conversation_leaves_other_conversations_labelled(
    store: ChatService,
) -> None:
    label = store.create_label("Shared")
    kept = _conversation(store)
    removed = _conversation(store)
    for conversation_id in (kept, removed):
        store.set_conversation_label(conversation_id, label["id"], True)

    store.delete_conversation(removed)

    assert [item["name"] for item in store.conversation_summary(kept)["labels"]] == [
        "Shared"
    ]
    assert store.list_labels()[0]["conversation_count"] == 1


def test_two_labels_cannot_share_a_name(store: ChatService) -> None:
    store.create_label("Hardware")

    with pytest.raises(ValueError, match="already exists"):
        store.create_label("  hardware  ")


def test_a_label_name_must_be_one_usable_line(store: ChatService) -> None:
    for name in ("", "   ", "two\nlines", "x" * 61):
        with pytest.raises(ValueError):
            store.create_label(name)


def test_a_label_tone_comes_from_the_products_palette(store: ChatService) -> None:
    with pytest.raises(ValueError, match="tone must be one of"):
        store.create_label("Neon", "#00ff00")


def test_label_counts_report_real_associations(store: ChatService) -> None:
    label = store.create_label("Counted")
    for _ in range(3):
        store.set_conversation_label(_conversation(store), label["id"], True)

    assert store.list_labels()[0]["conversation_count"] == 3


def test_removing_a_label_from_one_conversation_leaves_the_label(
    store: ChatService,
) -> None:
    label = store.create_label("Kept")
    conversation_id = _conversation(store)
    store.set_conversation_label(conversation_id, label["id"], True)

    store.set_conversation_label(conversation_id, label["id"], False)

    assert store.conversation_summary(conversation_id)["labels"] == []
    assert [item["name"] for item in store.list_labels()] == ["Kept"]
