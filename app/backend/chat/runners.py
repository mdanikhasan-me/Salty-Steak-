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
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..automation.capability_registry import FILE_OPERATIONS, FILES_CAPABILITY
from ..automation.credentials import redact
from ..automation.routing import apply_argument_aliases

from ..research import Budget, ResearchLoop
from ..workflow import Executor, WorkflowEngine, build_plan
from ..workflow.plan import Plan, PlanError
from .orchestrator import strip_reasoning

RUNNER_SCHEMA = "salty-steak-live-runner-v1"



MAX_LIVE_NODES = 40
MAX_REPLAN_OBSERVATIONS = 12
MAX_REPLAN_ITEMS = 20
MAX_REPLAN_FIELDS = 30
MAX_REPLAN_TEXT = 800
MAX_REPLAN_DEPTH = 5


class PlanRejected(ValueError):
    """Raised when a model-produced plan cannot be trusted to run."""


def _nodes_in_the_right_slots(
    payload: Mapping[str, Any],
    capabilities: Sequence[str],
    connectors: Sequence[str] = (),
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
    configured = set(connectors)
    nodes = payload.get("nodes")
    if not granted or not isinstance(nodes, list):
        return payload




    CANDIDATE_FIELDS = ("connector", "node", "id", "name", "tool", "action", "target")

    moved: list[Any] = []
    for entry in nodes:
        if not isinstance(entry, Mapping):
            moved.append(entry)
            continue





        if str(entry.get("capability") or "") in granted:
            moved.append(entry)
            continue

        corrected = dict(entry)
        corrected.pop("capability", None)


        if str(corrected.get("connector") or "") in granted:
            corrected["capability"] = corrected.pop("connector")
            corrected.pop("operation", None)
            moved.append(corrected)
            continue









        if str(corrected.get("connector") or "") not in configured:
            found = next(
                (
                    str(corrected[field])
                    for field in CANDIDATE_FIELDS
                    if str(corrected.get(field) or "") in granted
                ),
                "",
            )
            if found:
                corrected["capability"] = found
                corrected.pop("connector", None)
                corrected.pop("operation", None)
                moved.append(corrected)
                continue





        if (
            FILES_CAPABILITY in granted
            and str(corrected.get("connector") or "") not in configured
        ):
            arguments, _notes = apply_argument_aliases(
                FILES_CAPABILITY, dict(corrected.get("arguments") or {})
            )
            if _looks_like_a_path(arguments.get("path")):
                operation = str(
                    arguments.get("operation") or corrected.get("operation") or ""
                ).strip().casefold()
                if operation in FILE_OPERATIONS:
                    arguments["operation"] = operation
                    corrected["capability"] = FILES_CAPABILITY
                    corrected["arguments"] = arguments
                    corrected.pop("connector", None)
                    corrected.pop("operation", None)
                    moved.append(corrected)
                    continue











        if FILES_CAPABILITY in granted and _looks_like_a_path(
            corrected.get("connector")
        ):
            arguments = dict(corrected.get("arguments") or {})
            arguments.setdefault("path", str(corrected["connector"]))
            operation = str(corrected.get("operation") or "").strip().casefold()
            if operation in FILE_OPERATIONS:
                arguments.setdefault("operation", operation)
            corrected["capability"] = FILES_CAPABILITY
            corrected["arguments"] = arguments
            corrected.pop("connector", None)
            corrected.pop("operation", None)
            moved.append(corrected)
            continue

        moved.append(entry)
    return {**payload, "nodes": moved}


def _looks_like_a_path(value: Any) -> bool:
    """Whether a value is a place on this computer rather than a service name.

    Structural, not a list of drive letters: a service name has no separators
    and no drive, and a path has one or the other.
    """

    text = str(value or "").strip()
    if not text or "://" in text:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return text.startswith(("\\\\", "/", "~")) or ("\\" in text and " " not in text[:3])


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
            _nodes_in_the_right_slots(payload, capabilities, connectors), goal=goal
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


def _bounded_replan_value(value: Any, *, depth: int = 0) -> Any:
    value = redact(value)
    if depth >= MAX_REPLAN_DEPTH:
        return "[nested value omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_replan_value(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_REPLAN_FIELDS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_replan_value(item, depth=depth + 1)
            for item in list(value)[:MAX_REPLAN_ITEMS]
        ]
    if isinstance(value, str):
        return value[:MAX_REPLAN_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_REPLAN_TEXT]


