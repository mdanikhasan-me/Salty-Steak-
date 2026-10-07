from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.automation.policy import PolicyEngine
from app.backend.chat.task_runtime import COMPLETED, FAILED, STOPPED, WAITING, TaskContext
from app.backend.connectors import ConnectorManager, LocalCalendarConnector, LocalMailConnector
from app.backend.workflow import (
    Executor,
    Plan,
    PlanError,
    PlanNode,
    WorkflowEngine,
    build_plan,
    load_checkpoint,
    resume,
)
from app.backend.workflow.executor import (
    CAUSE_NOT_FOUND,
    CAUSE_STALE,
    CAUSE_TRANSIENT,
    classify_failure,
)
from app.backend.connectors.contract import ConnectorError


@pytest.fixture()
def registry() -> ConnectorManager:
    manager = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    mail = LocalMailConnector()
    mail.seed(
        [
            {"id": f"m{i}", "from": "sam@northwind.example", "subject": f"Atlas {i}"}
            for i in range(10
            )
        ]
    )
    manager.register(mail)
    manager.register(LocalCalendarConnector())
    return manager


def _engine(registry, task=None, **kwargs):
    task = task or TaskContext(goal="test")
    executor = Executor(
        connectors=registry, task=task, policy=registry.policy, authority_mode="full_access"
    )
    return WorkflowEngine(executor=executor, task=task, **kwargs), task


# --------------------------------------------------------------- plan shape


def test_a_plan_that_contradicts_itself_fails_before_anything_runs() -> None:
    with pytest.raises(PlanError, match="circle"):
        Plan(
            goal="loop",
            nodes=[
                PlanNode(node_id="a", objective="", capability="screen.capture", depends_on=("b",)),
                PlanNode(node_id="b", objective="", capability="screen.capture", depends_on=("a",)),
            ],
        )

    with pytest.raises(PlanError, match="not in the plan"):
        Plan(
            goal="dangling",
            nodes=[PlanNode(node_id="a", objective="", capability="screen.capture", depends_on=("ghost",))],
        )

    with pytest.raises(PlanError, match="either a capability or a connector"):
        PlanNode(node_id="a", objective="", capability="x", connector="y", operation="z")


def test_dependencies_decide_the_order_not_the_listing() -> None:
    plan = build_plan(
        {
            "goal": "ordered",
            "nodes": [
                {"node": "second", "connector": "mail.local", "operation": "list_labels", "depends_on": ["first"]},
                {"node": "first", "connector": "mail.local", "operation": "list_labels"},
            ],
        }
    )
    assert [node.node_id for node in plan.ready()] == ["first"]


def test_one_nodes_result_reaches_the_next_without_the_model_seeing_it() -> None:
    plan = Plan(goal="chain", nodes=[])
    plan.variables["found"] = ["m1", "m2"]

    resolved = plan.resolve({"ids": {"$from": "found"}, "label": "Atlas"})

    assert resolved == {"ids": ["m1", "m2"], "label": "Atlas"}
    with pytest.raises(PlanError, match="Nothing has produced"):
        plan.resolve({"ids": {"$from": "missing"}})


def test_a_runtime_reference_keeps_the_observed_value_and_type() -> None:
    plan = Plan(goal="chain", nodes=[])
    plan.observations["find"] = {
        "items": [
            {"id": "m1", "score": 0.7},
            {"id": "m2", "score": 0.9},
        ],
        "count": 2,
    }

    resolved = plan.resolve(
        {
            "id": {"$ref": "find.output.items[1].id"},
            "ids": {"$ref": "find.output.items[*].id"},
            "ids_plus_alias": {"$ref": "find.output.items[+].id"},
            "count": {"$ref": "find.output.count"},
        }
    )

    assert resolved == {
        "id": "m2",
        "ids": ["m1", "m2"],
        "ids_plus_alias": ["m1", "m2"],
        "count": 2,
    }
    assert isinstance(resolved["count"], int)
    with pytest.raises(PlanError, match="could not resolve"):
        plan.resolve({"id": {"$ref": "find.output.items[9].id"}})


def test_string_runtime_references_are_normalised_and_dependencies_inferred() -> None:
    plan = build_plan(
        {
            "nodes": [
                {
                    "node": "find",
                    "connector": "mail.local",
                    "operation": "search",
                },
                {
                    "node": "tag",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {
                        "ids": "$find.output.items[*].id",
                        "label": "$label.output.label",
                    },
                },
                {
                    "node": "label",
                    "connector": "mail.local",
                    "operation": "create_label",
                },
            ]
        }
    )

    assert plan.node("tag").arguments == {
        "ids": {"$ref": "find.output.items[*].id"},
        "label": {"$ref": "label.output.label"},
    }
    assert plan.node("tag").depends_on == ("find", "label")


