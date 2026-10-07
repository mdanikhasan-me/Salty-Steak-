"""What a conversation has established, and what the next turn may do with it.

The follow-up defect had two halves. "Give me the exact links" was fixed by
carrying the evidence; "open the cheapest one" was not, because the note that
carried it only ever talked about quoting. These cover both halves and the
seam between them.
"""

from __future__ import annotations

import json

from app.backend.chat import task_state

OBSERVATIONS = [
    {
        "product": "Lexar NM790 2TB",
        "price": 15400,
        "currency": "BDT",
        "seller": "Global Brand",
        "stock": "in stock",
        "url": "https://www.globalbrand.com.bd/lexar-nm790-2tb",
        "retrieved_at": "2026-08-16T06:00:00Z",
    },
    {
        "product": "WD Blue SN580 2TB",
        "price": 14500,
        "currency": "BDT",
        "seller": "Ryans",
        "stock": "in stock",
        "url": "https://www.ryans.com/wd-blue-sn580-2tb",
    },
]

SOURCES = [
    {"title": "Global Brand — Lexar NM790", "url": "https://www.globalbrand.com.bd/lexar-nm790-2tb"},
    {"title": "Ryans — WD Blue SN580", "url": "https://www.ryans.com/wd-blue-sn580-2tb"},
]


def test_every_verified_item_keeps_a_reference_and_its_address() -> None:
    state = task_state.build(observations=OBSERVATIONS, sources=SOURCES)
    assert [entity["ref"] for entity in state["entities"]] == ["e1", "e2"]
    assert state["entities"][1]["url"] == "https://www.ryans.com/wd-blue-sn580-2tb"
    assert state["entities"][1]["price"] == 14500
    assert task_state.is_empty(state) is False


def test_an_empty_conversation_adds_nothing_to_the_prompt() -> None:
    empty = task_state.build()
    assert task_state.is_empty(empty) is True
    assert task_state.project(empty) == ""
    assert task_state.project(empty, can_act=True) == ""


def test_the_evidence_is_reference_material_not_the_task() -> None:
    """It answered the wrong question.

    Asked "when did i ask for image gen wtf?" after a research turn, the model
    restated the SSD result. The evidence is persuasive and a small model reads
    the last strong instruction as the task, so the note says what it is for
    before it says anything else.
    """

    note = task_state.project(task_state.build(observations=OBSERVATIONS))
    assert "Answer the message that was actually sent" in note
    assert "not to be restated" in note
    # The framing comes before the data it frames.
    assert note.index("actually sent") < note.index("Lexar")


def test_without_authority_the_addresses_are_quotations() -> None:
    note = task_state.project(task_state.build(observations=OBSERVATIONS), can_act=False)
    assert "quote its address exactly" in note
    assert "never write a link that is not here" in note
    # Nothing invites action, because nothing could carry one out.
    assert "open one by using its exact url" not in note


def test_with_authority_the_addresses_are_destinations() -> None:
    """The half that was missing.

    Asked to open the cheapest one, the model had the verified prices in front
    of it and no sign it was allowed to act on them, so it searched again — a
    second minute of work to rediscover what was already known.
    """

    note = task_state.project(task_state.build(observations=OBSERVATIONS), can_act=True)
    assert "an action to carry out" in note
    # The other half of the same failure: told only not to search again, it
    # described the page it had been asked to open and called that done.
    assert "Describing the page instead of opening it does not do what was asked" in note
    assert "Do not research this again" in note
    # The addresses are still the real ones, whichever way the turn may use them.
    assert "https://www.ryans.com/wd-blue-sn580-2tb" in note


def test_the_projection_says_what_a_reference_means() -> None:
    note = task_state.project(task_state.build(observations=OBSERVATIONS))
    assert "the cheapest, the first, that one, those" in note


def test_the_projection_stays_small_enough_to_sit_on_every_turn() -> None:
    many = [dict(OBSERVATIONS[0], product=f"Drive {index}") for index in range(40)]
    state = task_state.build(observations=many, sources=SOURCES * 20)
    assert len(state["entities"]) == task_state.MAX_ENTITIES
    assert len(state["pages_read"]) == task_state.MAX_PAGES
    # Paid on every turn of a conversation that has ever researched anything.
    assert len(task_state.project(state, can_act=True)) < 3_000


def test_images_this_conversation_made_are_part_of_its_state() -> None:
    state = task_state.build(
        artifacts=[
            {"id": "abc123", "prompt": "TASK: logo — North Arc"},
            {"id": "def456", "prompt": "TASK: photograph — cow"},
        ]
    )
    assert [image["ref"] for image in state["images"]] == ["img1", "img2"]
    assert "North Arc" in task_state.project(state)


def test_the_note_carries_real_json_the_model_can_read() -> None:
    note = task_state.project(task_state.build(observations=OBSERVATIONS))
    payload = json.loads(note.splitlines()[-1])
    assert payload["entities"][0]["seller"] == "Global Brand"
    # The schema marker is plumbing and is not spent on prompt tokens.
    assert "schema" not in payload
