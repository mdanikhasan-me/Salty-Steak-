"""Live-path routing: the text model decides, the image engine renders.

These tests drive the SAME production orchestration interface a real model
reply goes through. The model is a deterministic stub so the routing is what
is under test, not the model's taste.
"""

from __future__ import annotations

import json

import pytest

from app.backend.chat.orchestrator import (
    GENERATE_IMAGE,
    PLAN,
    RESPOND,
    REVISE_IMAGE,
    OrchestrationError,
    build_decision_prompt,
    conversation_request,
    parse_decision,
)
from app.backend.chat.task_runtime import TaskContext
from app.backend.imaging import (
    ImageJobRegistry,
    ImageOrchestrationError,
    ImageOrchestrator,
    RenderBrief,
    build_brief,
    inspect,
)

# The request that exposed the failure, in the shape a user actually types.
BOILABIN = """Create a distinctive premium Boilabin ecommerce brand logo and icon.

Boilabin is an ecommerce marketplace. I want an original symbol with a subtle
conceptual relationship to the letter B, suggesting interconnection, exchange,
movement, discovery and many products converging. It should feel like modern
commerce, trust, reliability and fast convenient shopping, and scale
internationally.

Deliverables:
1. a standalone symbol
2. a horizontal lockup of the symbol with the Boilabin wordmark
3. a small monochrome version that still reads at 16px

Visual direction: minimal, premium, precise geometry, clean silhouette, clever
use of negative space, flat vector. One dominant sophisticated brand colour
with a neutral supporting colour. Plain neutral background, crisp vector
identity presentation.

Do not use a shopping cart. No shopping bag. Avoid a delivery truck. Do not use
a package as the primary symbol. No price tag, no storefront, no generic globe.
Never a plain B in a circle. Avoid random gradients, neon, 3D, glassmorphism and
sparkles. No AI startup aesthetic, no mascot, no mockup. Do not imitate named
marketplace brands."""


def _generator(**overrides):
    """A stand-in image backend that records exactly what it was told."""

    seen = []

    def generate(brief, job):
        seen.append({"brief": brief, "rendered": brief.render(), "job": job})
        return {
            "artifact": overrides.get("artifact", f"C:/artifacts/{job.job_id}.png"),
            "dimensions": "512x512",
            "backend": "steak-gen-1",
        }

    generate.seen = seen
    return generate


def _orchestrator(task=None, **kwargs):
    task = task or TaskContext(goal="image")
    generate = kwargs.pop("generate", None) or _generator()
    orchestrator = ImageOrchestrator(
        generate=generate, task=task, backend="steak-gen-1", **kwargs
    )
    return orchestrator, task, generate


# =================================================== TEST 1: image request


def test_1_a_branding_request_routes_to_image_generation() -> None:
    """No 'use the image tool' anywhere in the request. It still routes."""

    # The model decides semantically; the runtime never keyword-gates ahead of it.
    reply = json.dumps(
        {
            "action": "generate_image",
            "reason": "The user asked for a visual brand identity.",
            "brief": {
                "subject": "Boilabin ecommerce brand identity",
                "image_type": "logo",
                "brand": "Boilabin",
                "goal": "premium ecommerce brand logo identity",
                "deliverables": [
                    "standalone symbol",
                    "horizontal symbol with Boilabin wordmark",
                    "small monochrome version",
                ],
                "style": "minimal premium flat vector, precise geometry, negative space",
                "background": "plain neutral",
                "negative_constraints": ["shopping cart", "delivery truck"],
            },
        }
    )
    decision = parse_decision(reply)
    assert decision["action"] == GENERATE_IMAGE

    orchestrator, task, generate = _orchestrator()
    job = orchestrator.prepare(decision, original_request=BOILABIN)
    orchestrator.run(job)

    rendered = generate.seen[0]["rendered"]
    assert "Boilabin" in rendered
    assert "logo" in rendered.casefold()
    assert "standalone symbol" in rendered
    # Nothing else was reached for.
    assert task.metrics.screenshots == 0
    assert task.metrics.ui_automation_calls == 0
    assert task.metrics.api_calls == 0
    assert task.metrics.image_model_calls == 1


# =============================================== TEST 2: Boilabin regression


