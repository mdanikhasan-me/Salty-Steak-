"""Cooking is depth. Research and Agent are authorities. They never toggle each other.

With Research switched off and Cooking on, the installed application answered
"hi" by searching YouTube, Wikipedia and Cartoon Network. Research was the one
job type offered on every turn whatever the switch said, so the model chose
from a menu it should never have been shown.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.backend.chat.dispatch import build_turn_instruction
from app.backend.chat.service import ChatService


class _GenerationConfig:
    def section(self, name: str) -> dict[str, object]:
        assert name == "generation"
        return {
            "conversation_token_budget": 7680,
            "reserved_output_tokens": 512,
            "maximum_output_tokens": 256,
            "temperature": 0.8,
            "top_p": 0.95,
            "top_k": 40,
            "repetition_penalty": 1.1,
            "seed": -1,
            "reasoning_mode": "auto",
        }


def settings(**supplied) -> dict:
    """The generation settings a turn is actually run with."""

    service = object.__new__(ChatService)
    service.config = _GenerationConfig()
    service.model_bundle_runtime = SimpleNamespace(
        profile=SimpleNamespace(context_limit=65536)
    )
    service.model_bundle = {
        "id": "base-steak-2-0-9b",
        "configured_context_tokens": 65536,
        "default_context_tokens": 32768,
        "reasoning_default": "instant",
        "runtime_profile": {"context_limit": 65536},
        "context_presets": [32768, 65536],
    }
    return service._normalise_generation_settings(dict(supplied))


def instruction(**kwargs) -> str:
    base = {
        "image_available": False,
        "has_previous_image": False,
        "capabilities": (),
        "agent_mode": False,
        "research_available": False,
    }
    base.update(kwargs)
    return build_turn_instruction(**base)


def test_research_off_takes_the_door_away() -> None:
    assert '"research"' not in instruction()
    assert '"research"' in instruction(research_available=True)


def test_reasoning_depth_is_not_an_authority() -> None:
    """Cooking is a different dial and never turns a switch on.

    The instruction is built from what is available, and nothing about
    reasoning effort appears in it — which is the point: depth cannot smuggle
    in a capability.
    """

    # Whatever the reasoning mode, an unavailable capability stays absent.
    off = instruction()
    assert '"research"' not in off
    assert '"action" —' not in off
    assert '"plan" —' not in off


def test_agent_does_not_bring_research_with_it() -> None:
    agent_only = instruction(capabilities=["files.manage"], agent_mode=True)
    assert '"action" —' in agent_only
    assert '"research"' not in agent_only


def test_research_does_not_bring_the_computer_with_it() -> None:
    research_only = instruction(research_available=True)
    assert '"research"' in research_only
    assert '"action" —' not in research_only
    assert '"plan" —' not in research_only


def test_both_switches_offer_both_and_nothing_more() -> None:
    both = instruction(
        capabilities=["files.manage"], agent_mode=True, research_available=True
    )
    assert '"research"' in both
    assert '"action" —' in both
    # Images are their own availability and are not implied by either switch.
    assert "generate_image" not in both


def test_the_research_switch_offers_research_without_commanding_it() -> None:
    """The switch is an authority. It is not an instruction to go and search.

    One boolean carried both meanings and the imperative reading won every
    turn: the switch put research on the menu *and* sent the turn straight to
    the research runner before the decision was ever read. "hi" cost six
    sources and thirty-nine seconds because of it.
    """

    turn = settings(research_mode=True)
    assert turn["research_available"] is True
    assert turn["research_forced"] is False


def test_the_research_command_is_the_instruction() -> None:
    """`/research` is the user saying to search now, and it still does."""

    turn = settings(research_command=True)
    assert turn["research_available"] is True
    assert turn["research_forced"] is True
    assert settings(**turn)["research_forced"] is True


def test_research_off_reaches_neither() -> None:
    turn = settings()
    assert turn["research_available"] is False
    assert turn["research_forced"] is False


def test_agent_mode_never_asserts_the_user_asked_for_computer_work() -> None:
    """Availability is not a claim about what this turn wants.

    The instruction opened "You are operating this computer for the user" and
    closed "the user asked you to do this on their computer", so a greeting
    arrived at a model that had already been told work was wanted. It ran a
    terminal command on "hi".
    """

    text = instruction(
        capabilities=["files.manage", "terminal.execute"], agent_mode=True
    ).casefold()

    assert "the user asked you to do this" not in text
    assert "you are operating this computer" not in text
    # The capability is still plainly offered — restraint is not removal.
    assert "files.manage" in text


def test_answering_is_the_default_whatever_the_switches_say() -> None:
    """The discriminator is the request. It is never the switch."""

    # An empty menu produces no instruction at all, which is why the two cases
    # compared here both have something to offer.
    assert "Answer normally" in instruction(research_available=True)
    assert "Answer normally" in instruction(
        capabilities=["files.manage"], agent_mode=True
    )


def test_a_route_outside_the_turns_authority_reaches_no_runner() -> None:
    """Advertising is not enforcement, and the router is not the last word.

    A decision can name a switched-off family even when the instruction never
    offered it — from a hallucination, from the repair pass, or from stale task
    state. The orchestrator checks the turn's own authority after parsing and
    before any runner, so a bug upstream cannot escalate a turn.
    """

    from app.backend.chat.dispatch import TurnDispatcher

    reached: list[str] = []
    dispatcher = TurnDispatcher(
        run_research=lambda **kwargs: reached.append("research") or {},
        run_agent=lambda **kwargs: reached.append("action") or {},
        permitted={"respond"},
    )

    for action in ("research", "action"):
        outcome = dispatcher.dispatch(
            {"action": action, "question": "hi", "capability": "terminal.execute"},
            reply_text="Hello.",
            request="hi",
            conversation_id="c1",
        )
        assert reached == []
        assert outcome.kind == "respond"
        assert outcome.details["refused_by_authority"] == action


def test_a_permitted_route_still_runs() -> None:
    """The gate refuses what the turn cannot do and nothing else."""

    from app.backend.chat.dispatch import TurnDispatcher

    reached: list[str] = []
    dispatcher = TurnDispatcher(
        run_research=lambda **kwargs: (reached.append("research"), {"answer": "ok"})[1],
        permitted={"respond", "research"},
    )
    outcome = dispatcher.dispatch(
        {"action": "research", "question": "what is the population of Bangladesh"},
        reply_text="",
        request="what is the population of Bangladesh",
        conversation_id="c1",
    )
    assert reached == ["research"]
    assert outcome.kind == "research"


def test_an_unrestricted_dispatcher_is_unchanged() -> None:
    """No authority set means every existing caller behaves exactly as before."""

    from app.backend.chat.dispatch import TurnDispatcher

    dispatcher = TurnDispatcher(run_research=lambda **kwargs: {"answer": "ok"})
    outcome = dispatcher.dispatch(
        {"action": "research", "question": "q"},
        reply_text="",
        request="q",
        conversation_id="c1",
    )
    assert outcome.kind == "research"


def test_a_clear_direct_verdict_vetoes_the_route() -> None:
    """The second half of restraint: available, and actually required.

    Under Cooking the model picks from the menu whatever is on it — one trial
    of "hi" routed research over six sources, another began rendering a
    greeting card. The instruction cannot fix that on its own, because the
    variance is in the sampling of a structured choice, so a route is checked
    once against the request before it runs.
    """

    from app.backend.chat.service import _reads_as_unnecessary

    assert _reads_as_unnecessary("conversation") is True
    assert _reads_as_unnecessary("  Conversation.  ") is True
    assert _reads_as_unnecessary("CONVERSATION") is True
    assert _reads_as_unnecessary("conversation — they only said hello") is True
    # The first word decides. "task — this is not just conversation" contains
    # the veto word and means the opposite of it.
    assert _reads_as_unnecessary("task — this is not just conversation") is False


def test_the_necessity_check_fails_open() -> None:
    """A verifier that cannot run must not become a veto.

    Getting the model to route at all took four recorded sessions. A check that
    silently swallows good routes when it errors, times out or waffles would
    cost more than the restraint it buys, so only an explicit verdict stops a
    route and everything else lets it through.
    """

    from app.backend.chat.service import _reads_as_unnecessary

    for reply in ("task", "", "   ", "yes", "I am not sure", None, "{}"):
        assert _reads_as_unnecessary(reply) is False


def test_an_image_switch_is_independent_of_the_other_two() -> None:
    images = instruction(image_available=True)
    assert "generate_image" in images
    assert '"research"' not in images
    assert '"action" —' not in images
