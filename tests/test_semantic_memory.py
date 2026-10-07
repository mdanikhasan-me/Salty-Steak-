from __future__ import annotations

from pathlib import Path

import pytest

from app.backend.memory import MemoryRefused, SemanticMemory
from app.backend.memory.store import safe_to_remember


@pytest.fixture()
def memory(tmp_path: Path) -> SemanticMemory:
    store = SemanticMemory(tmp_path / "memory.db")
    yield store
    store.close()


def _seed(memory: SemanticMemory) -> None:
    memory.remember(
        kind="fact",
        subject="Work email address",
        body="The work address is a.hasan@example.com; the personal one is not used for clients.",
        tags=["email"],
    )
    memory.remember(
        kind="preference",
        subject="Sending email",
        body="Always draft replies for review. Never send without approval.",
        confidence=0.95,
        tags=["email"],
    )
    memory.remember(
        kind="entity",
        subject="Sam Okafor",
        body="Sam Okafor is the client at Northwind, not the colleague of the same name.",
        tags=["contacts"],
    )
    memory.remember(
        kind="procedure",
        subject="Invoicing",
        body="Invoices go out on the last Friday of each month.",
        tags=["billing"],
    )


# ------------------------------------------------------------------- recall


def test_a_memory_survives_to_be_recalled(memory: SemanticMemory) -> None:
    _seed(memory)

    found = memory.recall("what is the work email")

    assert found
    assert "a.hasan@example.com" in found[0].body


def test_recall_is_scoped_by_kind(memory: SemanticMemory) -> None:
    _seed(memory)

    found = memory.recall("email", kinds=["preference"])

    assert len(found) == 1
    assert found[0].kind == "preference"


def test_an_unrelated_task_carries_no_memory_preamble(memory: SemanticMemory) -> None:
    _seed(memory)

    # Better an empty briefing than an irrelevant one taking up the prompt.
    assert memory.briefing("compile the rust toolchain") == ""
    assert "last Friday" in memory.briefing("when do invoices go out")


def test_punctuation_in_a_query_cannot_break_the_search(memory: SemanticMemory) -> None:
    _seed(memory)

    # A raw FTS5 query would treat these as operators and raise.
    for query in ['email "OR" AND', "a.hasan@example.com", "NEAR(", "*", "-"]:
        memory.recall(query)


def test_a_word_recalls_a_memory_written_in_another_form(memory: SemanticMemory) -> None:
    _seed(memory)

    # Without stemming this is not recalled at all: the memory says "Invoices"
    # and the user said "invoice".
    found = memory.recall("when do I send the next invoice")

    assert "Invoicing" in [record.subject for record in found]


def test_confidence_settles_a_tie_without_overturning_relevance(
    memory: SemanticMemory,
) -> None:
    memory.remember(
        kind="fact", subject="Northwind", body="Northwind is the client account.",
        confidence=0.2,
    )
    memory.remember(
        kind="preference", subject="Tone", body="Keep replies brief.", confidence=1.0
    )

    # The low-confidence memory is squarely on topic and must still win.
    assert memory.recall("tell me about Northwind")[0].subject == "Northwind"


def test_a_memory_that_keeps_proving_useful_surfaces(memory: SemanticMemory) -> None:
    _seed(memory)
    for _ in range(6):
        memory.recall("email preference for sending")

    preference = next(r for r in memory.recent() if r.kind == "preference")
    assert preference.use_count >= 6
    assert preference.last_used_at is not None


# --------------------------------------------------------------- supersede


def test_a_changed_fact_is_superseded_not_overwritten(memory: SemanticMemory) -> None:
    original = memory.remember(
        kind="fact", subject="Work email address", body="It is old@example.com"
    )
    replacement = memory.remember(
        kind="fact",
        subject="Work email address",
        body="It is new@example.com",
        supersedes=original.memory_id,
    )

    assert memory.get(original.memory_id).superseded_by == replacement.memory_id
    assert memory.get(original.memory_id).active is False
    # Only the current belief is recalled, but the change stays visible.
    found = memory.recall("work email address")
    assert [record.memory_id for record in found] == [replacement.memory_id]
    assert len(memory.recall("work email address", include_superseded=True)) == 2
    assert memory.statistics()["superseded"] == 1