def test_2_boilabin_brief_keeps_brand_type_deliverables_and_negatives() -> None:
    """The regression fixture for the observed failure."""

    brief = build_brief(
        {
            "subject": "Boilabin ecommerce brand identity",
            "image_type": "logo",
            "brand": "Boilabin",
            "deliverables": [
                "standalone symbol",
                "horizontal symbol with Boilabin wordmark",
                "small monochrome version",
            ],
            # The model returned only two prohibitions out of the many given.
            "negative_constraints": ["shopping cart"],
        },
        original_request=BOILABIN,
    )

    assert "Boilabin" in brief.subject or brief.brand == "Boilabin"
    assert brief.image_type == "logo"
    joined = " | ".join(brief.deliverables).casefold()
    assert "standalone symbol" in joined
    assert "wordmark" in joined
    assert "monochrome" in joined

    # The prohibitions the model dropped were recovered from the user's words.
    negatives = " ; ".join(brief.negative_constraints).casefold()
    for forbidden in ("cart", "bag", "truck", "package", "circle", "gradient", "mascot"):
        assert forbidden in negatives, f"{forbidden} was lost from the brief"

    # The prohibitions go to negative conditioning, never into the positive
    # prompt: a diffusion model draws what the positive prompt names, so a
    # rendered "DO NOT INCLUDE" put those very words across the picture.
    rendered = brief.render()
    assert "DO NOT INCLUDE" not in rendered
    assert "cart" not in rendered.casefold()
    negative_prompt = brief.render_negative().casefold()
    for forbidden in ("cart", "truck"):
        assert forbidden in negative_prompt

    # The decisive assertion: nothing unrelated took over the brief.
    for unrelated in ("bus", "highway", "vehicle"):
        assert unrelated not in rendered.casefold()


def test_2b_a_bus_brief_for_a_logo_request_is_refused_before_inference() -> None:
    """Fail closed: no GPU time on a brief that is demonstrably wrong."""

    orchestrator, task, generate = _orchestrator()
    wrong = {
        "brief": {
            "subject": "a red bus on a highway at sunset",
            "image_type": "photograph",
        }
    }

    with pytest.raises(ImageOrchestrationError) as failure:
        orchestrator.prepare(wrong, original_request=BOILABIN)

    assert failure.value.kind == "brief_mismatch"
    # The image model was never called.
    assert generate.seen == []
    assert task.metrics.image_model_calls == 0
    kinds = [event.kind for event in task.events]
    assert "image_brief_rejected" in kinds


def test_2c_one_bounded_correction_is_allowed_then_it_stops() -> None:
    attempts = {"count": 0}

    def rebrief(_complaint, _brief):
        attempts["count"] += 1
        return {
            "subject": "Boilabin ecommerce logo",
            "image_type": "logo",
            "brand": "Boilabin",
        }

    orchestrator, task, generate = _orchestrator(rebrief=rebrief)
    job = orchestrator.prepare(
        {"brief": {"subject": "a red bus on a highway", "image_type": "photograph"}},
        original_request=BOILABIN,
    )

    assert attempts["count"] == 1
    assert "Boilabin" in job.brief.subject
    assert "image_brief_corrected" in [event.kind for event in task.events]

    # A corrector that keeps returning nonsense does not loop forever.
    orchestrator2, _, generate2 = _orchestrator(
        rebrief=lambda _c, _b: {"subject": "a bus", "image_type": "photograph"}
    )
    with pytest.raises(ImageOrchestrationError):
        orchestrator2.prepare(
            {"brief": {"subject": "a bus", "image_type": "photograph"}},
            original_request=BOILABIN,
        )
    assert generate2.seen == []


# ====================================================== TEST 3: revision


