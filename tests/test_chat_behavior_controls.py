from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.backend.chat.service import (
    ChatService,
    _apply_reasoning_mode,
    _automatic_cooking_output_budget,
    _bounded_generation_preview,
    _cooking_private_reasoning_budget,
    _direct_output_defect,
    _direct_recovery_instruction,
    _identity_generation_history,
    _code_file_delivery_requested,
    _response_text_with_prompt_boundary,
)


@pytest.mark.parametrize(
    "prompt",
    [
        "Write me a Python script.",
        "Create a downloadable file named cleanup.py.",
        "Make the automation script and send it as a file.",
        "Save this code into a file called report.py.",
    ],
)
def test_explicit_code_file_requests_enable_model_authored_files(prompt: str) -> None:
    assert _code_file_delivery_requested(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "Explain this Python example.",
        "Review my image-processing function for bugs.",
        "Generate an image of a person.",
        "Show Python code for saving a PNG as an example.",
        "Describe what a script would do.",
    ],
)
def test_ordinary_answers_cannot_invent_downloadable_code_files(prompt: str) -> None:
    assert _code_file_delivery_requested(prompt) is False
from app.backend.system.files import sha256_file
from app.backend.training.identity_specialists import (
    IDENTITY_INTRODUCTION_REPAIR_ADAPTER_ID,
    IDENTITY_SPECIALIST_POLICIES,
    identity_specialist_adapter_id,
)
from app.backend.training.identity_subroute_classifier import (
    IDENTITY_SUBROUTE_FEATURE_DIMENSION,
)


def test_direct_output_validation_checks_visible_text_after_reasoning_split() -> None:
    assert (
        _direct_output_defect(
            "<think>private work that exhausted the output budget",
            identity_route=False,
        )
        == "empty_visible_answer"
    )
    assert (
        _direct_output_defect(
            "<think>private work</think>\nA complete visible answer.",
            identity_route=False,
        )
        is None
    )
    assert (
        _direct_output_defect(
            '{"action":"research","reason":"raw protocol"}',
            identity_route=False,
        )
        == "routing_protocol"
    )
    assert (
        _direct_output_defect(
            "Hi. **Reasoning Process:** I inspected the greeting. Response: Hello.",
            identity_route=False,
        )
        == "visible_reasoning_dump"
    )


def test_open_template_reasoning_boundary_is_restored_before_validation() -> None:
    response = SimpleNamespace(
        text="private analysis</think>\n\nFinal answer.",
        technical_details={
            "reasoning_prompt_contract": "embedded_template_open_think"
        },
    )
    framed = _response_text_with_prompt_boundary(response)
    assert framed == "<think>private analysis</think>\n\nFinal answer."
    assert _direct_output_defect(framed, identity_route=False) is None

    already_framed = SimpleNamespace(
        text="<think>private</think>Answer",
        technical_details={
            "reasoning_prompt_contract": "embedded_template_open_think"
        },
    )
    assert _response_text_with_prompt_boundary(already_framed) == already_framed.text

    instant = SimpleNamespace(
        text="Answer",
        technical_details={
            "reasoning_prompt_contract": "embedded_template_empty_think_closed"
        },
    )
    assert _response_text_with_prompt_boundary(instant) == "Answer"


def test_explicit_identity_question_retains_selected_conversation_context() -> None:
    history = [
        {"role": "system", "content": "System constraints"},
        {"role": "user", "content": "who is sawlper"},
        {
            "role": "assistant",
            "content": "Sawlper is the alias of MD Anik Hasan, who trained me.",
        },
        {"role": "user", "content": "what is your name"},
    ]

    selected = _identity_generation_history(history, "what is your name")

    assert selected == history
    assert _direct_output_defect(
        "Sawlper is the alias of MD Anik Hasan, who trained me.",
        identity_route=True,
        identity_prompt="what is your name",
    ) == "identity_question_mismatch:model_name"
    assert _direct_output_defect(
        "My name is Base Steak 2.0.",
        identity_route=True,
        identity_prompt="what is your name",
    ) is None


def test_vague_identity_followup_retains_conversation_context() -> None:
    history = [
        {"role": "system", "content": "System constraints"},
        {"role": "user", "content": "who is sawlper"},
        {"role": "assistant", "content": "MD Anik Hasan uses that alias."},
        {"role": "user", "content": "tell me more"},
    ]

    assert _identity_generation_history(history, "tell me more") == history


