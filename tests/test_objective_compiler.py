from __future__ import annotations

import json

from app.backend.chat.dispatch import GOAL_SPEC_INSTRUCTION, PLAN_SHAPE
from app.backend.chat.goal_state import GoalSpec
from app.backend.chat.service import ChatService


def test_compiled_objective_keeps_operational_state_beside_predicates() -> None:
    spec = GoalSpec.from_compilation(
        {
            "objective": "Open the selected page for the user",
            "current_turn_intent": "operate the browser",
            "constraints": ["do not submit anything"],
            "protected_resources": ["the current draft"],
            "permission_scope": "agent:ask_every_time",
            "required_outcomes": [
                {"kind": "active_url", "subject": "https://example.test/result"},
                {"kind": "browser_visible", "subject": "true"},
            ],
            "unknowns": ["which result will match"],
            "future_dependencies": ["a search observation"],
            "stopping_conditions": ["the selected page is visible"],
        }
    )

    assert spec.goal == "Open the selected page for the user"
    assert spec.current_turn_intent == "operate the browser"
    assert spec.permission_scope == "agent:ask_every_time"
    assert spec.constraints == ("do not submit anything",)
    assert spec.protected_resources == ("the current draft",)
    assert [item.kind for item in spec.required] == ["active_url", "browser_visible"]
    assert spec.unknowns == ("which result will match",)
    assert spec.future_dependencies == ("a search observation",)
    assert spec.stopping_conditions == ("the selected page is visible",)


def test_objective_compiler_is_not_a_filesystem_only_prompt() -> None:
    assert "current_turn_intent" in GOAL_SPEC_INSTRUCTION
    assert "permission_scope" in GOAL_SPEC_INSTRUCTION
    assert "future_dependencies" in GOAL_SPEC_INSTRUCTION
    assert "window_present" in GOAL_SPEC_INSTRUCTION
    assert "active_url" in GOAL_SPEC_INSTRUCTION
    assert "browser_visible" in GOAL_SPEC_INSTRUCTION
    assert "media_playing" in GOAL_SPEC_INSTRUCTION
    assert "artifact_valid" in GOAL_SPEC_INSTRUCTION
    assert "Do not guess runtime" in GOAL_SPEC_INSTRUCTION
    assert "explicitly native or installed application" in GOAL_SPEC_INSTRUCTION


def test_routing_keeps_semantic_branching_in_the_observe_act_loop() -> None:
    assert "fresh result needs model judgment" in PLAN_SHAPE
    assert "Agent loop continues" in PLAN_SHAPE
    assert "$ref" in PLAN_SHAPE


class _ProgressContext:
    def __init__(self):
        self.updates = []

    def update(self, **values):
        self.updates.append(values)


class _CompilerModel:
    def __init__(self, *replies: object) -> None:
        self.replies = iter(replies)
        self.calls: list[list[dict[str, str]]] = []
        self.settings: list[dict[str, object]] = []

    def _agent_generate(self, messages, **kwargs):
        self.calls.append(list(messages))
        self.settings.append(dict(kwargs["generation_settings"]))
        if kwargs.get("on_preview"):
            kwargs["on_preview"]({"token_count": 12})
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _compile(model: _CompilerModel):
    return ChatService._goal_spec_for(
        model,
        request=(
            r"Remove every .tmp below C:\work\fixture, including nested folders, "
            r"but keep C:\work\fixture\notes.txt."
        ),
        permission_scope="computer:full_access",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )


def test_objective_compiler_repairs_invalid_model_json_once() -> None:
    model = _CompilerModel(
        r'{"objective":"clean","required_outcomes":[{"kind":"absent","subject":"C:\work\fixture\**\*.tmp"}]}',
        json.dumps(
            {
                "objective": "clean",
                "protected_resources": [r"C:\work\fixture\notes.txt"],
                "required_outcomes": [
                    {
                        "kind": "absent",
                        "subject": r"C:\work\fixture\**\*.tmp",
                    }
                ],
            }
        ),
    )

    spec, evidence = _compile(model)

    assert spec is not None
    assert [item.kind for item in spec.required] == ["absent", "present"]
    assert evidence["status"] == "compiled"
    assert evidence["attempt_count"] == 2
    assert evidence["attempts"][0]["status"] == "invalid_json"
    assert "could not be used" in model.calls[1][-1]["content"]
    assert [item["maximum_output_tokens"] for item in model.settings] == [1024, 1024]


def test_objective_compiler_repairs_an_empty_predicate_set_once() -> None:
    model = _CompilerModel(
        json.dumps({"objective": "clean", "required_outcomes": []}),
        json.dumps(
            {
                "objective": "clean",
                "required_outcomes": [
                    {"kind": "absent", "subject": r"C:\work\fixture\**\*.tmp"}
                ],
            }
        ),
    )

    spec, evidence = _compile(model)

    assert spec is not None
    assert len(spec.required) == 1
    assert evidence["attempts"][0]["status"] == "no_required_predicates"


def test_objective_compiler_fails_closed_after_one_bounded_repair() -> None:
    model = _CompilerModel("not json", "still not json")

    spec, evidence = _compile(model)

    assert spec is None
    assert evidence["status"] == "failed"
    assert evidence["attempt_count"] == 2
    assert len(model.calls) == 2


