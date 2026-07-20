"""Salty Steak Native Desktop AI Platform — structured plans.

A plan is what the model decided, written down so the runtime can carry it out
without asking again.

The agent loop asks the model what to do next after every single step. That is
right when the next move genuinely depends on what just happened, and wasteful
when it does not: reading a mailbox, filtering it and applying a label is one
decision followed by a great deal of deterministic work. A plan separates the
two, so the model is consulted at the points that need judgement and the
runtime does the rest.

Nodes name what they need rather than when they run. Dependencies give the
order, which means a plan can be checkpointed, resumed, and partially retried
without re-deriving what was already true.
"""

from __future__ import annotations

import copy
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PLAN_SCHEMA = "salty-steak-plan-v1"


NODE_PENDING = "pending"
NODE_READY = "ready"
NODE_RUNNING = "running"
NODE_VERIFYING = "verifying"
NODE_COMPLETED = "completed"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"
NODE_WAITING = "waiting"

TERMINAL_NODE_STATES = frozenset({NODE_COMPLETED, NODE_FAILED, NODE_SKIPPED})

MAX_NODES = 200


class PlanError(ValueError):
    """Raised when a plan could not be built or is not runnable."""


@dataclass
class PlanNode:
    """One unit of work the executor can perform without further reasoning."""

    node_id: str
    objective: str

    capability: str | None = None
    connector: str | None = None
    operation: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()


    verify: dict[str, Any] | None = None
    max_attempts: int = 2

    fallback: dict[str, Any] | None = None

    result_as: str | None = None

    state: str = NODE_PENDING
    attempts: int = 0
    result: dict[str, Any] | None = None
    verified: bool | None = None
    failure: str | None = None



    duration_ms: float | None = None

    def __post_init__(self) -> None:
        if bool(self.capability) == bool(self.connector):
            raise PlanError(
                f"Node {self.node_id!r} must name either a capability or a "
                "connector, not both and not neither."
            )
        if self.connector and not self.operation:
            raise PlanError(f"Node {self.node_id!r} names a connector but no operation.")

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_NODE_STATES

    @property
    def target(self) -> str:
        return self.capability or f"{self.connector}.{self.operation}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node_id,
            "objective": self.objective,
            "target": self.target,
            "capability": self.capability,
            "connector": self.connector,
            "operation": self.operation,
            "depends_on": list(self.depends_on),
            "state": self.state,
            "attempts": self.attempts,
            "verified": self.verified,
            "failure": self.failure,
            "duration_ms": self.duration_ms,




            "observation": self._reportable_result(),
        }

    def _reportable_result(self) -> dict[str, Any]:
        result = self.result if isinstance(self.result, dict) else {}
        artifact = result.get("artifact")
        return {"artifact": artifact} if isinstance(artifact, Mapping) else {}