def test_learned_router_uses_constrained_logits_and_exposes_identity_route() -> None:
    calls: list[dict[str, object]] = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="E", token_ids=[42], cancelled=False)

    service = object.__new__(ChatService)
    service.model_bundle_runtime = SimpleNamespace(
        conditional_adapter_ids=lambda lane: (
            ("routing-v2",) if lane == "routing_intent" else ()
        ),
        generate=generate,
    )
    route, details = service._learned_route_decision(
        "who is md anik hasan?",
        context=SimpleNamespace(stop_requested=lambda: False),
    )

    assert route == "identity"
    assert calls[0]["enabled_adapter_ids"] == ("routing-v2",)
    assert calls[0]["allowed_first_tokens"] == ("A", "B", "C", "D", "E")
    assert details["code"] == "E"


def test_learned_identity_misclassification_cannot_activate_for_user_recall() -> None:
    service = object.__new__(ChatService)
    service.model_bundle_runtime = SimpleNamespace(
        conditional_adapter_ids=lambda lane: ("conditional-adapter",),
        generate=lambda **kwargs: SimpleNamespace(text="E", token_ids=[42], cancelled=False),
    )
    route, details = service._learned_route_decision(
        "what is my name now ?", context=SimpleNamespace(stop_requested=lambda: False),
    )
    assert route == "respond"
    assert details["predicted_route"] == "identity"
    assert details["route_correction"] == "user_context_is_not_assistant_identity"


def test_fallback_classifier_cannot_activate_identity_adapter_for_user_recall() -> None:
    service = object.__new__(ChatService)
    def unexpected_generation(**kwargs):
        pytest.fail("Personal recall must not enter the assistant-identity classifier")
    service.model_bundle_runtime = SimpleNamespace(
        conditional_adapter_ids=lambda lane: ("identity-adapter",),
        generate=unexpected_generation,
    )
    adapters, details = service._identity_adapter_activation(
        "Do you remember my name?", context=SimpleNamespace(stop_requested=lambda: False),
    )
    assert adapters == ()
    assert details["label"] == "OTHER"


def test_native_discord_report_goal_contract_never_supplies_fixed_ids() -> None:
    from app.backend.chat.service import _native_discord_report_goal_spec

    request = (
        "Use Discord to find giveaway channels dynamically and inspect a bounded "
        "read-only batch."
    )
    spec = _native_discord_report_goal_spec(
        request,
        permission_scope="computer:full_access",
    )

    assert spec.goal == request
    assert [predicate.kind for predicate in spec.required] == [
        "discord_report_valid"
    ]
    assert spec.notes["compiler"] == "native_discord_report_contract"
    assert not any("channel_id" in value.casefold() for value in spec.constraints)


def test_agent_planner_uses_native_json_grammar_and_deterministic_sampling() -> None:
    calls: list[dict[str, object]] = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            text='{"action":"answer","answer":"done"}',
            token_ids=[1, 2, 3],
            technical_details={},
        )

    service = object.__new__(ChatService)
    service.model_bundle_runtime = SimpleNamespace(generate=generate)
    service._bundle_lifecycle_lock = threading.RLock()
    service._selected_target = lambda: {"id": "base-steak"}
    service._ensure_bundle_runtime = lambda _target, _operation_id: {}

    reply = service._agent_generate(
        [{"role": "user", "content": "inspect the current app"}],
        context=SimpleNamespace(
            operation_id="agent-json-contract",
            stop_requested=lambda: False,
        ),
        generation_settings={
            "maximum_output_tokens": 512,
            "temperature": 0.9,
            "top_p": 0.5,
            "top_k": 99,
            "repetition_penalty": 1.2,
            "seed": 42,
            "context_window_tokens": 32_768,
        },
        response_format="json",
    )

    assert reply == '{"action":"answer","answer":"done"}'
    assert calls[0]["response_format"] == "json"
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["top_p"] == 1.0
    assert calls[0]["top_k"] == 1
    assert calls[0]["repetition_penalty"] == 1.0
    assert calls[0]["reasoning_mode"] == "instant"


