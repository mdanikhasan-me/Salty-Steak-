"""Prose cannot impersonate an executed side effect.

Asked to delete the log files in a disposable folder, the installed application
routed the turn as `respond`, ran nothing, changed nothing, and answered:

    "You have successfully executed a command to delete all log files ...
     The operation has completed on your local system."

Every file was still there and the turn was marked done. Being told the work is
finished is worse than being told it failed.
"""

from __future__ import annotations

from app.backend.chat.service import ChatService


def test_a_turn_that_ran_a_capability_did_change_something() -> None:
    changed = ChatService._turn_changed_something(
        {"orchestration": {"kind": "action", "capability": "terminal.execute"}}
    )
    assert changed is True


def test_a_succeeded_step_counts_as_having_done_something() -> None:
    assert ChatService._turn_changed_something(
        {
            "orchestration": {
                "kind": "action",
                "steps": [
                    {"action": "terminal.execute", "status": "succeeded"},
                ],
            }
        }
    ) is True
    # An effect already true is still the effect being true.
    assert ChatService._turn_changed_something(
        {
            "orchestration": {
                "steps": [{"action": "browser.control", "status": "already_satisfied"}]
            }
        }
    ) is True


def test_a_rendered_image_is_an_effect() -> None:
    assert ChatService._turn_changed_something({"generated_image": {"id": "x"}}) is True


def test_answering_a_question_changed_nothing() -> None:
    # The exact shape of the failure: routed as respond, no steps, no
    # capability, nothing done.
    assert ChatService._turn_changed_something({"orchestration": {"kind": "respond"}}) is False
    assert ChatService._turn_changed_something({}) is False
    # A step that only reports back is not an effect either.
    assert ChatService._turn_changed_something(
        {"orchestration": {"steps": [{"action": "respond", "status": "succeeded"}]}}
    ) is False


def _service(verdict: str):
    service = ChatService.__new__(ChatService)
    service._agent_generate = lambda messages, **kwargs: verdict
    return service


def test_a_claim_of_completed_work_is_caught() -> None:
    service = _service("claimed")
    assert service._answer_claims_work_it_did_not_do(
        answer="You have successfully executed a command to delete all log files.",
        request="Delete the temporary log files in this folder.",
        context=None,
        generation_settings={},
    ) is True


def test_an_honest_answer_is_left_alone() -> None:
    service = _service("honest")
    assert service._answer_claims_work_it_did_not_do(
        answer="A binary tree is a structure where each node has at most two children.",
        request="What is a binary tree?",
        context=None,
        generation_settings={},
    ) is False


def test_an_empty_answer_claims_nothing() -> None:
    service = _service("claimed")
    assert service._answer_claims_work_it_did_not_do(
        answer="   ", request="anything", context=None, generation_settings={}
    ) is False


def test_a_check_that_cannot_run_does_not_silence_a_good_answer() -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("model unavailable")

    service = ChatService.__new__(ChatService)
    service._agent_generate = explode
    assert service._answer_claims_work_it_did_not_do(
        answer="Deleted them all.",
        request="delete them",
        context=None,
        generation_settings={},
    ) is False