@dataclass
class Plan:
    """The whole intended route from goal to done."""

    goal: str
    nodes: list[PlanNode] = field(default_factory=list)
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    variables: dict[str, Any] = field(default_factory=dict)



    observations: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self._validate()



    def _validate(self) -> None:
        if len(self.nodes) > MAX_NODES:
            raise PlanError(f"A plan may hold at most {MAX_NODES} nodes.")
        seen: set[str] = set()
        for node in self.nodes:
            if node.node_id in seen:
                raise PlanError(f"Two nodes share the id {node.node_id!r}.")
            seen.add(node.node_id)
        for node in self.nodes:
            for dependency in node.depends_on:
                if dependency not in seen:
                    raise PlanError(
                        f"Node {node.node_id!r} depends on {dependency!r}, which "
                        "is not in the plan."
                    )
        self._detect_cycles()

    def _detect_cycles(self) -> None:
        """A plan that waits on itself would hang rather than fail."""

        colour: dict[str, int] = {}

        def visit(node_id: str, trail: tuple[str, ...]) -> None:
            state = colour.get(node_id, 0)
            if state == 1:
                raise PlanError(
                    "These nodes depend on each other in a circle: "
                    + " -> ".join([*trail, node_id])
                )
            if state == 2:
                return
            colour[node_id] = 1
            for dependency in self.node(node_id).depends_on:
                visit(dependency, (*trail, node_id))
            colour[node_id] = 2

        for node in self.nodes:
            visit(node.node_id, ())

    def node(self, node_id: str) -> PlanNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise PlanError(f"There is no node called {node_id!r}.")



    def ready(self) -> list[PlanNode]:
        """Nodes whose dependencies are all satisfied.

        A node whose dependency failed is not ready and never will be; it is
        skipped rather than left pending forever.
        """

        available = []
        for node in self.nodes:
            if node.state not in {NODE_PENDING, NODE_READY}:
                continue
            states = [self.node(name).state for name in node.depends_on]
            if any(state in {NODE_FAILED, NODE_SKIPPED} for state in states):
                node.state = NODE_SKIPPED
                node.failure = "A step this depends on did not succeed."
                continue
            if all(state == NODE_COMPLETED for state in states):
                node.state = NODE_READY
                available.append(node)
        return available

    def settle(self) -> None:
        """Propagate skips until nothing more can change.

        A node whose dependency failed will never run, and saying so is the
        difference between a report that lists what was not attempted and one
        that leaves steps sitting at pending forever.
        """

        for _ in range(len(self.nodes) + 1):
            before = [node.state for node in self.nodes]
            self.ready()
            if [node.state for node in self.nodes] == before:
                return

    @property
    def finished(self) -> bool:
        return all(node.finished for node in self.nodes)

    @property
    def succeeded(self) -> bool:
        return bool(self.nodes) and all(
            node.state == NODE_COMPLETED for node in self.nodes
        )

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for node in self.nodes:
            tally[node.state] = tally.get(node.state, 0) + 1
        return tally



    def resolve(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Substitute earlier results into a node's arguments.

        A value written as ``{"$from": "name"}`` is replaced by that workflow
        variable. ``{"$ref": "find.output.items[0].id"}`` addresses a field in
        an earlier node's structured observation. Both preserve the value's
        native type; neither asks the model to see, copy, or guess runtime data.
        """

        def substitute(value: Any) -> Any:
            if isinstance(value, Mapping):
                if set(value) == {"$from"}:
                    key = str(value["$from"])
                    if key not in self.variables:
                        raise PlanError(
                            f"Nothing has produced a value called {key!r} yet."
                        )
                    return self.variables[key]
                if set(value) == {"$ref"}:
                    return self._resolve_reference(value["$ref"])
                return {key: substitute(item) for key, item in value.items()}
            if isinstance(value, list):
                return [substitute(item) for item in value]
            return value

        return {key: substitute(item) for key, item in dict(arguments).items()}

    def _resolve_reference(self, value: Any) -> Any:
        reference = str(value or "").strip()
        if not reference:
            raise PlanError("A runtime reference cannot be empty.")
        if len(reference) > 2_048:
            raise PlanError("A runtime reference is too long.")



        node_id = next(
            (
                candidate
                for candidate in sorted(self.observations, key=len, reverse=True)
                if reference == f"{candidate}.output"
                or reference.startswith((f"{candidate}.output.", f"{candidate}.output["))
            ),
            None,
        )
        if node_id is None:
            raise PlanError(
                f"Runtime reference {reference!r} could not resolve: no completed "
                "node has produced that output."
            )

        current = self.observations[node_id]
        suffix = reference[len(f"{node_id}.output") :]
        position = 0
        segment = re.compile(r"(?:\.([^\.\[\]]+)|\[(\d+)\])")
        while position < len(suffix):
            match = segment.match(suffix, position)
            if match is None:
                raise PlanError(
                    f"Runtime reference {reference!r} could not resolve: invalid path syntax."
                )
            field_name, index_text = match.groups()
            if field_name is not None:
                if not isinstance(current, Mapping) or field_name not in current:
                    raise PlanError(
                        f"Runtime reference {reference!r} could not resolve field "
                        f"{field_name!r}."
                    )
                current = current[field_name]
            else:
                index = int(index_text)
                if (
                    not isinstance(current, Sequence)
                    or isinstance(current, (str, bytes, bytearray))
                    or index >= len(current)
                ):
                    raise PlanError(
                        f"Runtime reference {reference!r} could not resolve index {index}."
                    )
                current = current[index]
            position = match.end()


        return copy.deepcopy(current)



    def to_dict(self, *, include_nodes: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "version": self.version,
            "goal": self.goal,
            "node_count": len(self.nodes),
            "counts": self.counts(),
            "finished": self.finished,
            "succeeded": self.succeeded,
            "variables": sorted(self.variables),
            "observations": sorted(self.observations),
        }
        if include_nodes:
            payload["nodes"] = [node.to_dict() for node in self.nodes]
        return payload


def build_plan(payload: Mapping[str, Any], *, goal: str = "") -> Plan:
    """Turn the model's structured plan into one the executor can run.

    Everything is validated here, before anything runs: a plan that names a
    missing dependency or contradicts itself must fail while it is still only
    a description.
    """

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise PlanError("A plan needs at least one step.")

    nodes = []
    for index, entry in enumerate(raw_nodes, start=1):
        if not isinstance(entry, Mapping):
            raise PlanError(f"Step {index} is not an object.")
        try:
            nodes.append(
                PlanNode(
                    node_id=str(entry.get("node") or entry.get("id") or f"n{index}"),
                    objective=str(entry.get("objective") or entry.get("reason") or ""),
                    capability=entry.get("capability"),
                    connector=entry.get("connector"),
                    operation=entry.get("operation"),
                    arguments=dict(entry.get("arguments") or {}),
                    depends_on=tuple(entry.get("depends_on") or ()),
                    verify=dict(entry["verify"]) if entry.get("verify") else None,
                    max_attempts=max(1, min(int(entry.get("max_attempts") or 2), 5)),
                    fallback=dict(entry["fallback"]) if entry.get("fallback") else None,
                    result_as=entry.get("result_as"),
                )
            )
        except PlanError:
            raise
        except (TypeError, ValueError) as error:
            raise PlanError(f"Step {index} is not usable: {error}") from error

    return Plan(
        goal=str(payload.get("goal") or goal),
        nodes=nodes,
        variables=dict(payload.get("variables") or {}),
    )


__all__ = [
    "MAX_NODES",
    "NODE_COMPLETED",
    "NODE_FAILED",
    "NODE_PENDING",
    "NODE_READY",
    "NODE_RUNNING",
    "NODE_SKIPPED",
    "NODE_VERIFYING",
    "NODE_WAITING",
    "PLAN_SCHEMA",
    "Plan",
    "PlanError",
    "PlanNode",
    "build_plan",
]