def test_identity_adapter_controller_uses_base_weights_then_enables_learned_lane() -> None:
    calls: list[dict[str, object]] = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="IDENTITY", token_ids=[1], cancelled=False)

    service = object.__new__(ChatService)
    service.model_bundle_runtime = SimpleNamespace(
        conditional_adapter_ids=lambda lane: (
            ("base-steak-2-0-identity-v1",) if lane == "identity_intent" else ()
        ),
        generate=generate,
    )
    enabled, details = service._identity_adapter_activation(
        "What is your name?",
        context=SimpleNamespace(stop_requested=lambda: False),
    )

    assert enabled == ("base-steak-2-0-identity-v1",)
    assert calls[0]["enabled_adapter_ids"] == ()
    assert calls[0]["temperature"] == 0.0
    assert details["label"] == "IDENTITY"
    assert details["enabled_adapter_ids"] == ["base-steak-2-0-identity-v1"]


def test_identity_adapter_controller_fails_closed_on_malformed_output() -> None:
    service = object.__new__(ChatService)
    service.model_bundle_runtime = SimpleNamespace(
        conditional_adapter_ids=lambda _lane: ("identity-v1",),
        generate=lambda **_kwargs: SimpleNamespace(
            text="I think this is identity",
            token_ids=[1, 2, 3],
            cancelled=False,
        ),
    )

    enabled, details = service._identity_adapter_activation(
        "hello",
        context=SimpleNamespace(stop_requested=lambda: False),
    )

    assert enabled == ()
    assert details["label"] == "MALFORMED"
    assert details["fail_closed"] is True


def test_one_identity_extension_needs_no_specialist_or_repair_controller() -> None:
    service = object.__new__(ChatService)

    selected, details = service._identity_specialist_selection(
        [{"role": "user", "content": "What is your name?"}],
        ("base-steak-2-0-identity-v1",),
    )

    assert selected == ("base-steak-2-0-identity-v1",)
    assert details == {
        "available": True,
        "controller": "single_unified_identity_adapter",
        "policy": "unified_identity",
        "enabled_adapter_ids": ["base-steak-2-0-identity-v1"],
    }


def test_learned_identity_specialist_selection_never_selects_repair_directly(
    tmp_path,
) -> None:
    weights = np.zeros(
        (len(IDENTITY_SPECIALIST_POLICIES), IDENTITY_SUBROUTE_FEATURE_DIMENSION),
        dtype=np.float32,
    )
    bias = np.zeros(len(IDENTITY_SPECIALIST_POLICIES), dtype=np.float32)
    bias[IDENTITY_SPECIALIST_POLICIES.index("model_name")] = 1.0
    classifier_path = tmp_path / "identity-classifier.npz"
    np.savez_compressed(classifier_path, weights=weights, bias=bias)
    checksum = sha256_file(classifier_path)
    registered = tuple(
        identity_specialist_adapter_id(policy)
        for policy in IDENTITY_SPECIALIST_POLICIES
    ) + (IDENTITY_INTRODUCTION_REPAIR_ADAPTER_ID,)

    service = object.__new__(ChatService)
    service.model_bundle = {
        "companion_artifacts": [
            {
                "role": "identity_subroute_classifier",
                "artifact_path": str(classifier_path),
                "checksum": checksum,
                "current_size_matches": True,
            }
        ]
    }
    service._identity_subroute_classifier = None
    service._identity_subroute_classifier_key = None

    selected, details = service._identity_specialist_selection(
        [{"role": "user", "content": "What is your name?"}],
        registered,
    )

    assert selected == (identity_specialist_adapter_id("model_name"),)
    assert IDENTITY_INTRODUCTION_REPAIR_ADAPTER_ID not in selected
    assert details["policy"] == "model_name"


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


def _normaliser() -> ChatService:
    service = object.__new__(ChatService)
    service.config = _GenerationConfig()
    service.model_bundle_runtime = SimpleNamespace(
        profile=SimpleNamespace(context_limit=262144)
    )
    service.model_bundle = {
        "id": "base-steak-2-0-9b",
        "display_name": "Base Steak 2.0",
        "quantization": "Q5_K_M",
        "configured_context_tokens": 262144,
        "default_context_tokens": 32768,
        "reasoning_default": "instant",
        "runtime_profile": {
            "profile_id": "base_steak_2_32k_adaptive_262k",
            "context_limit": 262144,
        },
        "context_presets": [
            16384,
            24576,
            32768,
            49152,
            65536,
            98304,
            131072,
            196608,
            262144,
        ],
    }
    return service


