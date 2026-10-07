"""The live chat path: a message goes in, the model routes it, work comes out.

These drive the real _generate_turn, not the orchestration utilities. The model
is a scripted stub so the routing is what is under test, but everything after
the model reply is the production path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.chat.dispatch import build_turn_instruction, read_decision
from app.backend.imaging import ImageJobRegistry, ImageOrchestrator
from app.backend.imaging.store import ImageJobStore

BOILABIN = (
    "Create a distinctive premium Boilabin ecommerce brand logo and icon. "
    "Boilabin is an ecommerce marketplace. Deliverables: a standalone symbol, a "
    "horizontal lockup with the Boilabin wordmark, and a small monochrome "
    "version. Minimal, premium, flat vector, negative space. Do not use a "
    "shopping cart. No shopping bag. Avoid a delivery truck. No generic globe."
)

IMAGE_DECISION = json.dumps(
    {
        "action": "generate_image",
        "reason": "The user asked for a brand identity.",
        "brief": {
            "subject": "Boilabin ecommerce brand identity",
            "image_type": "logo",
            "brand": "Boilabin",
            "deliverables": [
                "standalone symbol",
                "horizontal symbol with Boilabin wordmark",
                "small monochrome version",
            ],
        },
    }
)


def _application(tmp_path: Path, *, replies):
    """A real Application whose model is a scripted reply queue."""

    from tests.test_chat_actions import _application as build

    app = build(tmp_path) if callable(build) else None
    return app


# ------------------------------------------------------- decision detection


def test_a_plain_answer_is_not_mistaken_for_a_decision() -> None:
    # The common case must cost nothing and must never be parsed as routing.
    assert read_decision("LoRA adds low-rank adapters to frozen weights.") is None
    assert read_decision("Here is a JSON example: {\"a\": 1}") is None
    assert read_decision("") is None


def test_a_decision_is_recognised_through_thinking_and_fences() -> None:
    assert read_decision(IMAGE_DECISION)["action"] == "generate_image"
    assert (
        read_decision(f"<think>\nA logo request.\n</think>\n\n```json\n{IMAGE_DECISION}\n```")[
            "action"
        ]
        == "generate_image"
    )


def test_respond_without_an_answer_is_treated_as_ordinary_prose() -> None:
    # Otherwise a model that wraps its answer would lose it.
    assert read_decision('{"action":"respond","reason":"just answering"}') is None


def test_the_instruction_only_offers_what_is_reachable() -> None:
    # Reading the web and operating the computer are both the user's switches.
    # With neither on, the turn offers nothing at all and the model answers —
    # research used to be offered whatever the switch said, which is how "hi"
    # with Research off came back having searched the web.
    none_available = build_turn_instruction(
        image_available=False, has_previous_image=False
    )
    assert none_available == ""

    research_only = build_turn_instruction(
        image_available=False, has_previous_image=False, research_available=True
    )
    assert "research" in research_only
    # The single-action and plan job types are not offered. Matched with the
    # trailing em dash because the envelope example legitimately contains the
    # word "action" as the field name every decision carries.
    assert '"action" —' not in research_only
    assert '"plan" —' not in research_only

    images_only = build_turn_instruction(image_available=True, has_previous_image=False)
    assert "generate_image" in images_only
    assert "revise_image" not in images_only

    with_history = build_turn_instruction(
        image_available=True, has_previous_image=True, capabilities=["browser.control"]
    )
    assert "revise_image" in with_history
    assert "browser.control" in with_history
    # Small enough to sit in front of every chat turn.
    assert len(with_history) < 1_600


def test_agent_mode_tells_the_same_brain_to_act_rather_than_explain() -> None:
    off = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["application.launch"],
    )
    on = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["application.launch"],
        agent_mode=True,
    )
    # Both modes now open the same way, and this reverses a decision recorded
    # here earlier. Inverting the default inside agent mode did make the model
    # act — on everything. A greeting reached a model already told "you are
    # operating this computer for the user" and "the user asked you to do this
    # on their computer", and it ran a terminal command and a file operation on
    # "hi", then introduced itself as ready to operate the computer.
    #
    # The switch cannot know what the turn wants, so it must not claim to. What
    # separates acting from answering is the request, and the counterweights
    # below carry the bias that the opening line used to.
    assert off.startswith("Answer normally")
    assert on.startswith("Answer normally")
    assert "which is not a reason to use one" in on
    assert "Explaining how they could do it themselves is not doing it" in on
    for name in ('"action" —', '"plan" —', "action example:"):
        assert name in off and name in on


def test_agent_mode_promises_nothing_when_no_capability_is_granted() -> None:
    # Claiming the computer can be used while every capability is denied is
    # exactly the untruth this unification exists to remove. Research stays,
    # because it never needed a capability.
    text = build_turn_instruction(
        image_available=False, has_previous_image=False, agent_mode=True
    )
    assert '"action" —' not in text
    assert '"plan" —' not in text
    assert "Agent mode is on" not in text


def test_no_image_paragraph_when_the_backend_is_unavailable() -> None:
    text = build_turn_instruction(
        image_available=False, has_previous_image=False, capabilities=["terminal.execute"]
    )
    assert "generate_image" not in text
    assert "brief" not in text
    assert "terminal.execute" in text


# ------------------------------------------------------------- persistence


def _store(tmp_path: Path):
    from app.backend.database.control import Database

    database = Database(tmp_path / "control.db")
    database.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        ("11111111-1111-4111-8111-111111111111", "A", "2026-01-01", "2026-01-01"),
    )
    database.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        ("22222222-2222-4222-8222-222222222222", "B", "2026-01-01", "2026-01-01"),
    )
    return database, ImageJobStore(database)


def _job(orchestrator, conversation_id, request=BOILABIN, brief=None):
    job = orchestrator.prepare(
        {"brief": brief or json.loads(IMAGE_DECISION)["brief"]},
        original_request=request,
        conversation_id=conversation_id,
    )
    job.conversation_id = conversation_id
    return job


def test_an_image_job_survives_a_restart(tmp_path: Path) -> None:
    database, store = _store(tmp_path)
    orchestrator = ImageOrchestrator(generate=lambda brief, job: {"artifact": "a.png"})
    conversation = "11111111-1111-4111-8111-111111111111"

    job = _job(orchestrator, conversation)
    job.status = "completed"
    job.artifact = "C:/artifacts/a.png"
    store.save(job)

    # A fresh store, as after an application restart.
    reopened = ImageJobStore(database)
    restored = reopened.latest_for_conversation(conversation)

    assert restored is not None
    assert restored.job_id == job.job_id
    assert restored.brief.brand == "Boilabin"
    assert restored.brief.image_type == "logo"
    assert "standalone symbol" in restored.brief.deliverables
    # The prohibitions the user gave are still on the brief.
    negatives = " ".join(restored.brief.negative_constraints).casefold()
    assert "cart" in negatives and "truck" in negatives
    # And the original request is still there to revise against.
    assert "Boilabin" in restored.brief.original_request


def test_a_revision_after_restart_keeps_the_whole_original_brief(tmp_path: Path) -> None:
    database, store = _store(tmp_path)
    orchestrator = ImageOrchestrator(generate=lambda brief, job: {"artifact": "a.png"})
    conversation = "11111111-1111-4111-8111-111111111111"

    first = _job(orchestrator, conversation)
    first.status = "completed"
    first.artifact = "C:/artifacts/first.png"
    store.save(first)

    # A brand new runtime, with nothing in memory.
    fresh = ImageOrchestrator(
        generate=lambda brief, job: {"artifact": "b.png"},
        registry=ImageJobRegistry(),
    )
    parent = ImageJobStore(database).latest_for_conversation(conversation)
    fresh.registry.add(parent)

    revision = fresh.prepare_revision(
        {"action": "revise_image"},
        feedback="Make that logo more geometric.",
        conversation_id=conversation,
        parent_job_id=parent.job_id,
    )
    rendered = revision.brief.render()

    assert revision.revision == 2
    assert revision.parent_job_id == first.job_id
    assert "Boilabin" in rendered
    assert "monochrome" in rendered.casefold()
    # The prohibition survives the restart, in negative conditioning.
    assert "cart" in revision.brief.render_negative().casefold()
    assert "cart" not in rendered.casefold()
    # Emphatically not a five-word prompt. The threshold is lower than it was
    # only because the prohibitions moved out of this string, not because the
    # brief carries less.
    assert len(rendered) > 200


def test_a_revision_never_reaches_into_another_conversation(tmp_path: Path) -> None:
    database, store = _store(tmp_path)
    orchestrator = ImageOrchestrator(generate=lambda brief, job: {"artifact": "a.png"})
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"

    boilabin = _job(orchestrator, first)
    boilabin.status = "completed"
    store.save(boilabin)

    cow = _job(
        orchestrator,
        second,
        request="Generate an image of a cow.",
        brief={"subject": "a cow in a field", "image_type": "photograph"},
    )
    cow.status = "completed"
    store.save(cow)

    # The conversation is authoritative, not recency.
    assert store.latest_for_conversation(first).job_id == boilabin.job_id
    assert store.latest_for_conversation(second).job_id == cow.job_id
    assert "cow" in store.latest_for_conversation(second).brief.subject
    assert "Boilabin" in store.latest_for_conversation(first).brief.subject


def test_deleting_a_conversation_leaves_no_orphaned_image_metadata(
    tmp_path: Path,
) -> None:
    database, store = _store(tmp_path)
    orchestrator = ImageOrchestrator(generate=lambda brief, job: {"artifact": "a.png"})
    conversation = "11111111-1111-4111-8111-111111111111"
    store.save(_job(orchestrator, conversation))

    database.execute("DELETE FROM conversations WHERE id = ?", (conversation,))

    assert store.latest_for_conversation(conversation) is None
    remaining = database.fetch_all("SELECT job_id FROM image_generation_jobs", ())
    assert remaining == []


def test_a_revision_chain_is_stored_with_its_parent(tmp_path: Path) -> None:
    database, store = _store(tmp_path)
    orchestrator = ImageOrchestrator(generate=lambda brief, job: {"artifact": "a.png"})
    conversation = "11111111-1111-4111-8111-111111111111"

    first = _job(orchestrator, conversation)
    first.status = "completed"
    store.save(first)

    revision = orchestrator.prepare_revision(
        {"action": "revise_image"},
        feedback="sharper",
        conversation_id=conversation,
        parent_job_id=first.job_id,
    )
    revision.conversation_id = conversation
    store.save(revision)

    stored = store.get(revision.job_id)
    assert stored.parent_job_id == first.job_id
    assert stored.revision == 2
    assert stored.brief.brand == "Boilabin"


def test_a_bare_list_of_plan_nodes_is_a_plan_not_an_answer() -> None:
    # Live failure: "Open Notepad, bring it forward, then capture the screen"
    # produced exactly the right three nodes as a top-level JSON array with no
    # {"action":"plan"} wrapper, and the raw brackets were printed at the user.
    from app.backend.chat.dispatch import read_decision

    decision = read_decision(
        '[\n'
        ' {"node": "open", "capability": "application.launch", '
        '"arguments": {"target": "notepad"}},\n'
        ' {"node": "window.control", "capability": "bring_to_front", '
        '"arguments": {"focus_app": "notepad"}},\n'
        ' {"node": "screen.capture", "capability": "take_screenshot", '
        '"arguments": {}}\n'
        ']'
    )

    assert decision is not None
    assert decision["action"] == "plan"
    assert [node["node"] for node in decision["nodes"]] == [
        "open",
        "window.control",
        "screen.capture",
    ]


def test_an_ordinary_list_in_an_answer_stays_an_answer() -> None:
    from app.backend.chat.dispatch import read_decision

    assert read_decision('["milk", "bread"]') is None
    assert read_decision("Here are three options: 1. a 2. b 3. c") is None
    assert read_decision('[{"title": "a note"}]') is None


def test_a_json_document_is_never_the_answer_whatever_its_keys() -> None:
    # Live failure: asked for Gen4 SSD prices in Bangladesh, the model replied
    # with a well-formed research object — {"research": {"subject": ...,
    # "sources_analyzed": [...], "key_findings": {...}}} — and eighty lines of
    # raw braces were printed at the user.
    #
    # It is still never shown. What changed is what happens instead: a document
    # that names exactly one job is read as that job, so this now runs real
    # research on that subject rather than being rewritten into prose the model
    # composes from memory. The invented sources are discarded either way; only
    # one of the two outcomes actually goes and looks.
    from app.backend.chat.dispatch import (
        looks_like_a_decision_attempt,
        read_decision,
    )

    dump = json.dumps(
        {
            "research": {
                "subject": "Gen4 SSD 2TB Cheapest in Bangladesh",
                "sources_analyzed": ["Daraz Bangladesh", "Pickaboo"],
                "key_findings": {
                    "top_affordable_models": [
                        {"model": "WD Blue SN580 2TB", "approx_price_bdt": "14,500"}
                    ]
                },
            }
        },
        indent=1,
    )

    decision = read_decision(dump)
    assert decision is not None and decision["action"] == "research"
    # It was a decision, so it is no longer a broken one.
    assert looks_like_a_decision_attempt(dump) is False

    # A document that names no job at all is still not an answer.
    findings = json.dumps({"summary": "cheap drives", "items": [1, 2, 3]})
    assert read_decision(findings) is None
    assert looks_like_a_decision_attempt(findings) is True


def test_prose_that_merely_contains_a_brace_is_still_an_answer() -> None:
    from app.backend.chat.dispatch import looks_like_a_decision_attempt

    for answer in (
        "The cheapest is the WD Blue SN580 at about 14,500 BDT.",
        'In JSON you would write {"a": 1} but here is the plain answer.',
        "Use { and } to delimit a block in C.",
        "",
    ):
        assert looks_like_a_decision_attempt(answer) is False


def test_job_names_written_as_flags_are_still_a_decision() -> None:
    """The live image-generation failure, verbatim.

    Asked "Generate an image of a cow." on the installed application, the model
    read the list of job names as a set of booleans to set rather than as
    values for an action field:

        {"research": false, "generate_image": true, "brief": {...}}

    The choice was right and the shape was wrong, so nothing could read it, the
    repair pass rewrote it as prose, and the user was told "You should see the
    visual result now" while no image existed. One job named and every other
    explicitly declined is unambiguous, so it is normalised rather than lost.
    """

    decision = read_decision(
        json.dumps(
            {
                "research": False,
                "generate_image": True,
                "brief": {
                    "subject": "cow",
                    "image_type": "photograph-style illustration",
                    "style": "realistic, soft lighting, natural setting",
                },
            }
        )
    )
    assert decision is not None
    assert decision["action"] == "generate_image"
    assert decision["brief"]["subject"] == "cow"
    # The declined job must not survive as an argument.
    assert "research" not in decision


def test_a_job_named_with_its_arguments_is_a_decision() -> None:
    # The same slip without the flags: the job name is the key and its payload
    # is the value. Merged into the decision it belongs to.
    decision = read_decision('{"generate_image": {"subject": "a red barn"}}')
    assert decision["action"] == "generate_image"
    assert decision["subject"] == "a red barn"

    plain = read_decision('{"research": "cheapest 2TB NVMe in Bangladesh"}')
    assert plain["action"] == "research"
    assert plain["question"] == "cheapest 2TB NVMe in Bangladesh"


def test_two_jobs_at_once_is_not_a_decision() -> None:
    # Ambiguity is not repaired by guessing. Two claimed jobs means the model
    # did not decide, and the correction pass is the right place for that.
    assert (
        read_decision('{"research": true, "generate_image": true, "brief": {}}')
        is None
    )


def test_an_instruction_to_the_app_is_not_a_description_of_a_picture() -> None:
    """Seen live: TASK: other — Generate an image of a cow.

    The model returned a decision with no subject of its own, so the subject
    fell back to the user's sentence and the imperative was handed to the
    diffusion model as the thing to draw.
    """

    from app.backend.imaging.brief import build_brief, subject_from_request

    assert subject_from_request("Generate an image of a cow.") == "a cow"
    assert subject_from_request("please make me a picture of a red barn") == "a red barn"
    assert subject_from_request("Draw a photo showing two cats") == "two cats"
    # The wrapper is only removed where it is genuinely a wrapper. Stripping to
    # the word after "for" here would throw the logo away.
    logo = "Create a minimal professional logo for a fictional company called North Arc."
    assert subject_from_request(logo) == logo.rstrip(".")

    brief = build_brief({}, original_request="Generate an image of a cow.")
    assert brief.subject == "a cow"
    # "other" is a slot in a schema, not a kind of picture.
    assert brief.render().startswith("TASK: image — a cow")


def test_a_capability_says_what_it_is_for() -> None:
    """A dotted identifier is a name, not an affordance.

    The routing decision listed "files.manage, terminal.execute,
    application.launch" and nothing else, which tells a small model the names
    of seven things and what none of them does.
    """

    text = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["files.manage", "terminal.execute"],
    )
    assert "files.manage — look at and change files and folders" in text
    assert "terminal.execute — run a command" in text


def test_a_configured_mail_service_outranks_unrelated_desktop_apps() -> None:
    text = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["application.launch", "browser.control"],
        connectors=["mail.local: search(query), apply_label(message_ids,label)"],
        agent_mode=True,
    )

    assert "Prefer a matching service" in text
    assert "A mail service handles inbox" in text
    assert "Discord is not a substitute for an inbox" in text
    assert "mail.local" in text


def test_naming_what_to_delete_is_not_a_reason_to_describe_it() -> None:
    """The rule that produced the failure, and its replacement.

    Asked to delete three named log files, the model wrote out the procedure —
    obeying "if the request would delete ... answer describing exactly what you
    would do" exactly. What deserves a pause is destruction whose scope the
    user has not fixed, not destruction.
    """

    text = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["files.manage"],
        agent_mode=True,
    )
    assert "A goal they describe is already authorised" in text
    assert "could reach beyond what they asked for" in text
    # The unconditional form is gone.
    assert "would delete, overwrite or send something, answer describing" not in text


def test_not_knowing_the_files_is_not_the_same_as_not_knowing_the_user() -> None:
    """The two uncertainties were one, and the cautious half swallowed both.

    Told to delete the log files in a named folder and keep the notes, the
    model asked which files were meant. It knew exactly what was wanted and
    only lacked the list — which is something it can go and look at.
    """

    text = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["files.manage"],
        agent_mode=True,
    )
    assert "If you do not know what they want, ask" in text
    assert "not yet which things it applies to, LOOK" in text
    assert "Do not ask a person for something you can see for yourself" in text


def test_a_single_action_is_shown_its_envelope_like_a_plan_is() -> None:
    """A plan had an example and one action did not.

    Asked to open a page it had already verified, the model wrote a paragraph
    about the page. It knew exactly what a multi-step object looked like and
    had to invent the one-step one.
    """

    text = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["browser.control", "application.launch"],
    )
    assert '{"action":"action","capability":' in text
    # The shape is given; the choice is not.
    assert "<one listed below>" in text

    # No capability, no action, no example for one.
    without = build_turn_instruction(image_available=True, has_previous_image=False)
    assert '"action":"action"' not in without


def test_the_instruction_shows_the_object_it_asks_for() -> None:
    # Naming the choices without showing the envelope is what taught the model
    # to answer in flags. A small model copies an example far more reliably
    # than it infers a grammar.
    text = build_turn_instruction(image_available=True, has_previous_image=False)
    assert '{"action":' in text


def test_a_broken_decision_object_is_still_recognised() -> None:
    from app.backend.chat.dispatch import (
        looks_like_a_decision_attempt,
        recover_agent_route,
    )

    # Invalid JSON, but visibly reaching for a decision.
    assert looks_like_a_decision_attempt('{"action":"plan","nodes":[{') is True
    # Invalid JSON and reaching for nothing: prose starting with a brace.
    assert looks_like_a_decision_attempt('{this is not json at all') is False

    # An authorised agent turn recovers only the generic route.  It never
    # carries a truncated command, invented connector, or partial arguments
    # across the trust boundary; AgentLoop will select from the live registry.
    broken = '{"action":"plan","nodes":[{"connector":"made.up"}'
    recovered = recover_agent_route(
        broken,
        agent_mode=True,
        capabilities=["files.manage"],
    )
    assert recovered == {
        "action": "action",
        "reason": (
            "Recover the requested computer task through the "
            "capability-aware agent loop."
        ),
    }
    assert "connector" not in recovered

    # Authority and an enabled capability are both required.  Plain prose is
    # never promoted just because Agent mode is available.
    assert (
        recover_agent_route(
            broken,
            agent_mode=False,
            capabilities=["files.manage"],
        )
        is None
    )
    assert recover_agent_route(broken, agent_mode=True, capabilities=[]) is None
    assert (
        recover_agent_route(
            "hello",
            agent_mode=True,
            capabilities=["files.manage"],
        )
        is None
    )