def test_a_later_node_consumes_the_actual_output_of_an_observation(registry) -> None:
    engine, _task = _engine(registry)
    plan = build_plan(
        {
            "goal": "read the first matching message",
            "nodes": [
                {
                    "node": "find",
                    "connector": "mail.local",
                    "operation": "search",
                    "arguments": {"from": "sam@northwind.example"},
                },
                {
                    "node": "read",
                    "connector": "mail.local",
                    "operation": "get",
                    "arguments": {"id": {"$ref": "find.output.items[0].id"}},
                    "depends_on": ["find"],
                },
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == COMPLETED
    assert plan.node("read").result["message"]["id"] == plan.node("find").result[
        "items"
    ][0]["id"]


# ------------------------------------------------------------------ running


def test_a_plan_runs_to_completion_with_no_model_calls(registry) -> None:
    engine, task = _engine(registry)
    plan = build_plan(
        {
            "goal": "label the atlas mail",
            "nodes": [
                {
                    "node": "label",
                    "connector": "mail.local",
                    "operation": "create_label",
                    "arguments": {"name": "Atlas"},
                    "verify": {"operation": "list_labels", "expect": {"contains": "Atlas"}},
                },
                {
                    "node": "find",
                    "connector": "mail.local",
                    "operation": "search",
                    "arguments": {"from": "sam@northwind.example"},
                    "result_as": "matches",
                },
                {
                    "node": "apply",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {"ids": ["m0", "m1"], "label": "Atlas"},
                    "depends_on": ["label", "find"],
                    "verify": {
                        "operation": "search",
                        "arguments": {"label": "Atlas"},
                        "expect": {"at_least": 2},
                    },
                },
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == COMPLETED
    assert task.state == COMPLETED
    # Every node ran and every verifiable node was confirmed against the service.
    assert all(node.state == "completed" for node in plan.nodes)
    assert plan.node("apply").verified is True
    assert plan.node("label").verified is True
    # The decisive number: the whole workflow used no model calls at all.
    assert task.metrics.model_calls == 0


def test_verification_failure_is_not_reported_as_success(registry) -> None:
    engine, task = _engine(registry)
    plan = build_plan(
        {
            "goal": "claim something untrue",
            "nodes": [
                {
                    "node": "label",
                    "connector": "mail.local",
                    "operation": "create_label",
                    "arguments": {"name": "Atlas"},
                    # Asks for a label that was never created.
                    "verify": {"operation": "list_labels", "expect": {"contains": "Ghost"}},
                }
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == FAILED
    assert "could not be confirmed" in result.message
    assert plan.node("label").verified is False


def test_a_failed_node_skips_what_depended_on_it(registry) -> None:
    engine, task = _engine(registry)
    plan = build_plan(
        {
            "goal": "broken chain",
            "nodes": [
                {
                    "node": "bad",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {"ids": ["m0"], "label": "Nonexistent"},
                },
                {
                    "node": "after",
                    "connector": "mail.local",
                    "operation": "list_labels",
                    "depends_on": ["bad"],
                },
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == FAILED
    assert plan.node("after").state == "skipped"
    assert "not attempted" in result.message


# ------------------------------------------------------------------ recovery


def test_a_transient_failure_is_retried_without_asking_anybody(registry) -> None:
    engine, task = _engine(registry)
    attempts = {"count": 0}
    original = registry.invoke

    def flaky(connector_id, operation, arguments=None, **kwargs):
        if operation == "list_labels":
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ConnectorError("network hiccup", kind="timeout")
        return original(connector_id, operation, arguments, **kwargs)

    registry.invoke = flaky
    plan = build_plan(
        {
            "goal": "retry",
            "nodes": [{"node": "read", "connector": "mail.local", "operation": "list_labels", "max_attempts": 3}],
        }
    )

    result = engine.run(plan)

    assert result.state == COMPLETED
    assert attempts["count"] == 2
    assert task.metrics.retries == 1
    # No model call was needed to recover.
    assert task.metrics.model_calls == 0


def test_causes_are_classified_so_the_right_recovery_is_chosen() -> None:
    assert classify_failure(ConnectorError("x", kind="timeout")) == CAUSE_TRANSIENT
    assert classify_failure(ConnectorError("x", kind="stale_element")) == CAUSE_STALE
    assert classify_failure(ConnectorError("x", kind="not_found")) == CAUSE_NOT_FOUND
    assert classify_failure(ValueError("x")) == "unknown"


def test_a_repeated_plan_is_recognised_rather_than_run_again(registry) -> None:
    calls = {"count": 0}

    def always_the_same(plan, outcome):
        calls["count"] += 1
        return {
            "goal": plan.goal,
            "nodes": [
                {
                    "node": "bad",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {"ids": ["m0"], "label": "Nonexistent"},
                }
            ],
        }

    engine, task = _engine(registry, replan=always_the_same)
    plan = build_plan(
        {
            "goal": "loop",
            "nodes": [
                {
                    "node": "bad",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {"ids": ["m0"], "label": "Nonexistent"},
                }
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == FAILED
    # The model was asked once; the identical plan it returned was recognised
    # rather than run and asked about again.
    assert calls["count"] == 1
    assert task.metrics.stagnation_breaks == 1


def test_replanning_keeps_work_that_already_succeeded(registry) -> None:
    def repair(plan, outcome):
        return {
            "goal": plan.goal,
            "nodes": [
                {"node": "make", "connector": "mail.local", "operation": "create_label", "arguments": {"name": "Atlas"}},
                {
                    "node": "apply",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {"ids": ["m0"], "label": "Atlas"},
                    "depends_on": ["make"],
                },
            ],
        }

    engine, task = _engine(registry, replan=repair)
    plan = build_plan(
        {
            "goal": "label without creating first",
            "nodes": [
                {"node": "make", "connector": "mail.local", "operation": "list_labels"},
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

    result = engine.run(plan)

    assert result.state == COMPLETED
    assert task.metrics.replans == 1
    # The plan that replaced it is a new version, not a restart.
    assert result.plan.version == 2


# -------------------------------------------------------------------- stop


def test_stopping_prevents_the_next_node_from_starting(registry) -> None:
    engine, task = _engine(registry)
    ran: list[str] = []
    original = registry.invoke

    def watched(connector_id, operation, arguments=None, **kwargs):
        ran.append(operation)
        if operation == "create_label":
            task.request_stop("user_requested")
        return original(connector_id, operation, arguments, **kwargs)

    registry.invoke = watched
    plan = build_plan(
        {
            "goal": "stop midway",
            "nodes": [
                {"node": "one", "connector": "mail.local", "operation": "create_label", "arguments": {"name": "Atlas"}},
                {
                    "node": "two",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {"ids": ["m0"], "label": "Atlas"},
                    "depends_on": ["one"],
                },
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == STOPPED
    assert task.state == STOPPED
    # The second node never began: no mutation happened after the stop.
    assert "apply_label" not in ran
    assert plan.node("two").state == "pending"
    assert task.metrics.cancellation_latency_seconds is not None


# ------------------------------------------------------------- checkpointing


def test_a_workflow_can_be_resumed_without_repeating_side_effects(
    registry, tmp_path: Path
) -> None:
    checkpoint = tmp_path / "workflow.json"
    engine, task = _engine(registry, checkpoint_path=checkpoint)

    plan = build_plan(
        {
            "goal": "interrupted",
            "nodes": [
                {
                    "node": "make",
                    "connector": "mail.local",
                    "operation": "create_label",
                    "arguments": {"name": "Atlas"},
                    "verify": {"operation": "list_labels", "expect": {"contains": "Atlas"}},
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
    # Stop after the first node.
    original = registry.invoke

    def stop_after_label(connector_id, operation, arguments=None, **kwargs):
        result = original(connector_id, operation, arguments, **kwargs)
        if operation == "create_label":
            task.request_stop("user_requested")
        return result

    registry.invoke = stop_after_label
    first = engine.run(plan)
    assert first.state == STOPPED
    assert checkpoint.is_file()

    registry.invoke = original
    saved = load_checkpoint(checkpoint)
    assert saved["nodes"][0]["state"] == "completed"

    # A fresh task resumes where the old one stopped.
    resumed_engine, resumed_task = _engine(registry, checkpoint_path=checkpoint)
    calls: list[str] = []
    registry.invoke = lambda c, o, a=None, **k: (calls.append(o), original(c, o, a, **k))[1]

    result = resume(saved, engine=resumed_engine)

    assert result.state == COMPLETED
    # The label was not created twice: the completed node was re-verified and
    # then trusted, not replayed.
    assert calls.count("create_label") == 0
    assert "apply_label" in calls


def test_runtime_observations_survive_checkpoint_and_resume(
    registry, tmp_path: Path
) -> None:
    checkpoint = tmp_path / "runtime-dataflow.json"
    engine, task = _engine(registry, checkpoint_path=checkpoint)
    plan = build_plan(
        {
            "goal": "find, then read",
            "nodes": [
                {
                    "node": "find",
                    "connector": "mail.local",
                    "operation": "search",
                    "arguments": {"from": "sam@northwind.example"},
                },
                {
                    "node": "read",
                    "connector": "mail.local",
                    "operation": "get",
                    "arguments": {"id": {"$ref": "find.output.items[0].id"}},
                    "depends_on": ["find"],
                },
            ],
        }
    )
    original = registry.invoke

    def stop_after_observation(connector_id, operation, arguments=None, **kwargs):
        result = original(connector_id, operation, arguments, **kwargs)
        if operation == "search":
            task.request_stop("simulate_restart")
        return result

    registry.invoke = stop_after_observation
    first = engine.run(plan)
    saved = load_checkpoint(checkpoint)

    assert first.state == STOPPED
    assert saved["observations"]["find"]["items"]

    registry.invoke = original
    resumed_engine, _resumed_task = _engine(registry, checkpoint_path=checkpoint)
    calls: list[str] = []
    registry.invoke = lambda c, o, a=None, **k: (calls.append(o), original(c, o, a, **k))[1]

    result = resume(saved, engine=resumed_engine, reverify=False)

    assert result.state == COMPLETED
    assert "search" not in calls
    assert calls == ["get"]


def test_checkpoint_preserves_retry_policy_without_persisting_secrets(
    registry, tmp_path: Path
) -> None:
    checkpoint = tmp_path / "safe-workflow.json"
    engine, task = _engine(registry, checkpoint_path=checkpoint)
    plan = build_plan(
        {
            "goal": "durable policy",
            "variables": {"token": "variable-secret", "safe": "kept"},
            "nodes": [
                {
                    "node": "read",
                    "connector": "mail.local",
                    "operation": "list_labels",
                    "arguments": {"password": "argument-secret"},
                    "verify": {
                        "operation": "list_labels",
                        "arguments": {"api_key": "verification-secret"},
                    },
                    "max_attempts": 5,
                    "fallback": {
                        "arguments": {"access_token": "fallback-secret"}
                    },
                }
            ],
        }
    )

    # Stop before execution so the pending node's complete recovery policy is
    # what crosses the durable boundary.
    task.request_stop("simulate_restart")
    engine.run(plan)
    saved = load_checkpoint(checkpoint)

    assert saved["variables"] == {"token": "[redacted]", "safe": "kept"}
    node = saved["nodes"][0]
    assert node["arguments"]["password"] == "[redacted]"
    assert node["verify"]["arguments"]["api_key"] == "[redacted]"
    assert node["fallback"]["arguments"]["access_token"] == "[redacted]"
    assert node["max_attempts"] == 5

    resumed_engine, resumed_task = _engine(registry)
    resumed_task.request_stop("inspect_restored_plan")
    resumed = resume(saved, engine=resumed_engine, reverify=False)
    assert resumed.plan.node("read").max_attempts == 5
    assert resumed.plan.node("read").fallback == node["fallback"]


def test_recovery_redoes_work_whose_effect_has_disappeared(
    registry, tmp_path: Path
) -> None:
    checkpoint = tmp_path / "workflow.json"
    engine, task = _engine(registry, checkpoint_path=checkpoint)
    plan = build_plan(
        {
            "goal": "vanishing",
            "nodes": [
                {
                    "node": "make",
                    "connector": "mail.local",
                    "operation": "create_label",
                    "arguments": {"name": "Atlas"},
                    "verify": {"operation": "list_labels", "expect": {"contains": "Atlas"}},
                }
            ],
        }
    )
    engine.run(plan)
    saved = load_checkpoint(checkpoint)

    # The label is gone by the time the workflow resumes: a record saying the
    # node completed is not proof the effect survived.
    mail = registry.get("mail.local")
    mail._labels.clear()

    resumed_engine, resumed_task = _engine(registry)
    result = resume(saved, engine=resumed_engine)

    assert result.state == COMPLETED
    assert "Atlas" in mail._labels
    assert "recovery_redo" in [event.kind for event in resumed_task.events]


def test_an_interrupted_checkpoint_write_cannot_be_read_as_truth(
    registry, tmp_path: Path
) -> None:
    checkpoint = tmp_path / "workflow.json"
    engine, task = _engine(registry, checkpoint_path=checkpoint)
    engine.run(
        build_plan(
            {
                "goal": "atomic",
                "nodes": [{"node": "read", "connector": "mail.local", "operation": "list_labels"}],
            }
        )
    )

    # Written whole then moved into place; no partial file is left behind.
    assert checkpoint.is_file()
    assert not checkpoint.with_suffix(".partial").exists()
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["nodes"]