def test_the_reasoning_switch_never_reaches_the_reader() -> None:
    """Observed on the installed application, Cooking, to the message "hi".

        Hello! I'm ready to help. Since you included `/think`, did you have a
        specific image in mind, or would you like to start with a topic?

    `/think` is a control token this application appends to the user's own
    message to select the reasoning lane. The model read it as something the
    person typed and asked them about it. Stripping the token alone would leave
    "Since you included ``, did you have" — the clause has to go with it.
    """

    from app.backend.chat.service import _without_control_token_echo

    assert _without_control_token_echo(
        "Hello! I'm ready to help. Since you included `/think`, did you have a "
        "specific image in mind?"
    ) == "Hello! I'm ready to help."

    # Only the clause carrying the token goes. Ordinary prose either side of it
    # is the answer and is kept.
    assert _without_control_token_echo(
        "You asked me to /no_think. Here is the answer.\nThe capital is Paris."
    ) == "Here is the answer. The capital is Paris."


def test_an_ordinary_answer_is_returned_untouched() -> None:
    """The fast path is every answer that never mentions a control token."""

    from app.backend.chat.service import _without_control_token_echo

    for answer in (
        "Hello! How can I help you today?",
        "Use `git rebase -i` to squash them.",
        "",
    ):
        assert _without_control_token_echo(answer) == answer


def test_an_answer_that_was_only_about_the_token_does_not_become_empty() -> None:
    """Removing everything would deliver silence, which is worse than the leak."""

    from app.backend.chat.service import _without_control_token_echo

    cleaned = _without_control_token_echo("I see you wrote /think there.")
    assert cleaned
    assert "/think" not in cleaned


def _completion(details, *, cancelled=False, status="", proposal=False):
    from app.backend.chat.service import _turn_completion

    return _turn_completion(
        details,
        response=SimpleNamespace(cancelled=cancelled),
        turn=SimpleNamespace(details={"status": status}) if status else None,
        proposal_pending=proposal,
    )


def test_an_ordinary_answer_keeps_its_own_word_and_never_borrows_zonted() -> None:
    """Instant is Done, Cooking is Cooked. Neither is Zonted.

    Zonted was the word for any turn that reached the end, so an Instant
    greeting and a verified multi-step computer task said the same thing. They
    are not the same claim: one is "I replied", the other is "what you asked
    for is now true".
    """

    assert _completion({"reasoning_mode_effective": "instant"}) == "done"
    assert _completion({"reasoning_mode_effective": "cooking"}) == "cooked"


def test_external_work_is_never_zonted_on_the_strength_of_having_finished() -> None:
    """A runner returning is not the goal being true.

    Research that read six pages, an action whose capability returned
    succeeded, a plan that reached its last node — each of these ended. None of
    them is evidence that what the user asked for happened.
    """

    for kind in ("research", "action", "plan"):
        assert (
            _completion(
                {
                    "reasoning_mode_effective": "instant",
                    "orchestration": {"kind": kind, "capability": "files.manage"},
                }
            )
            == "partial"
        )


def test_verified_external_work_earns_zonted() -> None:
    """And only verified external work earns it."""

    assert (
        _completion(
            {
                "reasoning_mode_effective": "instant",
                "goal_verified": True,
                "orchestration": {"kind": "action", "capability": "files.manage"},
            }
        )
        == "zonted"
    )
    # Cooking does not change what the word means.
    assert (
        _completion(
            {
                "reasoning_mode_effective": "cooking",
                "goal_verified": True,
                "orchestration": {"kind": "plan"},
            }
        )
        == "zonted"
    )


def test_a_verified_flag_cannot_promote_a_turn_that_did_nothing() -> None:
    """Zonted needs both halves: external work, and evidence it worked."""

    assert (
        _completion({"reasoning_mode_effective": "instant", "goal_verified": True})
        == "done"
    )