def replan_packet(
    plan: Plan, outcome: Any, *, world: Mapping[str, Any] | None = None
) -> str:
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



        "observations": {
            node_id: _bounded_replan_value(value)
            for node_id, value in list(plan.observations.items())[
                -MAX_REPLAN_OBSERVATIONS:
            ]
        },
        "known": dict(world or {}),
    }
    return (
        "A step failed and retrying it will not help. Produce a corrected plan "
        "as one JSON object with the same shape. Keep the completed steps as "
        "they are and change only what is left. Use {$ref: "
        '"node.output.field"} for values supplied by existing observations; '
        "do not copy or guess them.\n" + json.dumps(packet, default=str)
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






_PROCESS_PHRASES = (
    "i read ",
    "i searched",
    "distinct findings",
    "i stopped because",
    "source budget",
)




FINALISER_BASE_RULES = (
    "Answer the user's question using only the findings below. Write the "
    "answer itself in plain prose — do not describe the search, do not count "
    "sources, do not mention findings or claims. "
    "Never combine parts of two different findings: if one finding names a "
    "thing and another names a number, they are not about each other unless a "
    "single finding says so. "
    "Do not estimate, round or fill a gap. If the findings do not give "
    "something that was asked for, say that they do not. "
    "If the findings disagree, say what the disagreement is. If they do not "
    "answer the question, say so plainly."
)






FINALISER_COMMERCE_RULES = (
    " A figure a finding gives as a range across a page or a catalogue is a "
    "range and must be reported as one, never as the price of a particular "
    "item. "
    "A price for a named item may only come from verified_products, which is "
    "the only place a product, its price and its seller were bound together "
    "by one page. Quote each seller's price separately and give its link; "
    "never average them. "
    "A finding marked as a catalogue page describes a whole shop, not any item "
    "on it. "
    "Write each price exactly as verified_products gives it, character for "
    "character. Never change its currency mark for another and never convert "
    "between currencies. "
    "Stock is evidence, not an assumption. A product whose stock is 'unknown' "
    "has NOT been confirmed available and may never be called in stock, "
    "available, or the cheapest one currently in stock — say its availability "
    "was not confirmed. "
    "Obey evidence_limits."
)


def _has_priced_evidence(observations: Sequence[Mapping[str, Any]]) -> bool:
    """Whether any observation actually bound an offer.

    A page can be observed without a price being established on it, and an
    observation with no price is not commerce evidence.
    """

    return any(
        str(item.get("price_display") or item.get("price") or "").strip()
        for item in observations
        if isinstance(item, Mapping)
    )


def finaliser_instruction(*, observations: Sequence[Mapping[str, Any]]) -> str:
    """The rules the answering pass is held to, for the evidence it actually has.

    Evidence-driven, never keyword-driven. The commerce block appears because
    priced observations exist, not because the question contained a word — a
    word list would be wrong about somebody's question sooner or later, and
    would reintroduce exactly the contamination it was meant to prevent.
    """

    if _has_priced_evidence(observations):
        return FINALISER_BASE_RULES + FINALISER_COMMERCE_RULES
    return FINALISER_BASE_RULES


def finaliser_payload(
    *,
    question: str,
    observations: Sequence[Mapping[str, Any]],
    evidence_limits: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What the answering pass is shown.

    An empty ``verified_products`` list is not neutral: a shopping-shaped field
    still tells the model the answer is about shopping, so it is left out
    entirely rather than sent empty.
    """

    payload: dict[str, Any] = {"question": question, "findings": list(findings)}
    if not _has_priced_evidence(observations):
        return payload
    payload["verified_products"] = [
        {
            "product": item.get("product"),


            "price": item.get("price_display") or item.get("price"),
            "currency": item.get("currency_code") or item.get("currency"),
            "seller": item.get("seller"),
            "stock": item.get("stock") or "unknown",
            "variant": item.get("variant") or "",
            "url": item.get("url"),
        }
        for item in observations
    ]
    payload["evidence_limits"] = dict(evidence_limits)
    return payload


def _evidence_limits(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the gathered evidence settles, and what it does not.

    Availability is its own observation and "unknown" is a real answer.
    Treating it as good as "in stock" is how a question about what can be
    bought today gets answered with something nobody established was buyable.
    """

    confirmed, unconfirmed, unavailable = [], [], []
    for item in observations:
        stock = str(item.get("stock") or "unknown")
        name = str(item.get("product") or "")[:120]
        if stock == "in_stock":
            confirmed.append(name)
        elif stock in {"out_of_stock", "preorder"}:
            unavailable.append(name)
        else:
            unconfirmed.append(name)
    return {
        "in_stock_confirmed": confirmed,
        "stock_not_confirmed": unconfirmed,
        "not_available": unavailable,


        "note": (
            "Availability was confirmed for none of these, so none of them can "
            "be called the cheapest one currently in stock."
            if not confirmed
            else "Only the items listed under in_stock_confirmed may be "
            "described as currently in stock."
        ),
    }


def _reads_like_process(answer: str) -> bool:
    head = answer.strip().lower()[:200]
    return any(phrase in head for phrase in _PROCESS_PHRASES)


def _plain_failure(node: Any) -> str:
    """A node's failure without the exception plumbing wrapped around it.

    Failures arrive as "CapabilityCallFailed: UI Automation command must be one
    of: collapse, expand, ..." — a class name, a colon, and a schema dump. The
    class name means nothing to the reader and the list is not an explanation,
    so the sentence keeps the human half and drops the rest.
    """

    text = str(getattr(node, "failure", "") or "").strip()

    while True:
        head, separator, tail = text.partition(": ")
        if not separator or " " in head or not head or not head[0].isupper():
            break
        text = tail.strip()
    listing = text.find(" must be one of:")
    if listing != -1:
        text = text[: listing + len(" must be one of")].replace(
            " must be one of", " was not one this capability accepts"
        )
    return text or "the step did not succeed"


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
        on_research: Callable[[Mapping[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        describe_screenshot: Callable[[str], str] | None = None,
        continue_until_satisfied: bool = False,
        follow_through_steps: int = 10,
        established: str = "",
        checkpoint_path: str | Path | None = None,
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

        self.on_research = on_research
        self.should_stop = should_stop
        self.describe_screenshot = describe_screenshot



        self.continue_until_satisfied = continue_until_satisfied


        self.follow_through_steps = max(1, int(follow_through_steps))




        self.established = str(established or "")
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None



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
            established=self.established,
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
            granted=self.capabilities,
            repair=self._repair_arguments if self.generate is not None else None,
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
            checkpoint_path=self.checkpoint_path,
        )
        result = engine.run(plan)
        return {
            "answer": self._plan_answer(result, request),
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
            established=self.established,
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

    def _plan_answer(self, result: Any, request: str) -> str:
        """What to tell the user once a plan has finished running.

        The engine answers "Done." for a completed plan, which is true and
        useless: asked to open Notepad, bring it forward and say what was on
        the screen, the three steps ran and the reply was the single word
        "Done." The steps each reported what they actually did, so the report
        is built from those observations — the same evidence the audit records
        keep — rather than from the request.
        """

        nodes = list(getattr(getattr(result, "plan", None), "nodes", []) or [])
        sentences: list[str] = []
        for node in nodes:
            if node.state != "completed" or not node.capability:
                continue
            route = SimpleNamespace(capability=node.capability)
            sentences.append(_describe_effect(route, node.result or {}))
        spoken = [sentence for sentence in sentences if sentence != "Done."]

        if str(getattr(result, "state", "")) == "completed":
            if not spoken:
                return str(getattr(result, "message", "") or "Done.")
            return " ".join(spoken)






        failed = next((node for node in nodes if node.state == "failed"), None)
        if failed is None:
            return str(getattr(result, "message", "") or "")
        parts = list(spoken)
        parts.append(f"I could not finish the last step: {_plain_failure(failed)}")
        return " ".join(parts)

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
            on_progress=self.on_research,
        )
        report = loop.run(str(decision.get("query") or question))
        return {
            "answer": self._answer_from(question, report),
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
            }


            | {"process_summary": self._summarise(report)},

            "sources": report["sources"],
            "claims": report["claims"][:25],



            "observations": report.get("observations") or [],




            "evidence_limits": _evidence_limits(report.get("observations") or []),
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

    def _answer_from(self, question: str, report: Mapping[str, Any]) -> str:
        """Answer the question from the evidence that was gathered.

        The research runner used to return its own execution statistics as the
        assistant's reply: "I read 6 sources and kept 145 distinct findings. I
        stopped because: source budget." Asked for the tallest building in the
        world, the user was told how the search went and never told the answer.
        Retrieval and comparison are the work; the answer is the product, and
        the statistics belong in the provenance beside it.
        """

        claims = list(report.get("claims") or [])
        if not claims:
            return (
                "I could not find enough to answer that. "
                + self._summarise(report)
            )



        ordered = sorted(
            claims,
            key=lambda claim: (
                bool(claim.get("disputed")),
                -int(claim.get("source_count") or 0),
            ),
        )[:24]

        if self.generate is not None:
            if self.task is not None:
                self.task.metrics.model_calls += 1




            observations = list(report.get("observations") or [])
            reply = self.generate(
                [
                    {
                        "role": "system",
                        "content": finaliser_instruction(observations=observations),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            finaliser_payload(
                                question=question,
                                observations=observations,
                                evidence_limits=_evidence_limits(observations),
                                findings=[
                                    {
                                        "text": claim.get("text"),
                                        "sources": int(claim.get("source_count") or 0),
                                        "disputed": bool(claim.get("disputed")),
                                    }
                                    for claim in ordered
                                ],
                            ),
                            default=str,
                        ),
                    },
                ]
            )
            answer = strip_reasoning(str(reply or "")).strip()
            if answer and not _reads_like_process(answer):
                return answer



        return "\n".join(
            f"- {claim.get('text')}"
            for claim in ordered[:6]
            if str(claim.get("text") or "").strip()
        ) or self._summarise(report)

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
    "FINALISER_BASE_RULES",
    "FINALISER_COMMERCE_RULES",
    "LiveRunners",
    "MAX_LIVE_NODES",
    "PlanRejected",
    "RUNNER_SCHEMA",
    "finaliser_instruction",
    "finaliser_payload",
    "replan_packet",
    "validate_plan",
]
