"""Salty Steak Native Desktop AI Platform — live runners for decided work.

Connects the three decisions that were recognised but not yet carried out:
one action, a plan, and research.

Everything here reuses machinery that already exists and is already proven —
the automation broker, AgentLoop, Plan validation, WorkflowEngine, the research
ledger — because a second implementation of any of them would be a second thing
to keep correct. What was missing was only the wiring from a live chat turn to
those runners, and the compact packets they need.

Base Steak stays the single brain. Planning, replanning and the agent loop all
go through the one inference path; there is no planner model.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..research import Budget, ResearchLoop
from ..workflow import Executor, WorkflowEngine, build_plan
from ..workflow.plan import Plan, PlanError
from .orchestrator import strip_reasoning

RUNNER_SCHEMA = "salty-steak-live-runner-v1"



MAX_LIVE_NODES = 40


class PlanRejected(ValueError):
    """Raised when a model-produced plan cannot be trusted to run."""


def _nodes_in_the_right_slots(
    payload: Mapping[str, Any],
    capabilities: Sequence[str],
) -> Mapping[str, Any]:
    """Put a granted capability the model wrote as a connector back in its slot.

    Asked to open Notepad, focus its window and capture the screen, the live
    model produced exactly the right three steps and wrote each one as
    ``"connector": "application.launch"``. The plan was rejected whole for
    naming a service that is not connected, which was true and useless: it had
    named a capability that *is* granted, in the wrong field, because the one
    worked example in the routing instruction is a connector node.

    A name that is a granted capability can only mean the capability — the two
    namespaces do not overlap — so it is moved rather than refused. Anything
    unrecognised is left exactly where the model put it and still fails
    validation.
    """

    granted = set(capabilities)
    nodes = payload.get("nodes")
    if not granted or not isinstance(nodes, list):
        return payload
    moved: list[Any] = []
    for entry in nodes:
        if (
            isinstance(entry, Mapping)
            and not entry.get("capability")
            and str(entry.get("connector") or "") in granted
        ):
            corrected = dict(entry)
            corrected["capability"] = corrected.pop("connector")


            corrected.pop("operation", None)
            moved.append(corrected)
        else:
            moved.append(entry)
    return {**payload, "nodes": moved}


def validate_plan(
    payload: Mapping[str, Any],
    *,
    goal: str,
    capabilities: Sequence[str],
    connectors: Sequence[str],
) -> Plan:
    """Check a model-authored plan completely before any of it executes.

    Structure is validated by ``build_plan`` — ids, dependencies, cycles. What
    is added here is authority: a plan may only name capabilities that are
    actually granted and connectors that are actually configured. A plan that
    reaches for something it cannot have is rejected whole, because letting it
    start and fail half-way leaves the user with partial side effects.
    """

    try:
        plan = build_plan(
            _nodes_in_the_right_slots(payload, capabilities), goal=goal
        )
    except PlanError as error:
        raise PlanRejected(str(error)) from error

    if len(plan.nodes) > MAX_LIVE_NODES:
        raise PlanRejected(
            f"That plan has {len(plan.nodes)} steps; {MAX_LIVE_NODES} is the limit."
        )

    granted = set(capabilities)
    configured = set(connectors)
    for node in plan.nodes:
        if node.capability and node.capability not in granted:
            raise PlanRejected(
                f"Step {node.node_id!r} needs {node.capability!r}, which is not "
                "enabled."
            )
        if node.connector and node.connector not in configured:
            raise PlanRejected(
                f"Step {node.node_id!r} needs the {node.connector!r} service, "
                "which is not connected."
            )
    return plan


def replan_packet(plan: Plan, outcome: Any, *, world: Mapping[str, Any] | None = None) -> str:
    """The compact brief handed back to Base Steak when a plan needs repair.

    Deliberately small. Resending the conversation and every tool event would
    put the whole failure history through prefill on each replan, which is both
    slow and worse: the model reads the noise instead of the failure.
    """

    packet = {
        "goal": plan.goal,
        "failed_node": outcome.node_id,
        "failure": outcome.cause,
        "error": (outcome.detail or {}).get("error", "")[:400],
        "completed": [
            node.node_id for node in plan.nodes if node.state == "completed"
        ],
        "remaining": [
            node.node_id
            for node in plan.nodes
            if node.state not in {"completed", "failed", "skipped"}
        ],
        "known": dict(world or {}),
    }
    return (
        "A step failed and retrying it will not help. Produce a corrected plan "
        "as one JSON object with the same shape. Keep the completed steps as "
        "they are and change only what is left.\n" + json.dumps(packet, default=str)
    )


def _describe_effect(route: Any, result: Mapping[str, Any]) -> str:
    """Say what actually happened when the model gave no reason of its own.

    "Done." is true and useless: it does not tell the user which application
    opened, which window moved, or where a screenshot went, so there is nothing
    to check the claim against. The broker already reports each of those, so
    the sentence is built from the result rather than from the request.
    """

    capability = str(getattr(route, "capability", "") or "")
    if capability == "application.launch":
        target = str(result.get("target") or "").strip()
        return f"I opened {target}." if target else "I opened that for you."
    if capability == "window.control":
        window = result.get("window") or {}
        title = str(window.get("title") or "").strip()
        action = str(result.get("action") or "").strip()
        if action == "list":
            return f"There are {int(result.get('window_count') or 0)} open windows."
        if title:
            return f"I brought {title} to the front." if action == "focus" else f"I closed {title}."
    if capability == "screen.capture":
        artifact = result.get("artifact") or {}
        path = str(artifact.get("path") or "").strip()
        if path:
            return f"I captured the screen to {path}."
    if capability == "terminal.execute":
        return f"The command finished with exit code {result.get('exit_code')}."
    if capability == "input.control":
        action = str(result.get("action") or "").strip()
        if action:
            return f"I sent {action.replace('_', ' ')} to the focused window."
    return "Done."


class LiveRunners:
    """Carry out an action, a plan, or research on behalf of a chat turn."""

    def __init__(
        self,
        *,
        broker: Any = None,
        connectors: Any = None,
        images: Any = None,
        generate: Callable[[list[dict[str, str]]], str] | None = None,
        task: Any = None,
        capabilities: Sequence[str] = (),
        authority_mode: str = "ask_every_time",
        approve: Callable[[Mapping[str, Any]], bool] | None = None,
        search: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
        read: Callable[[str], Mapping[str, Any]] | None = None,
        memory: Any = None,
        on_step: Callable[[Mapping[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        describe_screenshot: Callable[[str], str] | None = None,
        continue_until_satisfied: bool = False,
        follow_through_steps: int = 10,
    ) -> None:
        self.broker = broker
        self.connectors = connectors
        self.images = images
        self.generate = generate
        self.task = task
        self.capabilities = list(capabilities)
        self.authority_mode = authority_mode
        self.approve = approve
        self.search = search
        self.read = read



        self.memory = memory
        self.on_step = on_step
        self.should_stop = should_stop
        self.describe_screenshot = describe_screenshot



        self.continue_until_satisfied = continue_until_satisfied


        self.follow_through_steps = max(1, int(follow_through_steps))



    def run_action(self, *, decision: Mapping[str, Any], request: str) -> dict[str, Any]:
        """Carry out a single capability call.

        Deliberately not routed through the workflow engine. "Open YouTube" is
        one action; wrapping it in a plan, a verifier and a checkpoint would add
        latency and moving parts to something that has neither dependencies nor
        anything to verify beyond the call's own result.
        """

        from ..automation.invocation import (
            CapabilityCallFailed,
            invoke_capability,
        )
        from ..automation.routing import resolve_execution
        from .agent_loop import AgentLoop

        capability = str(decision.get("capability") or decision.get("tool") or "")
        arguments = dict(decision.get("arguments") or {})

        if capability and capability in self.capabilities and self.broker is not None:
            route = resolve_execution(capability, arguments, self.capabilities)
            if self.task is not None:
                self.task.note_tool_call(route.capability, route.tier_name)
            try:
                result = invoke_capability(
                    self.broker,
                    capability,
                    arguments,
                    authority_mode=self.authority_mode,
                    granted=self.capabilities,
                    repair=self._repair_arguments,
                )
            except CapabilityCallFailed as error:




                return {
                    "answer": (
                        f"I could not carry that out: {error}"
                    ),
                    "capability": route.capability,
                    "tier": route.tier_name,
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                }
            if self.task is not None and getattr(self.task, "world_state", None):
                self.task.world_state.absorb(route.capability, result or {})







            if self.continue_until_satisfied and self.generate is not None:
                return self._continue_from(decision, route, result or {}, request)

            return {
                "answer": (
                    str(decision.get("reason") or "").strip()
                    or _describe_effect(route, result or {})
                ),
                "capability": route.capability,
                "tier": route.tier_name,
                "status": (result or {}).get("status", "succeeded"),
                "result": dict(result or {}),
            }




        if not self.capabilities:
            return {
                "answer": (
                    "Using your computer is Agent work, and Agent mode is off "
                    "for this message. Turn on Agent in the composer and ask "
                    "again, and I will do it."
                ),
                "status": "declined",
                "requires_agent_mode": True,
            }



        if self.generate is None or self.broker is None:
            return {"answer": "That action is not available.", "status": "declined"}
        outcome = AgentLoop(
            broker=self.broker,
            generate=self.generate,
            capabilities=self.capabilities,
            authority_mode=self.authority_mode,
            approve=self.approve,
            task=self.task,
            memory=self.memory,
            on_step=self.on_step,
            should_stop=self.should_stop,
            describe_screenshot=self.describe_screenshot,
            max_iterations=8,
        ).run(request)
        return {
            "answer": str(outcome.get("answer") or ""),
            "status": outcome.get("state"),
            "steps": outcome.get("steps", []),
        }



    def run_plan(self, *, decision: Mapping[str, Any], request: str) -> dict[str, Any]:
        """Validate a model-authored plan, then run it."""

        connectors = (
            [item["connector"] for item in self.connectors.catalogue()]
            if self.connectors is not None
            else []
        )
        try:
            plan = validate_plan(
                decision.get("plan") or decision,
                goal=str(decision.get("goal") or request),
                capabilities=self.capabilities,
                connectors=connectors,
            )
        except PlanRejected as error:

            return {
                "answer": f"I could not build a safe plan for that: {error}",
                "status": "rejected",
                "plan_rejected": str(error),
            }

        executor = Executor(
            broker=self.broker,
            connectors=self.connectors,
            images=self.images,
            task=self.task,
            approve=self.approve,
            authority_mode=self.authority_mode,
        )
        engine = WorkflowEngine(
            executor=executor,
            task=self.task,
            replan=self._replan if self.generate is not None else None,



            validate=lambda payload, plan_goal: validate_plan(
                payload,
                goal=plan_goal,
                capabilities=self.capabilities,
                connectors=connectors,
            ),
        )
        result = engine.run(plan)
        return {
            "answer": result.message,
            "status": result.state,
            "plan": result.plan.to_dict(),
            "replans": result.replans,
            "waiting_for": result.waiting_for,
        }

    def _continue_from(
        self,
        decision: Mapping[str, Any],
        route: Any,
        result: Mapping[str, Any],
        request: str,
    ) -> dict[str, Any]:
        """Carry on from a completed first action until the goal is satisfied.

        The agent loop is the thing that already knows how to observe, decide
        and stop; this only gives it a running start so the work the decision
        layer already did is not repeated. The loop ends the moment the model
        responds, so a genuinely one-step goal costs one extra generation
        rather than a whole re-plan.
        """

        from .agent_loop import AgentLoop

        opening = {
            "completed_step": {
                "action": route.capability,
                "arguments": dict(route.arguments),
                "observation": dict(result),
            },
            "goal": request,
            "note": (
                "This already ran. If the goal is now satisfied, respond. "
                "Otherwise continue from here."
            ),
        }
        outcome = AgentLoop(
            broker=self.broker,
            generate=self.generate,
            capabilities=self.capabilities,
            authority_mode=self.authority_mode,
            approve=self.approve,
            task=self.task,
            memory=self.memory,
            on_step=self.on_step,
            should_stop=self.should_stop,
            describe_screenshot=self.describe_screenshot,
            max_iterations=self.follow_through_steps,
        ).run(request, opening=json.dumps(opening, default=str))
        steps = list(outcome.get("steps") or [])
        return {
            "answer": str(outcome.get("answer") or "")
            or _describe_effect(route, result),
            "capability": route.capability,
            "tier": route.tier_name,
            "status": outcome.get("state") or "succeeded",
            "result": dict(result),
            "steps": steps,
        }

    def _repair_arguments(self, brief: str) -> Mapping[str, Any] | None:
        """One correction, from the model, given the capability's real contract.

        The brief is machine-readable on purpose: what was rejected, why, and
        the exact field names the capability accepts. A model that is told only
        "unknown field" can do nothing but guess again.
        """

        if self.generate is None:
            return None
        reply = self.generate(
            [
                {
                    "role": "system",
                    "content": (
                        "That tool call was rejected. Reply with one JSON "
                        "object holding only the corrected arguments — no "
                        "prose, no capability name, no explanation."
                    ),
                },
                {"role": "user", "content": brief},
            ]
        )
        from .actions import _whole_json_object

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        if not isinstance(parsed, Mapping):
            return None

        inner = parsed.get("arguments")
        if isinstance(inner, Mapping):
            return inner
        return parsed

    def _replan(self, plan: Plan, outcome: Any) -> Mapping[str, Any] | None:
        """Ask Base Steak — the same model, not a planner — to repair a plan."""

        if self.generate is None:
            return None
        if self.task is not None:
            self.task.metrics.planning_model_calls += 1
        world = (
            self.task.world_state.briefing()
            if self.task is not None and getattr(self.task, "world_state", None)
            else {}
        )
        reply = self.generate(
            [
                {"role": "system", "content": "Reply with one JSON object and nothing else."},
                {"role": "user", "content": replan_packet(plan, outcome, world=world)},
            ]
        )
        from .actions import _whole_json_object

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        return parsed if isinstance(parsed, Mapping) else None



    def run_research(self, *, decision: Mapping[str, Any], request: str) -> dict[str, Any]:
        """Gather and compare sources, reasoning only where it is needed."""

        if self.search is None or self.read is None:
            return {
                "answer": "Web research is not available in this build.",
                "status": "declined",
            }

        question = str(decision.get("question") or decision.get("goal") or request)
        budget = Budget(
            max_sources=int(decision.get("max_sources") or 6),
            max_queries=int(decision.get("max_queries") or 3),
        )
        loop = ResearchLoop(
            question,
            search=self.search,
            read=self.read,
            follow_up=self._follow_up,
            budget=budget,
            task=self.task,
        )
        report = loop.run(str(decision.get("query") or question))
        return {
            "answer": self._summarise(report),
            "status": "completed",
            "research": {
                key: report[key]
                for key in (
                    "question",
                    "queries",
                    "source_count",
                    "claim_count",
                    "corroborated",
                    "disputed",
                    "stop_reason",
                )
            },

            "sources": report["sources"],
            "claims": report["claims"][:25],
        }

    def _follow_up(self, ledger: Any) -> str | None:
        """One model call decides whether another query is worth making.

        Not per page. The retrieval and extraction between queries is entirely
        deterministic, which is what keeps a research task at a few reasoning
        turns however many sources it reads.
        """

        if self.generate is None:
            return None
        gaps = [claim.to_dict() for claim in ledger.disputed_claims][:5]
        if not gaps and len(ledger.corroborated_claims) >= 2:
            return None
        if self.task is not None:
            self.task.metrics.planning_model_calls += 1
        reply = self.generate(
            [
                {
                    "role": "system",
                    "content": (
                        "Reply with one JSON object: "
                        '{"query":"..."} for one more search, or {"query":null} '
                        "if the evidence is sufficient."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": ledger.question,
                            "already_searched": ledger.queries,
                            "disagreements": gaps,
                            "settled": len(ledger.corroborated_claims),
                        },
                        default=str,
                    ),
                },
            ]
        )
        from .actions import _whole_json_object

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        query = (parsed or {}).get("query") if isinstance(parsed, Mapping) else None
        return str(query) if query else None

    @staticmethod
    def _summarise(report: Mapping[str, Any]) -> str:
        lines = [
            f"I read {report['source_count']} sources and kept "
            f"{report['claim_count']} distinct findings."
        ]
        disputed = [claim for claim in report["claims"] if claim["disputed"]]
        if disputed:
            lines.append(
                f"{len(disputed)} of them disagree between sources and are worth "
                "checking before relying on."
            )
        lines.append(f"I stopped because: {report['stop_reason'].replace('_', ' ')}.")
        return " ".join(lines)


__all__ = [
    "LiveRunners",
    "MAX_LIVE_NODES",
    "PlanRejected",
    "RUNNER_SCHEMA",
    "replan_packet",
    "validate_plan",
]