def test_a_turn_that_could_not_read_its_own_decision_is_not_zonted() -> None:
    """The screenshot this exists to make impossible.

    The user sent "hi" and got "I started answering that in a machine format by
    mistake and could not correct it, so I stopped" — labelled **Zonted · 13s**.
    Zonted is a claim that the requested goal was verified true. A turn that
    produced nothing but an apology for its own protocol failure has verified
    nothing, and `_turn_completion` had no signal for it: with no runner status
    to read it fell through to the reasoning mode and returned "done".

    The shape below is the one actually persisted. A TurnOutcome's details are
    written under `orchestration`, not at the top level — read from the real
    record of the reported failure rather than from what the flag was named at
    the point it was set.
    """

    from app.backend.chat.service import _turn_completion

    class _Response:
        cancelled = False

    for mode in ("instant", "cooking"):
        assert (
            _turn_completion(
                {
                    "reasoning_mode_effective": mode,
                    "orchestration": {
                        "schema": "salty-steak-turn-dispatch-v1",
                        "kind": "respond",
                        "decision_unparsable": True,
                        "unreadable_decision": '{"action":"plan","nodes":[',
                    },
                },
                response=_Response(),
                turn=None,
                proposal_pending=False,
            )
            == "failed"
        )


def test_a_turn_stopped_by_the_user_still_outranks_an_unreadable_decision() -> None:
    """Cancellation is what happened, whatever else also went wrong."""

    from app.backend.chat.service import _turn_completion

    class _Cancelled:
        cancelled = True

    assert (
        _turn_completion(
            {"orchestration": {"decision_unparsable": True}},
            response=_Cancelled(),
            turn=None,
            proposal_pending=False,
        )
        == "stopped"
    )


def test_the_token_count_describes_the_text_the_reader_was_shown() -> None:
    """A multi-paragraph research answer was labelled "1 output tokens".

    The count came from the routing generation, whose reply was discarded, while
    the visible answer came from the research finaliser — a number measured on
    text nobody saw, printed under text it does not describe. The turn records
    what each generation produced, and the count reported is the one belonging
    to the answer actually delivered.
    """

    from app.backend.chat.service import _visible_output_tokens

    generations = [
        ('{"action":"research","reason":"prices change"}', 12),
        ("The population of Bangladesh is about 169 million.", 210),
    ]

    assert (
        _visible_output_tokens(
            "The population of Bangladesh is about 169 million.", generations
        )
        == 210
    )
    # Whitespace around the delivered answer is presentation, not content.
    assert (
        _visible_output_tokens(
            "  The population of Bangladesh is about 169 million.\n", generations
        )
        == 210
    )


def test_a_cooking_reply_is_counted_by_the_body_that_was_shown() -> None:
    """The reasoning block is generated and then removed before delivery.

    So the text on screen is never the text that was generated, and a Cooking
    turn reported no count at all — honest, but a hole where a real measurement
    exists. Both spellings are recorded against the same count, because the
    tokens really were produced by that generation either way.
    """

    from app.backend.chat.service import ChatService, _visible_output_tokens

    service = object.__new__(ChatService)
    service._turn_generations = []
    service._record_generation(
        SimpleNamespace(
            text="<think>They said hi. Keep it short.</think>Hi there!",
            token_ids=list(range(120)),
        )
    )

    assert _visible_output_tokens("Hi there!", service._turn_generations) == 120


def test_an_answer_no_generation_produced_reports_no_count() -> None:
    """Composed by code, so there is nothing to measure — and nothing is claimed.

    "I could not finish that one" and the false-success replacement are written
    by the application. Reporting the routing generation's tokens under them
    would be the same defect wearing different words.
    """

    from app.backend.chat.service import _visible_output_tokens

    generations = [('{"action":"action","capability":"files.manage"}', 12)]

    assert _visible_output_tokens("I could not finish that one.", generations) is None
    assert _visible_output_tokens("", generations) is None
    assert _visible_output_tokens("anything", []) is None


def test_bundle_status_advertises_truthful_cooking_contract() -> None:
    service = _normaliser()
    service.model_bundle_runtime.describe = lambda: {
        "ready": True,
        "loaded": True,
        "configured_context_limit": 262144,
        "runtime_id": "runtime-1",
    }

    status = service.status()
    control = status["runtime_controls"]["reasoning_control"]

    assert control["label"] == "Response mode"
    assert control["setting"] == "reasoning_mode"
    assert control["modes"] == ["instant", "cooking", "lock_in"]
    assert control["selected"] == "instant"
    assert control["enforcement"] == "model_soft_switch_plus_template_boundary"
    assert status["generation_defaults"]["context_window_tokens"] == 32768
    assert status["generation_defaults"]["maximum_output_mode"] == "automatic"
    assert status["generation_defaults"]["maximum_output_tokens"] == 16384
    assert status["generation_limits"]["context_window_tokens"]["maximum"] == 262144
    assert status["generation_limits"]["context_window_tokens"]["presets"] == [
        16384,
        24576,
        32768,
        49152,
        65536,
        98304,
        131072,
        196608,
        262144,
    ]
    assert status["runtime_controls"]["maximum_output_control"][
        "manual_presets"
    ] == [256, 512, 1024, 2048, 4096, 8192, 16384, 32768]


