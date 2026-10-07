"""A picture in the history is not a standing instruction to draw.

The installed application answered "but this is the price of 512 gb" by
rendering a diagram of the SSD, and answered "when did i ask for image gen
wtf?" by revising that diagram. Both turns were corrections. Neither asked for
a picture.
"""

from __future__ import annotations

import pytest

from app.backend.chat.dispatch import build_turn_instruction


def test_revision_is_only_offered_after_an_image_turn() -> None:
    after_image = build_turn_instruction(
        image_available=True, has_previous_image=True
    )
    assert "revise_image" in after_image
    # The offer expires the moment the conversation moves on.
    after_research = build_turn_instruction(
        image_available=True, has_previous_image=False
    )
    assert "revise_image" not in after_research


def test_the_choice_is_about_the_newest_message() -> None:
    text = build_turn_instruction(image_available=True, has_previous_image=True)
    assert "THIS message" in text
    assert "context, not an instruction" in text
    # And it is not paid for when there is no earlier image to misread.
    assert "THIS message" not in build_turn_instruction(
        image_available=True, has_previous_image=False
    )


def test_a_correction_cannot_inherit_an_earlier_subject() -> None:
    """The guard reads what was asked now, not the joined thread.

    The thread carries every earlier request, so a brief inherited from a
    previous subject always shared terms with it and always passed.
    """

    from app.backend.imaging import ImageOrchestrationError, ImageOrchestrator

    orchestrator = ImageOrchestrator(backend="test")
    thread = (
        "What is the cheapest 2TB Gen4 NVMe SSD currently in stock in Bangladesh?\n\n"
        "but this is the price of 512 gb"
    )
    with pytest.raises(ImageOrchestrationError):
        orchestrator.prepare(
            {
                "action": "generate_image",
                "brief": {
                    "subject": "Hiksemi FUTURE 2TB M.2 NVMe PCIe Gen4x4 SSD",
                    "image_type": "diagram",
                },
            },
            original_request=thread,
            latest_request="but this is the price of 512 gb",
        )


def test_a_real_revision_still_works() -> None:
    # The fix must not close the door it was built to keep open.
    from app.backend.imaging import ImageOrchestrator

    orchestrator = ImageOrchestrator(backend="test")
    job = orchestrator.prepare(
        {"action": "generate_image", "brief": {"subject": "a red barn at sunrise"}},
        original_request="draw a red barn at sunrise",
        latest_request="draw a red barn at sunrise",
        conversation_id="c1",
    )
    revised = orchestrator.prepare_revision(
        {"action": "revise_image", "changes": {"style": "softer light"}},
        feedback="make the light softer",
        conversation_id="c1",
        parent_job_id=job.job_id,
    )
    assert revised.revision == 2
    assert "barn" in revised.brief.subject
