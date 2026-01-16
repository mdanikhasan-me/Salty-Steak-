"""Salty Steak Native Desktop AI Platform — workflow execution and recovery.

Runs a plan to completion, survives being interrupted, and knows the difference
between work it can retry and work it must ask about.

Long tasks get interrupted: the user stops them, a login expires, the machine
restarts. A workflow that could only start from the beginning would either
redo side effects — sending the same mail twice — or refuse to resume at all.
So execution state is checkpointed, and recovery re-verifies what was already
done before continuing rather than trusting a record that a step completed.

Replanning is bounded on purpose. A model asked to fix a broken plan will
happily produce the same plan again; without a limit that is an infinite loop
that looks like progress.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..chat.task_runtime import (
    COMPLETED,
    EXECUTING,
    FAILED,
    PLANNING,
    STOPPED,
    VERIFYING,
    WAITING,
    TaskContext,
)
from .executor import (
    CAUSE_CANCELLED,
    NEEDS_USER,
    RETRYABLE,
    Executor,
    NeedsUser,
    NodeOutcome,
)
from .plan import (
    NODE_COMPLETED,
    NODE_FAILED,
    NODE_PENDING,
    NODE_SKIPPED,
    Plan,
    PlanError,
    PlanNode,
    build_plan,
)

WORKFLOW_SCHEMA = "salty-steak-workflow-v1"




MAX_REPLANS = 3

MAX_TOTAL_NODES = 400



WAIT_SIGN_IN = "user_sign_in"
WAIT_APPROVAL = "user_approval"
WAIT_ANSWER = "user_answer"
WAIT_TWO_FACTOR = "user_2fa"
WAIT_DESTRUCTIVE_CHOICE = "ambiguous_destructive_choice"
WAIT_REASONS = (
    WAIT_SIGN_IN,
    WAIT_APPROVAL,
    WAIT_ANSWER,
    WAIT_TWO_FACTOR,
    WAIT_DESTRUCTIVE_CHOICE,
)


@dataclass
class WorkflowResult:
    """How a workflow ended."""

    state: str
    plan: Plan
    outcomes: list[NodeOutcome] = field(default_factory=list)
    waiting_for: str | None = None
    waiting_node: str | None = None
    message: str = ""
    replans: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORKFLOW_SCHEMA,
            "state": self.state,
            "message": self.message,
            "waiting_for": self.waiting_for,
            "waiting_node": self.waiting_node,
            "replans": self.replans,
            "plan": self.plan.to_dict(),
            "nodes": [outcome.to_dict() for outcome in self.outcomes],
        }


class WorkflowEngine:
    """Drive a plan, recover from what it can, and ask about what it cannot."""

    def __init__(
        self,
        *,
        executor: Executor,
        task: TaskContext,
        replan: Callable[[Plan, NodeOutcome], Mapping[str, Any] | None] | None = None,
        checkpoint_path: str | Path | None = None,
        max_replans: int = MAX_REPLANS,
    ) -> None:
        self.executor = executor
        self.task = task
        self.replan = replan
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.max_replans = max(0, int(max_replans))
        self.replans = 0


        self._attempted: set[str] = set()



    def run(self, plan: Plan) -> WorkflowResult:
        outcomes: list[NodeOutcome] = []
        self._attempted.add(self._fingerprint(plan))
        self.task.current_plan = [node.to_dict() for node in plan.nodes]
        self.task.transition(EXECUTING, plan=plan.plan_id, nodes=len(plan.nodes))

        while not plan.finished:
            if self.task.stop_requested:


                self._checkpoint(plan)
                self.task.finish_stopped()
                return WorkflowResult(
                    STOPPED, plan, outcomes, message="Stopped before the next step."
                )

            ready = plan.ready()
            if not ready:
                break

            node = ready[0]
            self.task.current_step = len(outcomes) + 1
            self.task.current_capability = node.target

            try:
                outcome = self.executor.run_node(plan, node)
            except NeedsUser as pause:
                self._checkpoint(plan)
                reason = (
                    WAIT_APPROVAL
                    if pause.reason in {"user_approval", "policy_denied"}
                    else WAIT_ANSWER
                )
                self.task.transition(
                    WAITING, waiting_for=reason, node=pause.node_id
                )
                return WorkflowResult(
                    WAITING,
                    plan,
                    outcomes,
                    waiting_for=reason,
                    waiting_node=pause.node_id,
                    message=str(pause),
                    replans=self.replans,
                )

            outcomes.append(outcome)
            self._checkpoint(plan)

            if outcome.state == NODE_COMPLETED:
                continue
            if outcome.cause == CAUSE_CANCELLED:
                self.task.finish_stopped()
                return WorkflowResult(
                    STOPPED, plan, outcomes, message="Stopped part-way through."
                )

            recovered = self._recover(plan, node, outcome, outcomes)
            if recovered is not None:
                return recovered

        self._checkpoint(plan)
        return self._finish(plan, outcomes)



    def _recover(
        self,
        plan: Plan,
        node: PlanNode,
        outcome: NodeOutcome,
        outcomes: list[NodeOutcome],
    ) -> WorkflowResult | None:
        """Retry, fall back, or replan — in that order of cost."""

        if outcome.cause in NEEDS_USER:
            self._checkpoint(plan)
            reason = (
                WAIT_SIGN_IN if outcome.cause == "authentication" else WAIT_APPROVAL
            )
            self.task.transition(WAITING, waiting_for=reason, node=node.node_id)
            return WorkflowResult(
                WAITING,
                plan,
                outcomes,
                waiting_for=reason,
                waiting_node=node.node_id,
                message=node.failure or "",
                replans=self.replans,
            )



        if outcome.cause in RETRYABLE and node.attempts < node.max_attempts:
            node.state = NODE_PENDING
            self.task.metrics.retries += 1
            self.task.record_event(
                "node_retry", node=node.node_id, attempt=node.attempts, cause=outcome.cause
            )
            return None



        if node.fallback:
            self.task.metrics.escalations += 1
            self.task.record_event("node_fallback", node=node.node_id)
            for key, value in node.fallback.items():
                setattr(node, key, value) if hasattr(node, key) else None
            node.state = NODE_PENDING
            node.attempts = 0
            node.fallback = None
            return None


        return self._request_replan(plan, node, outcome, outcomes)

    def _request_replan(
        self,
        plan: Plan,
        node: PlanNode,
        outcome: NodeOutcome,
        outcomes: list[NodeOutcome],
    ) -> WorkflowResult | None:
        if self.replan is None or self.replans >= self.max_replans:
            return self._finish(plan, outcomes)

        self.task.transition(PLANNING, replanning=node.node_id)
        self.replans += 1
        self.task.metrics.replans += 1
        try:
            revised = self.replan(plan, outcome)
        except Exception as error:
            self.task.record_event("replan_failed", error=str(error)[:200])
            return self._finish(plan, outcomes)

        if not revised:
            return self._finish(plan, outcomes)

        try:
            replacement = build_plan(revised, goal=plan.goal)
        except PlanError as error:
            self.task.record_event("replan_rejected", error=str(error)[:200])
            return self._finish(plan, outcomes)

        fingerprint = self._fingerprint(replacement)
        if fingerprint in self._attempted:


            self.task.metrics.stagnation_breaks += 1
            self.task.record_event("replan_repeated", node=node.node_id)
            return self._finish(plan, outcomes)

        self._attempted.add(fingerprint)
        if len(plan.nodes) + len(replacement.nodes) > MAX_TOTAL_NODES:
            return self._finish(plan, outcomes)






        done = {
            (existing.node_id, existing.target, json.dumps(existing.arguments, sort_keys=True, default=str)): existing
            for existing in plan.nodes
            if existing.state == NODE_COMPLETED
        }
        for fresh in replacement.nodes:
            key = (
                fresh.node_id,
                fresh.target,
                json.dumps(fresh.arguments, sort_keys=True, default=str),
            )
            if key in done:
                fresh.state = NODE_COMPLETED
                fresh.result = done[key].result
                fresh.verified = done[key].verified
        replacement.version = plan.version + 1
        replacement.variables.update(plan.variables)
        self.task.record_event(
            "replanned", version=replacement.version, nodes=len(replacement.nodes)
        )
        return self.run(replacement)



    def _finish(self, plan: Plan, outcomes: list[NodeOutcome]) -> WorkflowResult:


        plan.settle()
        if plan.succeeded:
            self.task.transition(VERIFYING, plan=plan.plan_id)
            unverified = [
                node.node_id for node in plan.nodes if node.verified is False
            ]
            if unverified:


                self.task.finish(FAILED, failure="Verification failed.")
                return WorkflowResult(
                    FAILED,
                    plan,
                    outcomes,
                    message=(
                        "These steps ran but could not be confirmed: "
                        + ", ".join(unverified)
                    ),
                    replans=self.replans,
                )
            self.task.finish(COMPLETED)
            return WorkflowResult(
                COMPLETED, plan, outcomes, message="Done.", replans=self.replans
            )

        failed = [node for node in plan.nodes if node.state == NODE_FAILED]
        skipped = [node for node in plan.nodes if node.state == NODE_SKIPPED]
        message = "; ".join(
            f"{node.node_id}: {node.failure}" for node in failed
        ) or "The plan could not be completed."
        if skipped:
            message += f" ({len(skipped)} later steps were not attempted)"
        self.task.finish(FAILED, failure=message)
        return WorkflowResult(
            FAILED, plan, outcomes, message=message, replans=self.replans
        )



    def _checkpoint(self, plan: Plan) -> None:
        """Write enough to resume without redoing anything that already ran."""

        if self.checkpoint_path is None:
            return
        payload = {
            "schema": WORKFLOW_SCHEMA,
            "task_id": self.task.task_id,
            "goal": plan.goal,
            "plan_id": plan.plan_id,
            "version": plan.version,
            "replans": self.replans,
            "saved_at": time.time(),
            "variables": plan.variables,
            "nodes": [
                {
                    **node.to_dict(),
                    "arguments": node.arguments,
                    "result_as": node.result_as,
                    "verify": node.verify,
                }
                for node in plan.nodes
            ],
        }
        try:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)


            temporary = self.checkpoint_path.with_suffix(".partial")
            temporary.write_text(json.dumps(payload, default=str), encoding="utf-8")
            temporary.replace(self.checkpoint_path)
        except OSError:


            self.task.record_event("checkpoint_failed", path=str(self.checkpoint_path))

    @staticmethod
    def _fingerprint(plan: Plan) -> str:
        return json.dumps(
            [
                [node.target, sorted(node.arguments), list(node.depends_on)]
                for node in plan.nodes
            ],
            sort_keys=True,
            default=str,
        )


def load_checkpoint(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resume(
    checkpoint: Mapping[str, Any],
    *,
    engine: WorkflowEngine,
    reverify: bool = True,
) -> WorkflowResult:
    """Continue an interrupted workflow without repeating its side effects.

    A record saying a node completed is not proof the effect survived. Anything
    that can be re-checked is re-checked before it is trusted; a node whose
    effect is gone becomes pending again, and one that never ran is untouched.
    """

    nodes = []
    for entry in checkpoint.get("nodes", []):
        node = PlanNode(
            node_id=entry["node"],
            objective=entry.get("objective", ""),
            capability=entry.get("capability"),
            connector=entry.get("connector"),
            operation=entry.get("operation"),
            arguments=dict(entry.get("arguments") or {}),
            depends_on=tuple(entry.get("depends_on") or ()),
            verify=entry.get("verify"),
            result_as=entry.get("result_as"),
        )
        node.state = entry.get("state", NODE_PENDING)
        node.verified = entry.get("verified")
        node.attempts = int(entry.get("attempts") or 0)


        if node.state in {NODE_FAILED, NODE_SKIPPED}:
            node.state = NODE_PENDING
            node.attempts = 0
        nodes.append(node)

    plan = Plan(
        goal=str(checkpoint.get("goal") or ""),
        nodes=nodes,
        variables=dict(checkpoint.get("variables") or {}),
    )
    plan.version = int(checkpoint.get("version") or 1)

    if reverify:
        for node in plan.nodes:
            if node.state != NODE_COMPLETED or node.verify is None:
                continue
            engine.task.record_event("recovery_check", node=node.node_id)
            still_true = engine.executor._verify(plan, node, node.arguments, node.result or {})
            if still_true is False:



                node.state = NODE_PENDING
                node.attempts = 0
                node.verified = None
                engine.task.record_event("recovery_redo", node=node.node_id)

    return engine.run(plan)


__all__ = [
    "MAX_REPLANS",
    "WAIT_ANSWER",
    "WAIT_APPROVAL",
    "WAIT_DESTRUCTIVE_CHOICE",
    "WAIT_REASONS",
    "WAIT_SIGN_IN",
    "WAIT_TWO_FACTOR",
    "WORKFLOW_SCHEMA",
    "WorkflowEngine",
    "WorkflowResult",
    "load_checkpoint",
    "resume",
]