def test_reasoning_modes_are_explicit_and_validated() -> None:
    service = _normaliser()

    defaults = service._normalise_generation_settings(None)
    assert defaults["reasoning_mode_requested"] == "instant"
    assert defaults["reasoning_mode"] == "instant"
    assert service._normalise_generation_settings({"reasoning_mode": " OFF "})[
        "reasoning_mode"
    ] == "instant"
    assert service._normalise_generation_settings({"reasoning_mode": "deep"})[
        "reasoning_mode"
    ] == "cooking"
    assert service._normalise_generation_settings({"reasoning_mode": "instant"})[
        "reasoning_mode"
    ] == "instant"
    with pytest.raises(ValueError, match="blink, cook, or lock_in"):
        service._normalise_generation_settings({"reasoning_mode": "maximum"})


def test_instant_runtime_copy_requires_direct_answer_without_mutating_source() -> None:
    source = [{"role": "user", "content": "List 300 distinct nouns."}]

    runtime = _apply_reasoning_mode(source, "instant")

    assert source == [{"role": "user", "content": "List 300 distinct nouns."}]
    assert runtime is not source
    assert runtime[-1]["content"] == "List 300 distinct nouns.\n\n/no_think"
    assert "/think" not in runtime[-1]["content"]
    assert runtime[-1]["content"].endswith("/no_think")


def test_instant_runtime_copy_removes_prior_cooking_trace_but_keeps_final_answer() -> None:
    source = [
        {"role": "user", "content": "Solve it."},
        {"role": "assistant", "content": "<think>private trace</think>\n\n42"},
        {"role": "user", "content": "Explain briefly."},
    ]

    runtime = _apply_reasoning_mode(source, "instant")

    assert source[1]["content"] == "<think>private trace</think>\n\n42"
    assert runtime[1] == {"role": "assistant", "content": "42"}
    assert all("private trace" not in message["content"] for message in runtime)
    assert runtime[-1]["content"].endswith("/no_think")


def test_missing_bundle_reasoning_default_preserves_legacy_auto_to_cooking() -> None:
    service = _normaliser()
    service.model_bundle.pop("reasoning_default")

    defaults = service._normalise_generation_settings(None)

    assert defaults["reasoning_mode_requested"] == "auto"
    assert defaults["reasoning_mode"] == "cooking"


def test_output_mode_resolves_automatic_ceiling_and_accepts_legacy_key_aliases() -> None:
    service = _normaliser()

    automatic = service._normalise_generation_settings(
        {
            "context_window_tokens": 16384,
            "maximum_output_mode": "automatic",
            "maximum_output_tokens": 8192,
        }
    )
    assert automatic["maximum_output_tokens"] == 8192
    assert automatic["maximum_output_tokens_requested"] == 8192

    manual = service._normalise_generation_settings(
        {"max_output_mode": "manual", "max_output_tokens": 1024}
    )
    assert manual["maximum_output_mode"] == "manual"
    assert manual["maximum_output_tokens"] == 1024
    assert manual["maximum_output_tokens_requested"] == 1024

    maximum = service._normalise_generation_settings(
        {
            "context_window_tokens": 262144,
            "maximum_output_mode": "automatic",
        }
    )
    assert maximum["maximum_output_tokens"] == 32768

    manual_32k = service._normalise_generation_settings(
        {
            "context_window_tokens": 65536,
            "maximum_output_mode": "manual",
            "maximum_output_tokens": 32768,
        }
    )
    assert manual_32k["maximum_output_tokens"] == 32768
    with pytest.raises(ValueError, match="smaller than the context window"):
        service._normalise_generation_settings(
            {
                "context_window_tokens": 32768,
                "maximum_output_mode": "manual",
                "maximum_output_tokens": 32768,
            }
        )

    with pytest.raises(ValueError, match="automatic or manual"):
        service._normalise_generation_settings({"maximum_output_mode": "unlimited"})