def test_3_that_is_wrong_generate_the_logo_keeps_the_whole_original_brief() -> None:
    """The exact failure the user reported."""

    orchestrator, task, generate = _orchestrator()
    first = orchestrator.prepare(
        {
            "brief": {
                "subject": "Boilabin ecommerce brand identity",
                "image_type": "logo",
                "brand": "Boilabin",
                "deliverables": [
                    "standalone symbol",
                    "horizontal symbol with Boilabin wordmark",
                    "small monochrome version",
                ],
            }
        },
        original_request=BOILABIN,
        conversation_id="c1",
    )
    orchestrator.run(first)

    # Turn two says almost nothing on its own.
    feedback = "That is wrong. Please generate the logo."
    revision = orchestrator.prepare_revision(
        {"action": "revise_image"}, feedback=feedback, conversation_id="c1"
    )
    orchestrator.run(revision)

    rendered = generate.seen[-1]["rendered"]
    # Everything the user said one turn earlier is still there.
    assert "Boilabin" in rendered
    assert "logo" in rendered.casefold()
    assert "standalone symbol" in rendered
    assert "monochrome" in rendered.casefold()
    # The prohibitions survive the revision, in the negative conditioning
    # where they can subtract rather than in the prompt where they would draw.
    negative_prompt = revision.brief.render_negative().casefold()
    for forbidden in ("cart", "truck", "gradient"):
        assert forbidden in negative_prompt
        assert forbidden not in rendered.casefold()
    # And it is a revision of the first job, not a new one.
    assert revision.revision == 2
    assert revision.parent_job_id == first.job_id
    assert revision.previous_artifact == first.artifact
    # The prompt is emphatically not just the six words the user typed. The
    # threshold moved only because the prohibitions left this string for the
    # negative prompt asserted above; the brief itself is unchanged.
    assert len(rendered) > 200


# ================================================ TEST 4: partial revision


def test_4_keep_everything_but_change_one_thing() -> None:
    orchestrator, task, generate = _orchestrator()
    first = orchestrator.prepare(
        {
            "brief": {
                "subject": "Boilabin ecommerce brand identity",
                "image_type": "logo",
                "brand": "Boilabin",
                "style": "minimal premium flat vector with soft rounded geometry",
                "background": "plain neutral",
                "deliverables": ["standalone symbol", "horizontal wordmark lockup"],
            }
        },
        original_request=BOILABIN,
        conversation_id="c1",
    )
    orchestrator.run(first)

    revision = orchestrator.prepare_revision(
        {
            "action": "revise_image",
            "changes": {"style": "minimal premium flat vector, sharp angular geometry"},
        },
        feedback="Keep everything, but make the icon less rounded and more geometric.",
        conversation_id="c1",
    )
    orchestrator.run(revision)

    brief = revision.brief
    # Unchanged requirements survive untouched.
    assert brief.brand == "Boilabin"
    assert brief.background == "plain neutral"
    assert "standalone symbol" in brief.deliverables
    assert brief.image_type == "logo"
    assert any("cart" in item.casefold() for item in brief.negative_constraints)
    # The one thing asked for did change.
    assert "sharp angular" in brief.style
    assert "rounded" not in brief.style.casefold()
    assert brief.revision_note.startswith("Keep everything")
    assert revision.revision == 2


def test_4b_a_revision_cannot_smuggle_in_a_different_subject() -> None:
    orchestrator, _, _ = _orchestrator()
    first = orchestrator.prepare(
        {"brief": {"subject": "Boilabin logo", "image_type": "logo", "brand": "Boilabin"}},
        original_request=BOILABIN,
        conversation_id="c1",
    )
    orchestrator.run(first)

    revision = orchestrator.prepare_revision(
        {"changes": {"original_request": "draw a bus instead"}},
        feedback="make it sharper",
        conversation_id="c1",
    )
    assert "Boilabin" in revision.brief.subject
    assert revision.brief.original_request == first.brief.original_request


# ============================================== TEST 5: instant vs cooking


@pytest.mark.parametrize("mode", ["instant", "cooking"])
def test_5_both_reasoning_modes_offer_and_reach_image_generation(mode: str) -> None:
    """Modes may differ in thinking depth. They must not differ in routing."""

    from app.backend.chat.service import _apply_reasoning_mode

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": BOILABIN},
    ]
    runtime = _apply_reasoning_mode(history, mode)
    # Whatever the mode does to thinking, the user's request survives intact.
    assert "Boilabin" in runtime[-1]["content"]

    prompt = build_decision_prompt(
        capabilities=["browser.control"], image_available=True
    )
    assert "generate_image" in prompt

    decision = parse_decision(
        json.dumps(
            {
                "action": "generate_image",
                "reason": "brand identity request",
                "brief": {
                    "subject": "Boilabin brand identity",
                    "image_type": "logo",
                    "brand": "Boilabin",
                },
            }
        )
    )
    orchestrator, task, generate = _orchestrator()
    orchestrator.run(orchestrator.prepare(decision, original_request=BOILABIN))

    assert task.metrics.image_model_calls == 1
    assert "Boilabin" in generate.seen[0]["rendered"]


