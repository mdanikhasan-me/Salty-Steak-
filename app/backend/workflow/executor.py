"""Salty Steak Native Desktop AI Platform — plan execution and verification.

The runtime half of the operator: it carries out decided work and checks that
the work actually happened.

The executor does no open-ended reasoning. It resolves a node's arguments from
world state and earlier results, asks policy, runs the capability or connector,
verifies the effect, and moves on. It returns to the model only when something
genuinely needs judgement — a failure it cannot classify, or a plan that no
longer fits reality.

Verification is the point of the whole thing. A connector returning success
says the service accepted a request; it does not say the label is on the
message. Anything that can be re-read is re-read, and a node that cannot be
checked says so rather than claiming a confidence it has not earned.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..automation.credentials import redact
from ..automation.policy import DENY, REQUIRE_APPROVAL, PolicyEngine, await_approval
from .plan import (
    NODE_COMPLETED,
    NODE_FAILED,
    NODE_RUNNING,
    NODE_VERIFYING,
    NODE_WAITING,
    Plan,
    PlanNode,
)

EXECUTOR_SCHEMA = "salty-steak-executor-v1"




CAUSE_TRANSIENT = "transient"
CAUSE_STALE = "stale"
CAUSE_NOT_FOUND = "not_found"
CAUSE_CONFLICT = "conflict"
CAUSE_AUTHENTICATION = "authentication"
CAUSE_APPROVAL = "approval"
CAUSE_DENIED = "denied"
CAUSE_CANCELLED = "cancelled"
CAUSE_UNKNOWN = "unknown"



RETRYABLE = frozenset({CAUSE_TRANSIENT, CAUSE_STALE})


NEEDS_USER = frozenset({CAUSE_APPROVAL, CAUSE_AUTHENTICATION})

FAILURE_KINDS = {
    "timeout": CAUSE_TRANSIENT,
    "transient": CAUSE_TRANSIENT,
    "stale_element": CAUSE_STALE,
    "wrong_tab": CAUSE_STALE,
    "not_found": CAUSE_NOT_FOUND,
    "unknown_tab": CAUSE_NOT_FOUND,
    "no_match": CAUSE_NOT_FOUND,
    "conflict": CAUSE_CONFLICT,
    "ambiguous": CAUSE_CONFLICT,
    "authentication_required": CAUSE_AUTHENTICATION,
    "approval_required": CAUSE_APPROVAL,
    "denied": CAUSE_DENIED,
    "cancelled": CAUSE_CANCELLED,
}


def classify_failure(error: BaseException) -> str:
    """Work out why a node failed, from the kind the layer below reported."""

    kind = str(getattr(error, "kind", "") or "")
    if kind in FAILURE_KINDS:
        return FAILURE_KINDS[kind]
    name = type(error).__name__
    if name in {"TimeoutError", "ConnectionError"}:
        return CAUSE_TRANSIENT
    if name == "PermissionError":
        return CAUSE_DENIED
    return CAUSE_UNKNOWN


@dataclass
class NodeOutcome:
    """What happened to one node."""

    node_id: str
    state: str
    cause: str | None = None
    verified: bool | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node_id,
            "state": self.state,
            "cause": self.cause,
            "verified": self.verified,
            "seconds": round(self.seconds, 3),
            **redact(self.detail),
        }


class NeedsUser(RuntimeError):
    """Raised when a node cannot proceed without the user."""

    def __init__(self, message: str, *, reason: str, node_id: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.node_id = node_id


class Executor:
    """Run plan nodes deterministically, and prove they worked."""

    def __init__(
        self,
        *,
        broker: Any = None,
        connectors: Any = None,
        images: Any = None,
        task: Any = None,
        policy: PolicyEngine | None = None,
        approve: Callable[[Mapping[str, Any]], bool] | None = None,
        authority_mode: str = "ask_every_time",
        on_node: Callable[[NodeOutcome], None] | None = None,
        granted: Any = (),
        repair: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.broker = broker
        self.connectors = connectors
        self.images = images
        self.task = task
        self.policy = policy or PolicyEngine(authority_mode=authority_mode)
        self.approve = approve
        self.authority_mode = authority_mode
        self.on_node = on_node
        self.granted = list(granted)
        self.repair = repair



    def run_node(self, plan: Plan, node: PlanNode) -> NodeOutcome:
        started = time.monotonic()
        node.attempts += 1
        node.state = NODE_RUNNING
        self._event("node_started", node=node.node_id, target=node.target)

        try:
            arguments = self._arguments(plan, node)
        except Exception as error:
            return self._fail(node, CAUSE_UNKNOWN, str(error), started)


        if self._stopped():
            return self._fail(node, CAUSE_CANCELLED, "The task was stopped.", started)

        try:
            result = self._perform(node, arguments)
        except NeedsUser:
            node.state = NODE_WAITING
            raise
        except BaseException as error:
            cause = classify_failure(error)
            if cause == CAUSE_CANCELLED:
                return self._fail(node, cause, "The task was stopped.", started)
            return self._fail(node, cause, f"{type(error).__name__}: {error}", started)

        node.result = result
        if node.result_as:
            plan.variables[node.result_as] = result

        node.state = NODE_VERIFYING
        node.verified = self._verify(plan, node, arguments, result)
        node.state = NODE_COMPLETED
        node.failure = None

        outcome = NodeOutcome(
            node_id=node.node_id,
            state=NODE_COMPLETED,
            verified=node.verified,
            detail={"target": node.target},
            seconds=time.monotonic() - started,
        )
        self._emit(outcome)
        return outcome



    def _verify(
        self,
        plan: Plan,
        node: PlanNode,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> bool | None:
        """Confirm the effect against the system that holds it.

        Returns None when nothing could be checked, which is reported rather
        than rounded up to success.
        """

        if node.verify is None:
            return None
        try:
            specification = plan.resolve(node.verify)
        except Exception:
            specification = dict(node.verify)

        expectation = specification.get("expect")
        try:
            if node.connector and self.connectors is not None:
                observed = self._verify_connector(node, specification, arguments)
            elif node.capability and self.broker is not None:
                observed = self._verify_capability(specification)
            else:
                return None
        except Exception as error:
            self._event(
                "verification_failed", node=node.node_id, error=str(error)[:200]
            )
            return False

        verdict = self._compare(observed, expectation, specification)
        self._event("verified", node=node.node_id, verified=verdict)
        return verdict

    def _verify_connector(
        self,
        node: PlanNode,
        specification: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> Any:
        operation = specification.get("operation")
        payload = dict(specification.get("arguments") or {})
        if operation:
            return self.connectors.invoke(
                specification.get("connector") or node.connector, operation, payload
            ).data


        return self.connectors.verify(node.connector, node.operation, payload).data

    def _verify_capability(self, specification: Mapping[str, Any]) -> Any:
        return self.broker.invoke(
            {
                "capability": specification["capability"],
                "arguments": dict(specification.get("arguments") or {}),
                "user_confirmed": True,
                "authority_mode": self.authority_mode,
            }
        )

    @staticmethod
    def _observed_count(observed: Any) -> int:
        """How many records a read actually found.

        The total, not the page. A paged read returns at most one page, so
        counting ``items`` would report 100 for a search that matched 140 and
        fail a verification that was in fact satisfied.
        """

        if isinstance(observed, Mapping):
            total = observed.get("total_estimate")
            if isinstance(total, int):
                return total
            items = observed.get("items")
            if isinstance(items, (list, tuple)):
                return len(items)
            count = observed.get("count")
            if isinstance(count, int):
                return count
        if isinstance(observed, (list, tuple)):
            return len(observed)
        return 0

    @staticmethod
    def _compare(
        observed: Any, expectation: Any, specification: Mapping[str, Any]
    ) -> bool:
        """Decide whether what was read back matches what was intended."""

        if expectation is None:

            return observed is not None

        text = str(observed)
        if isinstance(expectation, Mapping):
            if "contains" in expectation:
                return str(expectation["contains"]) in text
            if "count" in expectation:
                return Executor._observed_count(observed) == expectation["count"]
            if "at_least" in expectation:
                return Executor._observed_count(observed) >= int(
                    expectation["at_least"]
                )
            return all(
                str(value) in text for value in expectation.values()
            )
        return str(expectation) in text



    def _arguments(self, plan: Plan, node: PlanNode) -> dict[str, Any]:
        arguments = plan.resolve(node.arguments)
        if self.task is not None and getattr(self.task, "world_state", None) is not None:


            for key, value in list(arguments.items()):
                if isinstance(value, Mapping) and set(value) == {"$world"}:
                    arguments[key] = self.task.world_state.get(str(value["$world"]))
        return arguments

    def _perform(self, node: PlanNode, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if node.capability == "image.generate":



            if self.images is None:
                from ..connectors.contract import ConnectorError

                raise ConnectorError(
                    "Image generation is not configured.", kind="backend_unavailable"
                )
            job = self.images.prepare(
                {"brief": arguments.get("brief") or arguments},
                original_request=str(arguments.get("request") or node.objective),
            )
            return self.images.result_for_model(self.images.run(job))

        if node.connector:
            result = self.connectors.invoke(
                node.connector,
                node.operation,
                arguments,
                should_stop=self._stopped,
            )
            return result.data

        decision = self.policy.evaluate(node.capability, arguments)
        if decision.outcome == DENY:
            raise NeedsUser(
                decision.reason, reason="policy_denied", node_id=node.node_id
            )
        if decision.outcome == REQUIRE_APPROVAL:
            approved = (
                await_approval(decision, ask=self.approve, task=self.task)
                if self.approve is not None
                else False
            )
            if not approved:
                raise NeedsUser(
                    decision.reason, reason="user_approval", node_id=node.node_id
                )








        from ..automation.invocation import invoke_capability

        result = invoke_capability(
            self.broker,
            node.capability,
            arguments,
            authority_mode=self.authority_mode,
            granted=self.granted or [node.capability],
            repair=self.repair,
        )
        if isinstance(result, Mapping) and result.get("status") not in {
            None,
            "succeeded",
        }:
            from ..connectors.contract import ConnectorError

            raise ConnectorError(
                str(result.get("error") or result.get("reason") or "The step failed."),
                kind=str(result.get("failure_kind") or "failed"),
            )

        if self.task is not None and getattr(self.task, "world_state", None) is not None:
            self.task.world_state.absorb(node.capability, result or {})
        return dict(result or {})



    def _fail(
        self, node: PlanNode, cause: str, message: str, started: float
    ) -> NodeOutcome:
        node.state = NODE_FAILED
        node.failure = message[:500]
        outcome = NodeOutcome(
            node_id=node.node_id,
            state=NODE_FAILED,
            cause=cause,
            detail={"target": node.target, "error": node.failure},
            seconds=time.monotonic() - started,
        )
        self._emit(outcome)
        return outcome

    def _emit(self, outcome: NodeOutcome) -> None:
        self._event("node_finished", **outcome.to_dict())
        if self.on_node is not None:
            try:
                self.on_node(outcome)
            except BaseException:
                return

    def _event(self, kind: str, **detail: Any) -> None:
        if self.task is not None:
            self.task.record_event(kind, **detail)

    def _stopped(self) -> bool:
        return bool(self.task is not None and self.task.stop_requested)


__all__ = [
    "CAUSE_APPROVAL",
    "CAUSE_AUTHENTICATION",
    "CAUSE_CANCELLED",
    "CAUSE_CONFLICT",
    "CAUSE_DENIED",
    "CAUSE_NOT_FOUND",
    "CAUSE_STALE",
    "CAUSE_TRANSIENT",
    "CAUSE_UNKNOWN",
    "EXECUTOR_SCHEMA",
    "Executor",
    "NEEDS_USER",
    "NeedsUser",
    "NodeOutcome",
    "RETRYABLE",
    "classify_failure",
]