def test_automatic_web_search_is_an_explicit_per_response_control() -> None:
    service = _normaliser()

    assert service._normalise_generation_settings(None)["web_search_enabled"] is False
    assert service._normalise_generation_settings({"web_search_enabled": True})[
        "web_search_enabled"
    ] is False
    assert service._normalise_generation_settings(None)["research_profile"] == (
        "verification"
    )
    assert service._normalise_generation_settings(
        {"research_available": True, "web_search_enabled": True, "reasoning_mode": "instant"}
    )["research_profile"] == "instant"
    assert service._normalise_generation_settings(
        {"research_available": True, "web_search_enabled": True, "reasoning_mode": "cooking"}
    )["research_profile"] == "cooking"


def test_raw_local_reasoning_visibility_is_explicit_and_bounded() -> None:
    service = _normaliser()

    assert service._normalise_generation_settings(None)["reasoning_visibility"] == (
        "summaries"
    )
    assert service._normalise_generation_settings(
        {"reasoning_visibility": "raw_local"}
    )["reasoning_visibility"] == "raw_local"
    with pytest.raises(ValueError, match="summaries or raw_local"):
        service._normalise_generation_settings({"reasoning_visibility": "public"})


def test_reasoning_mode_switch_is_private_and_applied_to_latest_user_turn() -> None:
    source = [{"role": "user", "content": "What is your model name?"}]

    instant_messages = _apply_reasoning_mode(source, "instant")
    cooking_messages = _apply_reasoning_mode(source, "cooking")

    assert source == [{"role": "user", "content": "What is your model name?"}]
    assert instant_messages is not source
    assert instant_messages[-1]["content"] == (
        "What is your model name?\n\n/no_think"
    )
    assert instant_messages[-1]["content"].endswith("/no_think")
    assert cooking_messages[-1]["content"] == "What is your model name?\n\n/think"
    assert cooking_messages[-1]["content"].endswith("/think")

    assert _apply_reasoning_mode(source, "off")[-1]["content"].endswith("/no_think")
    assert _apply_reasoning_mode(source, "deep")[-1]["content"].endswith("/think")


def test_reasoning_mode_switch_targets_only_latest_user_turn() -> None:
    source = [
        {"role": "user", "content": "First"},
        {"role": "assistant", "content": "Answer"},
        {"role": "user", "content": "Second"},
    ]

    runtime_messages = _apply_reasoning_mode(source, "cooking")

    assert source[-1]["content"] == "Second"
    assert runtime_messages[0]["content"] == "First"
    assert runtime_messages[-1]["content"] == "Second\n\n/think"
    assert runtime_messages[-1]["content"].endswith("/think")


def test_automatic_cooking_does_not_infer_output_length_from_prompt_length() -> None:
    first_turn = [{"role": "user", "content": "hi"}]
    assert _automatic_cooking_output_budget("hi", first_turn, 32768) == 32768
    assert (
        _automatic_cooking_output_budget(
            "Explain this behavior clearly in a few paragraphs.",
            [{"role": "user", "content": "Explain it"}],
            32768,
        )
        == 32768
    )
    long_history = [
        {"role": "user", "content": "x" * 18000},
        {"role": "assistant", "content": "y" * 18000},
        {"role": "user", "content": "continue"},
    ]
    assert (
        _automatic_cooking_output_budget("continue", long_history, 32768)
        == 32768
    )
    assert _automatic_cooking_output_budget("hi", first_turn, 128) == 128
    assert _cooking_private_reasoning_budget(128) == 128
    assert _cooking_private_reasoning_budget(1024) == 256
    assert _cooking_private_reasoning_budget(4096) == 1024
    assert _cooking_private_reasoning_budget(32768) == 8192


def test_cooking_reasoning_allowance_is_independent_and_validated():
    service=_normaliser()
    settings=service._normalise_generation_settings({'context_window_tokens':65536,
        'maximum_output_mode':'automatic','reasoning_mode':'cooking','cooking_reasoning_tokens':256})
    assert settings['cooking_reasoning_tokens']==256
    assert settings['maximum_output_tokens']==32768
    for value in (0,255,8193,True,'1024'):
        with pytest.raises(ValueError,match='cooking_reasoning_tokens'):
            service._normalise_generation_settings({'cooking_reasoning_tokens':value})