def test_native_application_goal_repairs_browser_only_observers() -> None:
    model = _CompilerModel(
        json.dumps(
            {
                "objective": "show the native application",
                "required_outcomes": [
                    {"kind": "active_url", "subject": "$selected_url"},
                    {"kind": "browser_visible", "subject": "true"},
                ],
            }
        ),
        json.dumps(
            {
                "objective": "show the native application",
                "required_outcomes": [
                    {"kind": "window_present", "subject": "Example App"},
                    {"kind": "window_focused", "subject": "Example App"},
                ],
            }
        ),
    )

    spec, evidence = ChatService._goal_spec_for(
        model,
        request="Open the native Example App window and verify it is visible.",
        permission_scope="computer:full_access",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )

    assert spec is not None
    assert [predicate.kind for predicate in spec.required] == ["window_present"]
    assert evidence["attempt_count"] == 2
    assert evidence["attempts"][0]["status"] == "incompatible_observers"
    assert "browser-only outcome kinds" in model.calls[1][-1]["content"]


def test_visible_window_goal_does_not_invent_a_final_focus_requirement() -> None:
    model = _CompilerModel(
        json.dumps(
            {
                "objective": "show the native application",
                "required_outcomes": [
                    {"kind": "window_present", "subject": "Example App"},
                    {"kind": "window_focused", "subject": "Example App"},
                ],
            }
        )
    )

    spec, evidence = ChatService._goal_spec_for(
        model,
        request="Open the native Example App and verify its window is visible.",
        permission_scope="computer:full_access",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )

    assert spec is not None
    assert [predicate.kind for predicate in spec.required] == ["window_present"]
    assert evidence["attempt_count"] == 1


def test_explicit_focus_goal_keeps_window_focused_predicate() -> None:
    model = _CompilerModel(
        json.dumps(
            {
                "objective": "focus the native application",
                "required_outcomes": [
                    {"kind": "window_present", "subject": "Example App"},
                    {"kind": "window_focused", "subject": "Example App"},
                ],
            }
        )
    )

    spec, _evidence = ChatService._goal_spec_for(
        model,
        request="Open the native Example App and keep its window focused.",
        permission_scope="computer:full_access",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )

    assert spec is not None
    assert [predicate.kind for predicate in spec.required] == [
        "window_present",
        "window_focused",
    ]


def test_research_compiler_drops_invented_paths_and_placeholder_urls() -> None:
    model = _CompilerModel(
        json.dumps(
            {
                "objective": "research Python 3.14",
                "required_outcomes": [
                    {
                        "kind": "active_url",
                        "subject": "$official_python_status_page",
                    },
                    {
                        "kind": "present",
                        "subject": "/usr/local/bin/python3.14",
                    },
                ],
            }
        )
    )

    spec, evidence = ChatService._goal_spec_for(
        model,
        request="Research the current Python 3.14 release status.",
        permission_scope="research:enabled",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )

    assert spec is None
    assert evidence["status"] == "research_evidence_verifier"
    assert len(model.calls) == 1


def test_research_compiler_keeps_an_exact_user_supplied_url() -> None:
    model = _CompilerModel(
        json.dumps(
            {
                "objective": "verify link",
                "required_outcomes": [
                    {
                        "kind": "active_url",
                        "subject": "https://example.test/report",
                    }
                ],
            }
        )
    )

    spec, evidence = ChatService._goal_spec_for(
        model,
        request="Verify https://example.test/report and cite it.",
        permission_scope="research:enabled",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )

    assert evidence["status"] == "compiled"
    assert spec is not None
    assert spec.required[0].kind == "exact_page"
    assert spec.required[0].subject == "https://example.test/report"


def test_research_compiler_drops_status_misclassified_as_product_stock() -> None:
    model = _CompilerModel(
        json.dumps(
            {
                "objective": "research Python 3.14 status",
                "required_outcomes": [
                    {
                        "kind": "stock_confirmed",
                        "subject": "Python 3.14",
                        "detail": "Confirm status with an independent source.",
                    }
                ],
            }
        )
    )

    spec, evidence = ChatService._goal_spec_for(
        model,
        request="Research the current Python 3.14 release status.",
        permission_scope="research:enabled",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )

    assert spec is None
    assert evidence["status"] == "research_evidence_verifier"


def test_research_compiler_keeps_explicit_product_stock_requirement() -> None:
    model = _CompilerModel(
        json.dumps(
            {
                "objective": "verify a named product is in stock",
                "required_outcomes": [
                    {
                        "kind": "stock_confirmed",
                        "subject": "Lexar NM790 2TB",
                    }
                ],
            }
        )
    )

    spec, evidence = ChatService._goal_spec_for(
        model,
        request="Find the cheapest Lexar NM790 2TB currently in stock.",
        permission_scope="research:enabled",
        context=_ProgressContext(),
        generation_settings={"reasoning_mode": "instant"},
    )

    assert evidence["status"] == "compiled"
    assert spec is not None
    assert spec.required[0].kind == "stock_confirmed"


def test_goal_compilation_reports_its_actual_phase_and_token_progress():
    context = _ProgressContext()
    model = _CompilerModel(json.dumps({"objective":"inspect", "required_outcomes":[{"kind":"present","subject":"C:/fixture.txt"}]}))
    ChatService._goal_spec_for(model, request="Inspect C:/fixture.txt", permission_scope="computer:full_access", context=context, generation_settings={"reasoning_mode":"instant"})
    assert context.updates[0]["phase"] == "Checking the requested result"
    assert context.updates[-1]["details"]["goal_compilation"]["output_tokens"] == 12