def test_5b_the_decision_prompt_stays_small_for_a_small_model() -> None:
    prompt = build_decision_prompt(
        capabilities=["browser.control", "terminal.execute", "ui.automation"],
        image_available=True,
        has_previous_image=True,
    )
    # Compact enough to sit in front of every turn without crowding the request.
    assert len(prompt) < 2_000
    assert prompt.count("\n") < 40


# =================================================== TEST 6: simple image


def test_6_generate_an_image_of_a_cow() -> None:
    decision = parse_decision(
        json.dumps(
            {
                "action": "generate_image",
                "reason": "The user asked for a picture.",
                "brief": {"subject": "a cow standing in a field", "image_type": "photograph"},
            }
        )
    )
    orchestrator, task, generate = _orchestrator()
    job = orchestrator.run(orchestrator.prepare(decision, original_request="Generate an image of a cow."))

    assert job.status == "completed"
    assert "cow" in generate.seen[0]["rendered"].casefold()
    # One image call, and nothing else at all.
    assert task.metrics.image_model_calls == 1
    assert task.metrics.screenshots == 0
    assert task.metrics.api_calls == 0
    assert task.metrics.ui_automation_calls == 0


def test_image_decision_accepts_model_authored_with_as_the_brief() -> None:
    decision = parse_decision(
        '{"action":"generate_image","with":{"subject":"Copper lighthouse",'
        '"image_type":"photograph","style":"blue-hour cinematic"}}'
    )

    assert decision["action"] == "generate_image"
    assert decision["brief"] == {
        "subject": "Copper lighthouse",
        "image_type": "photograph",
        "style": "blue-hour cinematic",
    }


def test_context_referent_is_grounded_in_the_assistant_description() -> None:
    latest = "Generate an image of what you just described."
    described = (
        "A rain-soaked neon street in Dhaka at blue hour, viewed through a "
        "cinematic 35mm lens, with rickshaw reflections across the pavement."
    )

    def author(**_kwargs):
        return {
            "subject": "a rain-soaked neon street in Dhaka with rickshaw reflections",
            "image_type": "concept_art",
            "style": "cinematic 35mm environment concept art",
            "background": "blue-hour city haze",
        }

    orchestrator = ImageOrchestrator(author=author)
    job = orchestrator.prepare(
        {
            "action": "generate_image",
            "brief": {
                "subject": "what you just described",
                "image_type": "concept_art",
            },
        },
        original_request=latest,
        latest_request=latest,
        reference_context=described,
    )

    assert "rain-soaked neon street" in job.brief.subject
    assert "what you just described" not in job.brief.subject


def test_complete_model_brief_skips_redundant_second_authoring_pass() -> None:
    author_calls: list[bool] = []

    def author(**_kwargs):
        author_calls.append(True)
        return {"subject": "wrong replacement"}

    orchestrator = ImageOrchestrator(author=author)
    request = "Create an image of a copper lighthouse at blue hour."
    job = orchestrator.prepare(
        {
            "action": "generate_image",
            "brief": {
                "subject": "Copper lighthouse",
                "image_type": "photograph",
                "style": "atmospheric cinematic lighting",
            },
        },
        original_request=request,
        latest_request=request,
    )

    assert author_calls == []
    assert "blue hour" in job.brief.goal.casefold()


def test_concrete_image_request_cannot_borrow_an_unrelated_reference() -> None:
    orchestrator = ImageOrchestrator()
    with pytest.raises(ImageOrchestrationError, match="subject_drift"):
        orchestrator.prepare(
            {
                "brief": {
                    "subject": "a 512 GB solid-state drive",
                    "image_type": "photograph",
                }
            },
            original_request="Generate an image of a cow.",
            latest_request="Generate an image of a cow.",
            reference_context="A detailed explanation of SSD prices and capacity.",
        )


# ============================================== TEST 7: non-image request


def test_7_an_explanation_request_does_not_become_an_image() -> None:
    decision = parse_decision(
        json.dumps(
            {"action": "respond", "reason": "This is a question about a technique."}
        )
    )
    assert decision["action"] == RESPOND

    # The guard would also catch a mistaken routing: a description of LoRA is
    # not a brief for a picture of one.
    from app.backend.imaging.brief import infer_image_type

    assert infer_image_type("Explain how LoRA works.") == "other"


