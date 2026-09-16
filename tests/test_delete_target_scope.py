import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backend.automation.invocation import invoke_capability, CapabilityCallFailed
from app.backend.chat.task_runtime import TaskContext


class Broker:
    def __init__(self): self.calls = []
    def invoke(self, request):
        self.calls.append(request)
        return {"status": "succeeded"}


def task_for(*paths):
    task = TaskContext(goal="Delete selected files")
    task.deletion_targets = tuple(str(path) for path in paths)
    return task


def invoke(broker, task, **arguments):
    return invoke_capability(broker, "files.manage", {"operation": "delete", **arguments}, authority_mode="full_access", task=task)


def test_requested_file_cannot_be_replaced_by_unrelated_file(tmp_path):
    wanted, unrelated = tmp_path / "wanted.tmp", tmp_path / "unrelated.tmp"
    wanted.touch(); unrelated.touch()
    broker = Broker()
    with pytest.raises(CapabilityCallFailed, match="does not match"):
        invoke(broker, task_for(wanted), path=str(unrelated))
    assert broker.calls == []
    invoke(broker, task_for(wanted), path=str(wanted))
    assert len(broker.calls) == 1
    assert wanted.exists() and unrelated.exists()


def test_goal_pattern_freezes_only_matching_observed_paths(tmp_path):
    (tmp_path / "wanted.tmp").touch(); (tmp_path / "notes.txt").touch()
    broker = Broker()
    task = task_for(tmp_path / "*.tmp")
    invoke(broker, task, path=str(tmp_path), pattern="*.tmp")
    assert broker.calls[0]["arguments"]["paths"] == [str(tmp_path / "wanted.tmp")]
    assert "pattern" not in broker.calls[0]["arguments"]
    with pytest.raises(CapabilityCallFailed):
        invoke(broker, task, path=str(tmp_path), pattern="*")
    assert len(broker.calls) == 1


def test_directory_scope_is_not_a_string_prefix(tmp_path):
    allowed = tmp_path / "safe"; allowed.mkdir()
    sibling = tmp_path / "safe-elsewhere"; sibling.mkdir()
    inside = allowed / "one.tmp"; inside.touch()
    outside = sibling / "two.tmp"; outside.touch()
    broker = Broker()
    invoke(broker, task_for(allowed), path=str(inside))
    with pytest.raises(CapabilityCallFailed): invoke(broker, task_for(allowed), path=str(outside))
    assert len(broker.calls) == 1


@pytest.mark.parametrize("scope", [(), ("$target",), ("relative.tmp",)])
def test_missing_or_unresolved_scope_does_not_invoke_broker(tmp_path, scope):
    target = tmp_path / "one.tmp"; target.touch()
    broker = Broker()
    with pytest.raises(CapabilityCallFailed): invoke(broker, task_for(*scope), path=str(target))
    assert broker.calls == []


def test_schema_repair_cannot_switch_to_another_target(tmp_path):
    wanted = tmp_path / "wanted.tmp"; wanted.touch()
    other = tmp_path / "other.tmp"; other.touch()
    broker = Broker()
    def reject(request):
        broker.calls.append(request)
        raise ValueError("Unknown automation fields")
    broker.invoke = reject
    with pytest.raises(CapabilityCallFailed, match="does not match"):
        invoke_capability(broker, "files.manage", {"operation":"delete", "path":str(wanted)}, authority_mode="full_access", task=task_for(wanted), repair=lambda _: {"operation":"delete", "path":str(other)})
    assert len(broker.calls) == 1
    assert broker.calls[0]["arguments"]["path"] == str(wanted)


def test_recursive_goal_pattern_accepts_root_and_nested_matches(tmp_path):
    (tmp_path / "one.tmp").touch(); (tmp_path / "nested").mkdir(); (tmp_path / "nested" / "two.tmp").touch()
    broker = Broker()
    invoke(broker, task_for(tmp_path / "**" / "*.tmp"), path=str(tmp_path), pattern="*.tmp", recursive=True)
    assert len(broker.calls[0]["arguments"]["paths"]) == 2


@pytest.mark.parametrize("route", ["direct", "agent", "workflow"])
def test_every_execution_route_carries_the_task_scope(tmp_path, route):
    wanted = tmp_path / "wanted.tmp"; wanted.touch()
    other = tmp_path / "other.tmp"; other.touch()
    broker, task = Broker(), task_for(wanted)
    arguments = {"operation":"delete", "path":str(other)}
    if route == "direct":
        from app.backend.chat.runners import LiveRunners
        result = LiveRunners(broker=broker, capabilities=["files.manage"], task=task, authority_mode="full_access").run_action(decision={"capability":"files.manage", "arguments":arguments}, request="Delete wanted.tmp")
        assert result["status"] == "failed"
    elif route == "agent":
        from app.backend.chat.agent_loop import AgentLoop
        replies = iter([json.dumps({"action":"files.manage", "arguments":arguments}), json.dumps({"action":"respond", "answer":"The selected path was refused."})])
        AgentLoop(broker=broker, capabilities=["files.manage"], task=task, authority_mode="full_access", generate=lambda _: next(replies)).run("Delete wanted.tmp")
    else:
        from app.backend.workflow import Executor, PlanNode
        executor = Executor(broker=broker, task=task, authority_mode="full_access", granted=["files.manage"])
        with pytest.raises(CapabilityCallFailed):
            executor._perform(PlanNode(node_id="delete", objective="Delete requested file", capability="files.manage", arguments=arguments), arguments)
    assert broker.calls == []


@pytest.mark.parametrize("outside", [False, True])
def test_workflow_observed_reference_is_checked_after_resolution(tmp_path, outside):
    from app.backend.workflow import Executor, Plan, PlanNode
    wanted = tmp_path / "wanted.tmp"; wanted.touch()
    other = tmp_path / "other.tmp"; other.touch()
    broker = Broker()
    node = PlanNode(node_id="delete", objective="Delete wanted file", capability="files.manage", arguments={"operation":"delete", "path":str(tmp_path), "paths":{"$ref":"find.output.paths"}})
    plan = Plan(goal="Delete wanted file")
    plan.observations["find"] = {"paths":[str(other if outside else wanted)]}
    executor = Executor(broker=broker, task=task_for(wanted), authority_mode="full_access", granted=["files.manage"])
    result = executor.run_node(plan, node)
    assert len(broker.calls) == (0 if outside else 1)
    if not outside:
        assert broker.calls[0]["arguments"]["paths"] == [str(wanted)]


def test_empty_or_unreadable_selection_cannot_reach_mutation(tmp_path, monkeypatch):
    broker = Broker()
    with pytest.raises(CapabilityCallFailed):
        invoke(broker, task_for(tmp_path / "*.tmp"), path=str(tmp_path), pattern="*.tmp")
    def denied(*args, **kwargs): raise PermissionError("enumeration denied")
    monkeypatch.setattr("app.backend.automation.broker._matching_paths", denied)
    with pytest.raises(CapabilityCallFailed, match="enumeration denied"):
        invoke(broker, task_for(tmp_path / "*.tmp"), path=str(tmp_path), pattern="*.tmp")
    assert broker.calls == []