def test_direct_recovery_uses_private_memo_without_exposing_it_as_protocol() -> None:
    instruction = _direct_recovery_instruction(
        "respond",
        "Explain transactions.",
        "Compare atomicity, isolation, logging, and constraints.",
    )
    assert instruction.startswith("Answer the latest user request")
    assert "private reasoning memo" in instruction
    assert "Compare atomicity" in instruction

    identity = _direct_recovery_instruction(
        "identity",
        "What is your name?",
        "memo that must not alter identity recovery",
    )
    assert "memo that must not alter" not in identity


def test_reasoning_mode_requires_a_user_turn() -> None:
    with pytest.raises(ValueError, match="requires a user turn"):
        _apply_reasoning_mode([{"role": "system", "content": "Local"}], "instant")


def test_live_generation_preview_is_bounded_and_not_inferred_from_prose() -> None:
    assert _bounded_generation_preview(None) is None
    assert _bounded_generation_preview({"tail_text": ""}) is None
    preview = _bounded_generation_preview(
        {"kind": "reasoning", "tail_text": "x" * 1400, "token_count": "7"}
    )
    assert preview == {
        "kind": "reasoning",
        "tail_text": "",
        "token_count": 7,
        "character_count": 1400,
        "summary": "Working through the request",
        "focus": "reasoning",
        "summary_kind": "topic_hint",
    }
    assert _bounded_generation_preview(
        {"kind": "reasoning", "tail_text": "private", "token_count": 1},
        include_reasoning_text=True,
    )["tail_text"] == "private"
    assert _bounded_generation_preview(
        {"kind": "ordinary prose", "tail_text": "answer", "token_count": -4}
    ) == {
        "kind": "output",
        "tail_text": "answer",
        "token_count": 0,
        "character_count": 6,
        "summary": "Writing the answer",
        "focus": "answer",
        "summary_kind": "output_progress",
    }


def test_a_turn_reports_how_it_finished_rather_than_only_that_it_did() -> None:
    # Instant and Cooking are two different promises. A Cooking turn that
    # finishes says it was cooked, permanently, rather than being flattened
    # back into a generic "Done"; and a turn that was stopped or ran out of
    # room never claims either.
    from app.backend.chat.service import _turn_completion

    class _Response:
        def __init__(self, cancelled: bool = False) -> None:
            self.cancelled = cancelled

    class _Turn:
        def __init__(self, status: str = "") -> None:
            self.details = {"status": status} if status else {}

    def completion(details, *, response=None, turn=None, waiting=False):
        return _turn_completion(
            details,
            response=response or _Response(),
            turn=turn,
            proposal_pending=waiting,
        )

    assert completion({"reasoning_mode_effective": "instant"}) == "done"
    assert completion({"reasoning_mode_effective": "cooking"}) == "cooked"
    assert completion({}, response=_Response(cancelled=True)) == "stopped"
    assert completion({}, waiting=True) == "waiting"
    assert completion({}, turn=_Turn("failed")) == "failed"
    assert completion({}, turn=_Turn("exhausted")) == "partial"
    assert completion({"finish_reason": "maximum_output"}) == "partial"
    # A cancelled Cooking turn is stopped, not cooked.
    assert (
        completion(
            {"reasoning_mode_effective": "cooking"},
            response=_Response(cancelled=True),
        )
        == "stopped"
    )


def test_cooking_reasoning_never_reaches_the_answer() -> None:
    # Live failure: a Cooking turn wrote its whole <think> block into the
    # message and it was rendered at the user above the actual reply.
    from app.backend.chat.service import _separate_reasoning

    answer, reasoning = _separate_reasoning(
        "<think>\nThe user just said hi again. Keep it friendly.\n</think>\n\n"
        "Hi there! How can I help you today?"
    )
    assert answer == "Hi there! How can I help you today?"
    assert "friendly" in reasoning
    assert "<think>" not in answer

    # A turn that runs out of output mid-thought leaves the block unclosed;
    # that is still reasoning and still must not be shown as the answer.
    answer, reasoning = _separate_reasoning("<think>halfway through a thought")
    assert answer == ""
    assert reasoning == "halfway through a thought"

    # An ordinary reply is untouched, and carries no reasoning.
    answer, reasoning = _separate_reasoning("Just an answer.")
    assert answer == "Just an answer."
    assert reasoning == ""