# ---------------------------------------------------------------- refusal


@pytest.mark.parametrize(
    "body",
    [
        "The portal password: hunter2rocks",
        "Use api_key = ABCD1234EFGH5678",
        "Authorization Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "The session cookie is sk-abcdefghijklmnopqrstuvwx",
        "Token is dGhpcy1pcy1hLXZlcnktbG9uZy1vcGFxdWUtc2VjcmV0LXZhbHVl",
    ],
)
def test_a_credential_is_refused_before_it_reaches_the_disk(
    memory: SemanticMemory, body: str
) -> None:
    with pytest.raises(MemoryRefused, match="credential"):
        memory.remember(kind="fact", subject="Portal login", body=body)

    # The decisive check: nothing was written, so nothing leaked.
    assert memory.statistics()["active"] == 0
    assert memory.recall("portal login password token") == []


def test_a_credential_field_cannot_even_be_a_subject(memory: SemanticMemory) -> None:
    with pytest.raises(MemoryRefused):
        memory.remember(kind="fact", subject="password", body="the usual one")


def test_ordinary_text_about_accounts_is_still_allowed(memory: SemanticMemory) -> None:
    # Refusing every mention of the word would make memory useless for the
    # workflows it exists to serve.
    record = memory.remember(
        kind="procedure",
        subject="Student portal",
        body="The portal asks for a sign-in, so wait for the user to authenticate.",
    )
    assert record.active


def test_a_payload_whose_only_content_was_secret_is_not_worth_remembering() -> None:
    assert safe_to_remember({"url": "https://example.com", "title": "Home"}) is True
    assert safe_to_remember({"url": "https://example.com", "token": "abc"}) is False


# ----------------------------------------------------------------- hygiene


def test_a_memory_must_be_a_fact_not_a_transcript(memory: SemanticMemory) -> None:
    with pytest.raises(MemoryRefused, match="transcript"):
        memory.remember(kind="fact", subject="Long", body="x" * 2_001)
    with pytest.raises(MemoryRefused, match="not a memory kind"):
        memory.remember(kind="gossip", subject="s", body="b")
    with pytest.raises(MemoryRefused):
        memory.remember(kind="fact", subject="  ", body="b")


def test_explicit_context_memory_has_a_bounded_larger_limit(
    memory: SemanticMemory,
) -> None:
    body = "conversation context " * 300
    record = memory.remember(
        kind="context",
        subject="User-saved conversation",
        body=body,
        source="explicit_user_command",
    )
    assert record.kind == "context"
    assert memory.get(record.memory_id).body == body.strip()
    with pytest.raises(MemoryRefused, match="12,000-character"):
        memory.remember(
            kind="context",
            subject="Too large",
            body="x" * 12_001,
            source="explicit_user_command",
        )


def test_the_user_can_erase_what_is_remembered(memory: SemanticMemory) -> None:
    _seed(memory)
    target = memory.recall("Sam Okafor")[0]

    assert memory.forget(target.memory_id) is True
    assert memory.get(target.memory_id) is None
    assert memory.recall("Sam Okafor") == []
    assert memory.forget(target.memory_id) is False

    assert memory.forget_all() == 3
    assert memory.statistics()["active"] == 0
    assert memory.recall("email") == []


def test_memory_outlives_the_process(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    first = SemanticMemory(path)
    _seed(first)
    first.close()

    second = SemanticMemory(path)
    try:
        assert "last Friday" in second.recall("invoices")[0].body
        assert second.statistics()["active"] == 4
    finally:
        second.close()
