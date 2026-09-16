"""Live acceptance for the three decisions that had no runner.

The plans here come from the model-facing path, never from a hand-built Plan
in the test. That distinction is the whole point: a test that calls build_plan
itself proves the engine works and proves nothing about whether the model can
drive it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.automation.policy import PolicyEngine
from app.backend.chat.dispatch import TurnDispatcher, read_decision
from app.backend.chat.runners import LiveRunners, PlanRejected, replan_packet, validate_plan
from app.backend.chat.task_runtime import COMPLETED, FAILED, TaskContext
from app.backend.connectors import (
    ApprovalRequired,
    ConnectorManager,
    LocalCalendarConnector,
    LocalMailConnector,
    collect,
)


# ------------------------------------------------------------ plan validation


def _plan_payload(**overrides):
    payload = {
        "goal": "label the atlas mail",
        "nodes": [
            {
                "node": "make",
                "connector": "mail.local",
                "operation": "create_label",
                "arguments": {"name": "Project Atlas"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_alternate_key_and_key_args_normalise_only_when_the_capability_is_unique() -> None:
    plan = validate_plan(
        {
            "nodes": [
                {
                    "node": "open",
                    "key": {"name": "application.launch"},
                    "key_args": {"target": "discord"},
                }
            ]
        },
        goal="open Discord",
        capabilities=["application.launch"],
        connectors=[],
    )

    assert plan.nodes[0].capability == "application.launch"
    assert plan.nodes[0].arguments == {"target": "discord"}


def test_a_plan_naming_an_ungranted_capability_is_refused_whole() -> None:
    with pytest.raises(PlanRejected, match="not enabled"):
        validate_plan(
            {
                "goal": "x",
                "nodes": [
                    {"node": "a", "capability": "terminal.execute", "arguments": {}}
                ],
            },
            goal="x",
            capabilities=["application.launch"],
            connectors=[],
        )


def test_a_plan_naming_an_unconfigured_service_is_refused_whole() -> None:
    with pytest.raises(PlanRejected, match="not connected"):
        validate_plan(
            _plan_payload(), goal="x", capabilities=[], connectors=["calendar.local"]
        )


def test_a_capability_plan_missing_its_required_command_is_refused_before_execution() -> None:
    with pytest.raises(PlanRejected, match="arguments: command"):
        validate_plan(
            {
                "nodes": [
                    {
                        "node": "type",
                        "capability": "ui.automation",
                        "arguments": {"value": "hello"},
                    }
                ]
            },
            goal="draft a message",
            capabilities=["ui.automation"],
            connectors=[],
        )


def test_an_invalid_unobserved_ui_plan_recovers_to_the_iterative_agent() -> None:
    class _Broker:
        def invoke(self, _request):
            raise AssertionError("responding needs no broker call")

    runners = LiveRunners(
        broker=_Broker(),
        generate=lambda _messages: json.dumps(
            {"action": "respond", "answer": "The account needs sign-in first."}
        ),
        capabilities=["ui.automation"],
    )

    result = runners.run_plan(
        decision={
            "action": "plan",
            "nodes": [
                {
                    "node": "type",
                    "capability": "ui.automation",
                    "arguments": {"value": "hello"},
                }
            ],
        },
        request="Draft a message, but do not send it.",
    )

    assert result["invalid_plan_recovered_to_agent"] is True
    assert result["status"] == "completed"
    assert "sign-in" in result["answer"]
    assert result["agent_task"]["step_count"] == 1
    assert result["agent_task"]["mission_budget"]["step_limit"] == 8_192
    assert (
        result["agent_task"]["mission_budget"]["duration_limit_seconds"]
        == 8 * 60 * 60
    )


def test_connector_preflight_corrects_message_id_reference_for_thread_slot() -> None:
    manager = ConnectorManager(policy=PolicyEngine())
    manager.register(LocalMailConnector())
    plan = validate_plan(
        {
            "nodes": [
                {
                    "node": "find",
                    "connector": "mail.local",
                    "operation": "search",
                    "arguments": {},
                },
                {
                    "node": "inspect",
                    "connector": "mail.local",
                    "operation": "get_thread",
                    "arguments": {
                        "thread_id": {"$ref": "find.output.items[0].id"}
                    },
                }
            ]
        },
        goal="inspect a thread",
        capabilities=[],
        connectors=["mail.local"],
    )

    LiveRunners(connectors=manager)._preflight_connector_plan(plan)

    assert plan.node("inspect").arguments["thread_id"] == {
        "$ref": "find.output.items[0].thread_id"
    }


def test_connector_preflight_reads_single_ids_argument_as_get_id() -> None:
    manager = ConnectorManager(policy=PolicyEngine())
    manager.register(LocalMailConnector())
    plan = validate_plan(
        {
            "nodes": [
                {
                    "node": "find",
                    "connector": "mail.local",
                    "operation": "search",
                    "arguments": {},
                },
                {
                    "node": "review",
                    "connector": "mail.local",
                    "operation": "get",
                    "arguments": {"ids": {"$ref": "find.output.items[0].id"}},
                },
            ]
        },
        goal="review one message",
        capabilities=[],
        connectors=["mail.local"],
    )

    LiveRunners(connectors=manager)._preflight_connector_plan(plan)

    assert plan.node("review").arguments == {
        "id": {"$ref": "find.output.items[0].id"}
    }


def test_connector_preflight_expands_plural_batch_reference_and_rejects_placeholders() -> None:
    manager = ConnectorManager(policy=PolicyEngine())
    manager.register(LocalMailConnector())
    plan = validate_plan(
        {
            "nodes": [
                {
                    "node": "find",
                    "connector": "mail.local",
                    "operation": "search",
                    "arguments": {},
                },
                {
                    "node": "tag",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {
                        "ids": {"$ref": "find.output.items[1].id"},
                        "label": "Atlas",
                    },
                }
            ]
        },
        goal="organize the matching messages",
        capabilities=[],
        connectors=["mail.local"],
    )
    runners = LiveRunners(connectors=manager)

    runners._preflight_connector_plan(plan)

    assert plan.node("tag").arguments["ids"] == {
        "$ref": "find.output.items[*].id"
    }
    placeholder = validate_plan(
        {
            "nodes": [
                {
                    "node": "inspect",
                    "connector": "mail.local",
                    "operation": "get_thread",
                    "arguments": {"thread_id": "{unknown thread}"},
                }
            ]
        },
        goal="inspect",
        capabilities=[],
        connectors=["mail.local"],
    )
    with pytest.raises(PlanRejected, match="placeholder"):
        runners._preflight_connector_plan(placeholder)


def test_a_malformed_plan_never_partially_executes() -> None:
    for payload in (
        {"goal": "x", "nodes": []},
        {"goal": "x", "nodes": [{"node": "a", "depends_on": ["ghost"], "capability": "screen.capture"}]},
        {
            "goal": "x",
            "nodes": [
                {"node": "a", "capability": "screen.capture", "depends_on": ["b"]},
                {"node": "b", "capability": "screen.capture", "depends_on": ["a"]},
            ],
        },
    ):
        with pytest.raises(PlanRejected):
            validate_plan(
                payload, goal="x", capabilities=["screen.capture"], connectors=[]
            )


def test_a_plan_is_bounded_in_size() -> None:
    with pytest.raises(PlanRejected, match="limit"):
        validate_plan(
            {
                "goal": "x",
                "nodes": [
                    {"node": f"n{i}", "connector": "mail.local", "operation": "list_labels"}
                    for i in range(60)
                ],
            },
            goal="x",
            capabilities=[],
            connectors=["mail.local"],
        )


def test_a_live_plan_writes_a_resumable_checkpoint(tmp_path: Path) -> None:
    checkpoint = tmp_path / "missions" / "task.json"
    task = TaskContext(goal="label mail")
    registry = ConnectorManager(
        policy=PolicyEngine(authority_mode="full_access"), task=task
    )
    registry.register(LocalMailConnector())
    runners = LiveRunners(
        connectors=registry,
        task=task,
        authority_mode="full_access",
        checkpoint_path=checkpoint,
    )

    result = runners.run_plan(decision=_plan_payload(), request="label the atlas mail")

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result["status"] == COMPLETED
    assert saved["task_id"] == task.task_id
    assert saved["goal"] == "label the atlas mail"
    assert saved["nodes"][0]["state"] == "completed"


# ----------------------------------------------------- LIVE ACCEPTANCE A


def test_a_open_youtube_is_one_action_and_nothing_more() -> None:
    """A simple action must stay simple: no plan, no verifier, no screenshots."""

    calls: list[dict] = []

    class _Broker:
        def invoke(self, request):
            calls.append(dict(request))
            return {"status": "succeeded", "target": "https://www.youtube.com"}

    task = TaskContext(goal="open youtube")
    runners = LiveRunners(
        broker=_Broker(),
        task=task,
        capabilities=["application.launch", "browser.control", "screen.capture"],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "action",
                "reason": "The user wants YouTube open.",
                "capability": "application.launch",
                "arguments": {"target": "youtube"},
            }
        )
    )

    result = runners.run_action(decision=decision, request="Open YouTube.")

    assert result["status"] == "succeeded"
    assert len(calls) == 1
    assert calls[0]["capability"] == "application.launch"
    # Resolved to a real address by the runtime, after the model chose the tool.
    assert "youtube.com" in json.dumps(calls[0]["arguments"])
    # The cheap rung of the ladder, and nothing else touched.
    assert task.metrics.screenshots == 0
    assert task.metrics.vision_calls == 0
    assert task.metrics.raw_input_calls == 0
    assert task.metrics.replans == 0


def test_a_refused_call_is_reported_rather_than_ending_the_turn() -> None:
    # The broker refusing a malformed argument is the execution boundary
    # working. Raising out of the runner turned that into a failed generation
    # with no assistant message at all, so the user saw nothing.
    class _Broker:
        def invoke(self, request):
            raise ValueError("Unknown automation fields: ['app_name']")

    runners = LiveRunners(
        broker=_Broker(),
        task=TaskContext(goal="open youtube"),
        capabilities=["application.launch"],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "action",
                "reason": "The user wants YouTube open.",
                "capability": "application.launch",
                "arguments": {"target": "youtube", "elevate": True},
            }
        )
    )

    result = runners.run_action(decision=decision, request="Open YouTube.")

    assert result["status"] == "failed"
    assert "app_name" in result["error"]["message"]
    assert result["answer"]


# ----------------------------------------------------- LIVE ACCEPTANCE B


def _mailbox(count: int = 140) -> LocalMailConnector:
    mail = LocalMailConnector()
    mail.seed(
        [
            {
                "id": f"atlas-{index}",
                "from": "sam.okafor@northwind.example",
                "subject": f"Project Atlas milestone {index}",
            }
            for index in range(count)
        ]
        + [
            {"id": f"promo-{index}", "from": "deals@shop.example", "subject": "Sale"}
            for index in range(40)
        ]
    )
    return mail


def test_b_a_model_authored_plan_organises_a_mailbox(monkeypatch) -> None:
    """The plan comes from the model-facing path, not from build_plan here."""

    task = TaskContext(goal="organise atlas mail")
    registry = ConnectorManager(
        policy=PolicyEngine(authority_mode="ask_every_time"), task=task
    )
    mail = _mailbox()
    registry.register(mail)

    model_calls = {"count": 0}

    def generate(messages):
        model_calls["count"] += 1
        return "{}"

    runners = LiveRunners(
        connectors=registry,
        generate=generate,
        task=task,
        capabilities=[],
        authority_mode="ask_every_time",
    )

    # This is what the model returned for the user's sentence.
    decision = read_decision(
        json.dumps(
            {
                "action": "plan",
                "goal": "organise Project Atlas mail",
                "nodes": [
                    {
                        "node": "label",
                        "connector": "mail.local",
                        "operation": "create_label",
                        "arguments": {"name": "Project Atlas"},
                        "verify": {
                            "operation": "list_labels",
                            "expect": {"contains": "Project Atlas"},
                        },
                    },
                    {
                        "node": "find",
                        "connector": "mail.local",
                        "operation": "search",
                        "arguments": {"from": "sam.okafor@northwind.example", "limit": 200},
                        "result_as": "matches",
                    },
                    {
                        "node": "apply",
                        "connector": "mail.local",
                        "operation": "apply_label",
                        "arguments": {
                            "ids": [f"atlas-{index}" for index in range(140)],
                            "label": "Project Atlas",
                        },
                        "depends_on": ["label", "find"],
                        "verify": {
                            "operation": "search",
                            "arguments": {"label": "Project Atlas"},
                            "expect": {"at_least": 140},
                        },
                    },
                ],
            }
        )
    )

    result = runners.run_plan(decision=decision, request="Organize my Project Atlas messages.")

    assert result["status"] == COMPLETED
    labelled = collect(registry, "mail.local", "search", {"label": "Project Atlas"})
    assert len(labelled) == 140
    # Model calls are not proportional to messages: the plan was one decision.
    assert model_calls["count"] == 0
    assert task.metrics.image_model_calls == 0

    # The destructive part still stops for a person.
    with pytest.raises(ApprovalRequired):
        registry.invoke(
            "mail.local",
            "delete",
            {"ids": [f"promo-{index}" for index in range(40)]},
        )
    assert mail.message_count == 180


# ----------------------------------------------------- LIVE ACCEPTANCE C


def test_c_a_model_authored_plan_schedules_around_a_conflict() -> None:
    task = TaskContext(goal="schedule")
    calendar = LocalCalendarConnector()
    calendar.seed(
        [
            {
                "title": "Design review",
                "start": "2026-08-20T14:00:00",
                "end": "2026-08-20T16:00:00",
            }
        ]
    )
    registry = ConnectorManager(
        policy=PolicyEngine(authority_mode="ask_every_time"), task=task
    )
    registry.register(calendar)
    runners = LiveRunners(connectors=registry, task=task, authority_mode="ask_every_time")

    decision = read_decision(
        json.dumps(
            {
                "action": "plan",
                "goal": "schedule the meeting on Thursday afternoon",
                "nodes": [
                    {
                        "node": "look",
                        "connector": "calendar.local",
                        "operation": "free_busy",
                        "arguments": {
                            "start": "2026-08-20T12:00:00",
                            "end": "2026-08-20T18:00:00",
                            "minimum_minutes": 60,
                        },
                        "result_as": "availability",
                    },
                    {
                        "node": "book",
                        "connector": "calendar.local",
                        "operation": "create_event",
                        "arguments": {
                            "title": "Project Atlas sync",
                            "start": "2026-08-20T16:00:00",
                            "end": "2026-08-20T17:00:00",
                        },
                        "depends_on": ["look"],
                        "verify": {
                            "operation": "list_events",
                            "arguments": {
                                "start": "2026-08-20T16:00:00",
                                "end": "2026-08-20T17:00:00",
                            },
                            "expect": {"contains": "Project Atlas sync"},
                        },
                    },
                ],
            }
        )
    )

    result = runners.run_plan(decision=decision, request="Schedule the meeting Thursday afternoon.")

    assert result["status"] == COMPLETED
    # Two events, not one booked over the other.
    assert calendar.event_count == 2
    booked = registry.invoke(
        "calendar.local",
        "list_events",
        {"start": "2026-08-20T00:00:00", "end": "2026-08-21T00:00:00"},
    ).data["items"]
    assert [item["start"] for item in booked] == [
        "2026-08-20T14:00:00",
        "2026-08-20T16:00:00",
    ]


# ----------------------------------------------------- LIVE ACCEPTANCE D


def test_d_base_steak_replans_once_and_the_revised_plan_succeeds() -> None:
    """A failure retry cannot fix, repaired by the same model, exactly once."""

    task = TaskContext(goal="label without creating first")
    registry = ConnectorManager(
        policy=PolicyEngine(authority_mode="full_access"), task=task
    )
    mail = LocalMailConnector()
    mail.seed([{"id": "m0", "from": "sam@x.test", "subject": "Atlas"}])
    registry.register(mail)

    planning_calls: list[str] = []

    def generate(messages):
        # The same inference entry point the rest of the turn uses.
        planning_calls.append(messages[-1]["content"])
        return json.dumps(
            {
                "goal": "label the mail",
                "nodes": [
                    {
                        "node": "make",
                        "connector": "mail.local",
                        "operation": "create_label",
                        "arguments": {"name": "Atlas"},
                    },
                    {
                        "node": "apply",
                        "connector": "mail.local",
                        "operation": "apply_label",
                        "arguments": {"ids": ["m0"], "label": "Atlas"},
                        "depends_on": ["make"],
                    },
                ],
            }
        )

    runners = LiveRunners(
        connectors=registry,
        generate=generate,
        task=task,
        authority_mode="full_access",
    )

    # The model's first plan forgets to create the label.
    decision = read_decision(
        json.dumps(
            {
                "action": "plan",
                "goal": "label the mail",
                "nodes": [
                    {
                        "node": "apply",
                        "connector": "mail.local",
                        "operation": "apply_label",
                        "arguments": {"ids": ["m0"], "label": "Atlas"},
                    }
                ],
            }
        )
    )

    result = runners.run_plan(decision=decision, request="Label that message Atlas.")

    assert result["status"] == COMPLETED
    assert result["replans"] == 1
    # Exactly one repair, not a loop.
    assert len(planning_calls) == 1
    assert task.metrics.planning_model_calls == 1
    # The packet was compact: a brief, not the whole history.
    assert len(planning_calls[0]) < 1_200
    assert "failed_node" in planning_calls[0]


def test_the_replan_packet_stays_small_and_says_what_matters() -> None:
    from app.backend.workflow import build_plan
    from app.backend.workflow.executor import NodeOutcome

    plan = build_plan(
        {
            "goal": "do the thing",
            "nodes": [
                {"node": "a", "connector": "mail.local", "operation": "list_labels"},
                {
                    "node": "b",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    plan.node("a").state = "completed"
    plan.observations["a"] = {
        "items": [{"id": "runtime-id-7", "token": "must-not-persist"}]
    }
    outcome = NodeOutcome("b", "failed", cause="not_found", detail={"error": "no label"})

    packet = replan_packet(plan, outcome, world={"default_browser": "msedge"})

    assert "do the thing" in packet
    assert '"failed_node": "b"' in packet
    assert "runtime-id-7" in packet
    assert "must-not-persist" not in packet
    assert "[redacted]" in packet
    assert "$ref" in packet
    assert '"completed": [\n    "a"\n  ]' in packet or '"completed": ["a"]' in packet
    assert len(packet) < 1_500


# ----------------------------------------------------- LIVE ACCEPTANCE E


SOURCES = {
    "https://a.example": {
        "summary": (
            "The autumn term begins on the first of September. "
            "Registration closes on the fifth of September."
        )
    },
    "https://b.example": {
        "summary": (
            "The autumn term begins on the first of September. "
            "Registration closes on the ninth of September."
        )
    },
    "https://c.example": {
        "summary": "The examination period begins in the middle of December."
    },
}


def test_e_research_reads_several_sources_without_a_call_per_page() -> None:
    task = TaskContext(goal="research")
    reasoning = {"count": 0}

    def generate(messages):
        reasoning["count"] += 1
        if "findings" in str(messages[-1].get("content")):
            return "The autumn term starts on the first of September; sources disagree on the registration closing date."
        return json.dumps({"query": None})

    runners = LiveRunners(
        generate=generate,
        task=task,
        search=lambda query: [{"url": url} for url in SOURCES],
        read=lambda url: SOURCES[url],
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "research",
                "question": "when does the autumn term start and registration close",
            }
        )
    )

    result = runners.run_research(
        decision=decision, request="When does term start and registration close?"
    )

    assert result["status"] == "completed"
    assert result["research"]["source_count"] == 3
    # Corroboration collapsed; the disagreement was kept.
    assert result["research"]["corroborated"] >= 1
    assert result["research"]["disputed"] == 2
    assert result["research"]["stop_reason"]
    # Provenance travels with the findings.
    assert all(claim["sources"] for claim in result["claims"])
    assert result["sources"][0]["url"] in SOURCES
    # Three pages: one call to decide whether to search again, and one to write
    # the answer. Neither scales with the number of pages read, which is the
    # property this guards — a per-page reasoning call is what makes research
    # unaffordable.
    assert reasoning["count"] <= 2


def test_research_declines_honestly_when_it_cannot_retrieve() -> None:
    runners = LiveRunners(task=TaskContext(goal="research"))
    result = runners.run_research(decision={"action": "research"}, request="anything")
    assert result["status"] == "declined"


# ----------------------------------------------------- LIVE ACCEPTANCE F


def test_f_research_findings_feed_an_image_node_in_one_plan() -> None:
    """Research then draw, decided by the model, run as a single workflow."""

    from app.backend.imaging import ImageOrchestrator

    task = TaskContext(goal="research then design")
    rendered: list[str] = []
    images = ImageOrchestrator(
        generate=lambda brief, job: rendered.append(brief.render())
        or {"artifact": "C:/artifacts/logo.png", "dimensions": "512x512"},
        task=task,
        backend="deterministic",
    )
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"), task=task)
    runners = LiveRunners(
        connectors=registry,
        images=images,
        task=task,
        capabilities=["image.generate"],
        authority_mode="full_access",
    )

    decision = read_decision(
        json.dumps(
            {
                "action": "plan",
                "goal": "pick a direction and draw one logo concept",
                "nodes": [
                    {
                        "node": "draw",
                        "capability": "image.generate",
                        "objective": "one logo concept in the strongest direction",
                        "arguments": {
                            "request": "premium minimal ecommerce logo for Boilabin",
                            "brief": {
                                "subject": "Boilabin ecommerce logo concept",
                                "image_type": "logo",
                                "brand": "Boilabin",
                                "style": "minimal premium flat vector",
                            },
                        },
                    }
                ],
            }
        )
    )

    result = runners.run_plan(
        decision=decision,
        request="Research three visual directions and generate one logo concept.",
    )

    assert result["status"] == COMPLETED
    assert task.metrics.image_model_calls == 1
    assert "Boilabin" in rendered[0]
    # Image generation is a plan node here, not a chat-only special case.
    assert result["plan"]["nodes"][0]["target"] == "image.generate"


def test_a_finished_action_says_what_it_actually_did() -> None:
    # "Done." is true and unverifiable. The live run answered exactly that,
    # because the model named a capability without a reason. The broker knows
    # what happened, so the sentence comes from the result.
    class _Broker:
        def invoke(self, request):
            return {"status": "succeeded", "target": "https://www.youtube.com"}

    runners = LiveRunners(
        broker=_Broker(),
        task=TaskContext(goal="open youtube"),
        capabilities=["application.launch"],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "action",
                "capability": "application.launch",
                "arguments": {"target": "youtube"},
            }
        )
    )

    result = runners.run_action(decision=decision, request="Open YouTube.")

    assert result["answer"] == "I opened https://www.youtube.com."


def test_the_model_s_own_reason_is_preferred_over_the_generated_one() -> None:
    class _Broker:
        def invoke(self, request):
            return {"status": "succeeded", "target": "https://www.youtube.com"}

    runners = LiveRunners(
        broker=_Broker(),
        task=TaskContext(goal="open youtube"),
        capabilities=["application.launch"],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "action",
                "reason": "Opening YouTube so you can watch it.",
                "capability": "application.launch",
                "arguments": {"target": "youtube"},
            }
        )
    )

    result = runners.run_action(decision=decision, request="Open YouTube.")

    assert result["answer"] == "Opening YouTube so you can watch it."


def test_a_capability_written_in_the_connector_slot_is_moved_not_refused() -> None:
    # Asked to open Notepad, focus it and capture the screen, the live model
    # produced exactly the right three steps and wrote each as
    # "connector": "application.launch". The plan was refused whole for naming
    # a service that is not connected — true, and useless.
    plan = validate_plan(
        {
            "goal": "open notepad and look at it",
            "nodes": [
                {
                    "node": "launch_notepad",
                    "connector": "application.launch",
                    "operation": "launch",
                    "arguments": {"target": "notepad"},
                },
                {
                    "node": "capture",
                    "connector": "screen.capture",
                    "arguments": {},
                    "depends_on": ["launch_notepad"],
                },
            ],
        },
        goal="open notepad and look at it",
        capabilities=["application.launch", "screen.capture"],
        connectors=[],
    )

    assert [node.capability for node in plan.nodes] == [
        "application.launch",
        "screen.capture",
    ]
    # The stale operation goes with it, or the node still reads as a call to a
    # service further down.
    assert all(node.connector is None and node.operation is None for node in plan.nodes)


def test_an_unknown_name_in_the_connector_slot_is_still_refused() -> None:
    # Moving names between slots must never become a way for an unrecognised
    # target to reach execution.
    with pytest.raises(PlanRejected, match="not connected"):
        validate_plan(
            {
                "goal": "x",
                "nodes": [
                    {
                        "node": "a",
                        "connector": "totally.invented",
                        "operation": "go",
                        "arguments": {},
                    }
                ],
            },
            goal="x",
            capabilities=["application.launch"],
            connectors=[],
        )


def test_a_node_with_a_file_path_is_resolved_from_its_argument_shape() -> None:
    plan = validate_plan(
        {
            "goal": "inspect a folder",
            "nodes": [
                {
                    "node": "inspect",
                    "connector": "invented.filesystem.service",
                    "operation": "search",
                    "arguments": {
                        "path": "C:\\Users\\person\\Documents",
                        "pattern": "*.txt",
                    },
                }
            ],
        },
        goal="inspect a folder",
        capabilities=["files.manage"],
        connectors=[],
    )

    node = plan.node("inspect")
    assert node.capability == "files.manage"
    assert node.arguments == {
        "operation": "search",
        "path": "C:\\Users\\person\\Documents",
        "pattern": "*.txt",
    }


def test_a_replan_cannot_reach_a_capability_that_was_never_granted() -> None:
    # The first plan is refused whole when it names an ungranted capability.
    # A revision was being built directly, so it skipped that check: the broker
    # would still refuse the call, but the plan would run part-way first.
    class _Broker:
        def invoke(self, request):
            raise RuntimeError("permanent failure")

    revisions = [
        json.dumps(
            {
                "goal": "x",
                "nodes": [
                    {"node": "sneak", "capability": "terminal.execute", "arguments": {}}
                ],
            }
        )
    ]

    runners = LiveRunners(
        broker=_Broker(),
        generate=lambda messages: revisions.pop(0) if revisions else "{}",
        task=TaskContext(goal="x"),
        capabilities=["screen.capture"],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "plan",
                "goal": "x",
                "nodes": [{"node": "shot", "capability": "screen.capture", "arguments": {}}],
            }
        )
    )

    result = runners.run_plan(decision=decision, request="x")

    # The replan was asked for and refused; nothing reached terminal.execute.
    assert result["replans"] == 1
    assert all(
        node["capability"] != "terminal.execute" for node in result["plan"]["nodes"]
    )


def test_computer_control_is_refused_outside_agent_mode() -> None:
    """The boundary is enforced by the runner, not by hiding the Agent button."""

    calls: list[dict] = []

    class _Broker:
        def invoke(self, request):
            calls.append(dict(request))
            return {"status": "succeeded"}

    # Agent mode off is expressed by handing the runners no capabilities.
    runners = LiveRunners(
        broker=_Broker(),
        task=TaskContext(goal="open notepad"),
        capabilities=[],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "action",
                "capability": "application.launch",
                "arguments": {"target": "notepad"},
            }
        )
    )

    result = runners.run_action(decision=decision, request="open notepad")

    # Nothing reached the broker, and the reply says which mode is needed.
    assert calls == []
    assert result["status"] == "declined"
    assert result.get("requires_agent_mode") is True
    assert "Agent" in result["answer"]


def test_a_node_naming_its_capability_only_in_its_id_still_resolves() -> None:
    # Live failure: a node conceptually meaning screen.capture named neither a
    # capability nor a connector, and the plan was refused whole.
    plan = validate_plan(
        {
            "goal": "look at the screen",
            "nodes": [{"node": "screen.capture", "arguments": {}}],
        },
        goal="look at the screen",
        capabilities=["screen.capture"],
        connectors=[],
    )

    assert [node.capability for node in plan.nodes] == ["screen.capture"]


def test_a_node_naming_nothing_recognisable_is_still_refused() -> None:
    with pytest.raises(PlanRejected):
        validate_plan(
            {"goal": "x", "nodes": [{"node": "do the thing", "arguments": {}}]},
            goal="x",
            capabilities=["screen.capture"],
            connectors=[],
        )


def test_an_invented_service_does_not_hide_the_capability_the_node_named() -> None:
    # Live failure: "Show me my screen" produced a node whose id was
    # screen.capture and whose connector was the invented service "display".
    # The plan was refused for the service rather than run as the capability it
    # plainly named.
    plan = validate_plan(
        {
            "goal": "show me my screen",
            "nodes": [
                {
                    "node": "screen.capture",
                    "connector": "display",
                    "operation": "capture",
                    "arguments": {},
                }
            ],
        },
        goal="show me my screen",
        capabilities=["screen.capture"],
        connectors=[],
    )

    assert [node.capability for node in plan.nodes] == ["screen.capture"]
    assert not any(node.connector for node in plan.nodes)


def test_a_configured_service_keeps_its_node_even_when_a_field_looks_granted() -> None:
    # A node that names a real connected service means that service. Slot
    # correction must not steal it just because another field happens to carry
    # a capability name.
    plan = validate_plan(
        {
            "goal": "x",
            "nodes": [
                {
                    "node": "screen.capture",
                    "connector": "files",
                    "operation": "list",
                    "arguments": {},
                }
            ],
        },
        goal="x",
        capabilities=["screen.capture"],
        connectors=["files"],
    )

    assert [node.connector for node in plan.nodes] == ["files"]
    assert not any(node.capability for node in plan.nodes)


def test_a_capability_slot_naming_a_verb_resolves_from_the_node_id() -> None:
    # Live failure: the model wrote "capability": "bring_to_front" and
    # "capability": "take_screenshot" — neither is a capability — while naming
    # the real ones in the node ids. The plan was refused for the verbs.
    plan = validate_plan(
        {
            "goal": "open notepad and capture the screen",
            "nodes": [
                {
                    "node": "open",
                    "capability": "application.launch",
                    "arguments": {"target": "notepad"},
                },
                {
                    "node": "window.control",
                    "capability": "bring_to_front",
                    "arguments": {"title": "Notepad"},
                },
                {
                    "node": "screen.capture",
                    "capability": "take_screenshot",
                    "arguments": {},
                },
            ],
        },
        goal="open notepad and capture the screen",
        capabilities=["application.launch", "window.control", "screen.capture"],
        connectors=[],
    )

    assert [node.capability for node in plan.nodes] == [
        "application.launch",
        "window.control",
        "screen.capture",
    ]


def test_an_ungranted_capability_is_still_refused_when_nothing_else_resolves() -> None:
    # Treating an unrecognised capability slot as empty must not become a way
    # to reach a capability the user never granted.
    with pytest.raises(PlanRejected, match="not enabled"):
        validate_plan(
            {
                "goal": "x",
                "nodes": [
                    {"node": "a", "capability": "terminal.execute", "arguments": {}}
                ],
            },
            goal="x",
            capabilities=["application.launch"],
            connectors=[],
        )


def test_a_finished_plan_reports_what_it_did_rather_than_done() -> None:
    # Live failure: "Open Notepad, bring it to the front, then capture the
    # screen and tell me what is on it" ran all three steps and answered with
    # the single word "Done."
    captured = {
        "status": "succeeded",
        "artifact": {"path": r"C:\artifacts\screen.bmp", "width": 1920, "height": 1080},
    }

    class _Broker:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def invoke(self, request):
            self.calls.append(dict(request))
            capability = request["capability"]
            if capability == "application.launch":
                return {"status": "succeeded", "target": "notepad"}
            if capability == "window.control":
                return {
                    "status": "succeeded",
                    "action": "focus",
                    "window": {"title": "Notepad"},
                }
            return dict(captured)

    runners = LiveRunners(
        broker=_Broker(),
        task=TaskContext(goal="open notepad"),
        capabilities=["application.launch", "window.control", "screen.capture"],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "plan",
                "goal": "open notepad and show the screen",
                "nodes": [
                    {
                        "node": "open",
                        "capability": "application.launch",
                        "arguments": {"target": "notepad"},
                    },
                    {
                        "node": "focus",
                        "capability": "window.control",
                        "arguments": {"action": "focus", "title": "Notepad"},
                    },
                    {"node": "shot", "capability": "screen.capture", "arguments": {}},
                ],
            }
        )
    )

    result = runners.run_plan(decision=decision, request="open notepad and show me")

    assert result["status"] == "completed"
    assert result["answer"] != "Done."
    assert "notepad" in result["answer"].lower()
    assert "screen.bmp" in result["answer"]

    # The capture reaches the turn above, which is what puts it in the
    # transcript instead of leaving the user with nothing to look at.
    nodes = result["plan"]["nodes"]
    artifacts = [node.get("observation", {}).get("artifact") for node in nodes]
    assert any(artifact and artifact.get("path") for artifact in artifacts)
    # And each step reports the time it actually took.
    assert all(
        isinstance(node.get("duration_ms"), float) for node in nodes
    )


def test_a_partly_finished_plan_says_what_ran_and_what_stopped() -> None:
    # Live failure: three steps ran, the fourth named a UI Automation command
    # that does not exist, and the whole assistant reply became
    # "analyze: CapabilityCallFailed: UI Automation command must be one of:
    # collapse, expand, find_control, ..." — an exception class and a schema
    # dump, shown to a person.
    class _Broker:
        def invoke(self, request):
            if request["capability"] == "application.launch":
                return {"status": "succeeded", "target": "notepad"}
            raise ValueError(
                "UI Automation command must be one of: collapse, expand, "
                "find_control, get_text, invoke"
            )

    runners = LiveRunners(
        broker=_Broker(),
        task=TaskContext(goal="open notepad and read it"),
        capabilities=["application.launch", "ui.automation"],
        authority_mode="full_access",
    )
    decision = read_decision(
        json.dumps(
            {
                "action": "plan",
                "goal": "open notepad and read it",
                "nodes": [
                    {
                        "node": "open",
                        "capability": "application.launch",
                        "arguments": {"target": "notepad"},
                    },
                    {
                        "node": "analyze",
                        "capability": "ui.automation",
                        "arguments": {"command": "analyze"},
                        "depends_on": ["open"],
                    },
                ],
            }
        )
    )

    result = runners.run_plan(decision=decision, request="open notepad and read it")

    assert result["status"] != "completed"
    answer = result["answer"]
    # What did happen is still reported.
    assert "notepad" in answer.lower()
    # And the failure is a sentence, not a traceback.
    assert "CapabilityCallFailed" not in answer
    assert "ValueError" not in answer
    assert "could not finish" in answer


def test_research_answers_the_question_instead_of_narrating_the_search() -> None:
    # Live failure: "Search the web for the tallest building in the world and
    # summarise what you find" returned "I read 6 sources and kept 145 distinct
    # findings. ... I stopped because: source budget." The user was told how
    # the search went and never told the answer.
    seen: list[dict] = []

    def generate(messages):
        seen.append(messages[-1])
        if "findings" in str(messages[-1].get("content")):
            return "The Burj Khalifa in Dubai is the tallest building, at 828 metres."
        return json.dumps({"query": None})

    runners = LiveRunners(
        generate=generate,
        task=TaskContext(goal="research"),
        search=lambda query: [{"url": url} for url in SOURCES],
        read=lambda url: SOURCES[url],
    )
    decision = read_decision(
        json.dumps({"action": "research", "question": "tallest building"})
    )

    result = runners.run_research(decision=decision, request="tallest building?")

    assert result["answer"].startswith("The Burj Khalifa")
    assert "distinct findings" not in result["answer"]
    assert "stopped because" not in result["answer"]
    # The statistics are kept — beside the answer, not instead of it.
    assert "distinct findings" in result["research"]["process_summary"]
    assert result["research"]["source_count"] == 3


def test_a_synthesis_that_narrates_the_search_is_not_used() -> None:
    # The model asked to synthesise sometimes reports on the process instead,
    # which is the exact failure this boundary exists to stop.
    def generate(messages):
        if "findings" in str(messages[-1].get("content")):
            return "I read 3 sources and kept 5 distinct findings."
        return json.dumps({"query": None})

    runners = LiveRunners(
        generate=generate,
        task=TaskContext(goal="research"),
        search=lambda query: [{"url": url} for url in SOURCES],
        read=lambda url: SOURCES[url],
    )
    decision = read_decision(
        json.dumps({"action": "research", "question": "when does term start"})
    )

    result = runners.run_research(decision=decision, request="when does term start?")

    assert "distinct findings" not in result["answer"]
    # Failed synthesis must not promote a list of retrieved snippets to an answer.
    assert "could not produce a supported answer" in result["answer"]
    assert result["status"] == "failed"