def test_7b_descriptive_words_alone_do_not_imply_an_image() -> None:
    from app.backend.imaging.brief import infer_image_type

    # "picture" here is figurative; routing is the model's call, not a regex's.
    assert infer_image_type("Explain the big picture of how attention works") == "other"


# ==================================================== TEST 8: mixed task


def test_8_image_generation_is_a_node_inside_a_plan() -> None:
    """Research then draw, as one workflow."""

    from app.backend.automation.policy import PolicyEngine
    from app.backend.chat.task_runtime import COMPLETED
    from app.backend.connectors import ConnectorManager
    from app.backend.workflow import Executor, WorkflowEngine, build_plan

    task = TaskContext(goal="research then design")
    generate = _generator()
    images = ImageOrchestrator(generate=generate, task=task, backend="steak-gen-1")
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"), task=task)
    executor = Executor(
        connectors=registry,
        images=images,
        task=task,
        policy=registry.policy,
        authority_mode="full_access",
    )
    engine = WorkflowEngine(executor=executor, task=task)

    plan = build_plan(
        {
            "goal": "research directions then generate one logo concept",
            "nodes": [
                {
                    "node": "draw",
                    "capability": "image.generate",
                    "objective": "generate one logo concept for the brand",
                    "arguments": {
                        "request": "premium ecommerce logo for Boilabin",
                        "brief": {
                            "subject": "Boilabin ecommerce logo concept",
                            "image_type": "logo",
                            "brand": "Boilabin",
                        },
                    },
                }
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == COMPLETED
    assert task.metrics.image_model_calls == 1
    assert "Boilabin" in generate.seen[0]["rendered"]


# ======================================================== stop / metrics


def test_a_late_image_result_after_stop_is_detached_not_presented() -> None:
    task = TaskContext(goal="image")

    def slow(brief, job):
        # The user presses Stop while the model is mid-render.
        task.request_stop("user_requested")
        return {"artifact": "C:/artifacts/late.png", "dimensions": "512x512"}

    orchestrator = ImageOrchestrator(generate=slow, task=task)
    job = orchestrator.prepare(
        {"brief": {"subject": "a cow", "image_type": "photograph"}},
        original_request="Generate an image of a cow.",
    )
    finished = orchestrator.run(job)

    assert finished.status == "detached"
    # Kept for the audit trail, never offered as the current answer.
    assert orchestrator.result_for_model(finished)["status"] == "detached"


def test_stopping_before_generation_never_starts_the_image_model() -> None:
    task = TaskContext(goal="image")
    generate = _generator()
    orchestrator = ImageOrchestrator(generate=generate, task=task)
    job = orchestrator.prepare(
        {"brief": {"subject": "a cow", "image_type": "photograph"}},
        original_request="Generate an image of a cow.",
    )
    task.request_stop("user_requested")

    finished = orchestrator.run(job)

    assert finished.status == "cancelled"
    assert generate.seen == []


def test_image_calls_are_counted_apart_from_reasoning_calls() -> None:
    orchestrator, task, _ = _orchestrator()
    task.note_model_call(0.4)
    orchestrator.run(
        orchestrator.prepare(
            {"brief": {"subject": "a cow", "image_type": "photograph"}},
            original_request="Generate an image of a cow.",
        )
    )

    metrics = task.metrics.to_dict()
    assert metrics["model_calls"] == 1
    assert metrics["image_model_calls"] == 1
    # A diffusion run and a reasoning turn are never averaged together.
    assert metrics["image_generation_seconds"] >= 0.0


def test_the_model_is_given_metadata_not_pixels_or_a_whole_brief() -> None:
    orchestrator, _, _ = _orchestrator()
    job = orchestrator.run(
        orchestrator.prepare(
            {"brief": {"subject": "a cow", "image_type": "photograph"}},
            original_request="Generate an image of a cow.",
        )
    )

    payload = orchestrator.result_for_model(job)
    text = json.dumps(payload)
    assert payload["artifact"]
    assert "DO NOT INCLUDE" not in text
    assert len(text) < 600


# ============================================================ decisions


def test_a_reply_that_is_not_a_known_decision_is_rejected() -> None:
    with pytest.raises(OrchestrationError):
        parse_decision('{"action":"launch_missiles"}')
    with pytest.raises(OrchestrationError):
        parse_decision("I think we should generate an image!")


def test_a_thinking_block_before_the_decision_is_not_a_parse_failure() -> None:
    """Observed from the real local model, not imagined.

    Base Steak emits <think> around its reasoning in Cooking, and an empty
    block in Instant because the template still closes one.
    """

    reply = (
        "<think>\nThe user wants a picture of a cow, so this is an image "
        "request.\n</think>\n\n"
        '{"action":"generate_image","reason":"picture requested",'
        '"brief":{"subject":"a cow","image_type":"photograph"}}'
    )
    decision = parse_decision(reply)
    assert decision["action"] == GENERATE_IMAGE
    assert decision["brief"]["subject"] == "a cow"

    # An empty block, which is what Instant produces.
    assert parse_decision('<think>\n\n</think>\n\n{"action":"respond"}')["action"] == RESPOND


def test_a_fenced_json_reply_is_not_a_parse_failure() -> None:
    """Also observed from the real model on the Boilabin request.

    It wraps structured output in a markdown fence whenever the request looks
    like it wants formatted output, which a long brief always does.
    """

    reply = (
        "<think>\nA branding request.\n</think>\n\n"
        "```json\n"
        '{"action":"generate_image","reason":"brand identity",'
        '"brief":{"subject":"Boilabin logo","image_type":"logo","brand":"Boilabin"}}\n'
        "```"
    )
    decision = parse_decision(reply)
    assert decision["action"] == GENERATE_IMAGE
    assert decision["brief"]["brand"] == "Boilabin"

    # A bare fence with no language tag too.
    assert parse_decision('```\n{"action":"respond"}\n```')["action"] == RESPOND


def test_near_miss_decision_names_are_accepted() -> None:
    assert parse_decision('{"action":"image"}')["action"] == GENERATE_IMAGE
    assert parse_decision('{"action":"workflow"}')["action"] == PLAN
    assert parse_decision('{"action":"answer"}')["action"] == RESPOND


def test_revise_is_only_offered_once_an_image_exists() -> None:
    assert "revise_image" not in build_decision_prompt(image_available=True)
    assert "revise_image" in build_decision_prompt(
        image_available=True, has_previous_image=True
    )
    # And nothing about images at all when the backend is unavailable.
    assert "generate_image" not in build_decision_prompt(image_available=False)


def test_the_request_is_read_from_the_thread_not_the_last_line() -> None:
    history = [
        {"role": "user", "content": BOILABIN},
        {"role": "assistant", "content": "here is an image"},
        {"role": "user", "content": "That is wrong. Generate the logo."},
    ]

    request = conversation_request(history)

    # This is the fix for the handoff: the original brief is still in scope.
    assert "Boilabin" in request
    assert "monochrome" in request
    assert "That is wrong" in request


def test_the_model_may_name_the_capability_instead_of_the_job_type() -> None:
    """Observed from the real local model on "Open YouTube."

    The capability list sits directly below the job types in the instruction,
    so naming the capability is a reasonable thing for it to do — and more
    informative than the indirection. It is accepted rather than rejected, and
    the target is normalised out of whichever key the model chose.
    """

    from app.backend.chat.orchestrator import SINGLE_ACTION

    decision = parse_decision('{"action":"application.launch","app":"YouTube"}')

    assert decision["action"] == SINGLE_ACTION
    assert decision["capability"] == "application.launch"
    assert decision["arguments"]["target"] == "YouTube"


def test_an_invented_capability_is_still_refused() -> None:
    # Tolerating the shape would let any dotted string through as an action.
    for reply in ('{"action":"launch.missiles"}', '{"action":"mail.delete_all"}'):
        with pytest.raises(OrchestrationError):
            parse_decision(reply)


def test_a_connector_written_as_the_action_becomes_a_validated_one_node_plan() -> None:
    decision = parse_decision(
        '{"action":"mail.local","capability":"create_draft",'
        '"arguments":{"to":"alex@example.com","subject":"Hello"}}'
    )

    assert decision["action"] == "plan"
    assert decision["nodes"] == [
        {
            "node": "service_action",
            "connector": "mail.local",
            "operation": "create_draft",
            "arguments": {"to": "alex@example.com", "subject": "Hello"},
        }
    ]
