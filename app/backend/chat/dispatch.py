"""Salty Steak Native Desktop AI Platform — the live turn dispatcher.

Where a chat message becomes whichever kind of work it actually is.

One model pass decides. The instruction says: answer normally unless this
request needs something more, in which case return a decision object. A plain
question therefore costs exactly one generation, the same as it always did,
while a request that needs an image or a plan announces itself in the same
breath. A second "what kind of request is this?" round trip would double the
latency of every ordinary chat turn to serve the few that need routing.

Nothing decides ahead of the model. The regex that used to sit in front of it
could not tell that "a distinctive premium brand logo and icon" was an image
request, because the word image never appears in it — and no list of nouns
ever will cover what people actually type.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .orchestrator import (
    GENERATE_IMAGE,
    JOB_TYPE_MANIFEST,
    PLAN,
    RESEARCH,
    RESPOND,
    REVISE_IMAGE,
    SINGLE_ACTION,
    OrchestrationError,
    parse_decision,
    strip_reasoning,
)

DISPATCH_SCHEMA = "salty-steak-turn-dispatch-v1"








DECISION_INSTRUCTION = """\
Answer normally. If the request needs more than an answer, reply with ONE JSON \
object instead:
{jobs}
Judge by intent, not wording: a logo, icon or poster request is an image request.\
"""

IMAGE_SHAPE = (
    '\ngenerate_image adds: "brief":{"subject","image_type","brand","style",'
    '"deliverables":[],"negative_constraints":[]}'
)









PLAN_SHAPE = (
    '\nplan example: {"action":"plan","nodes":[{"node":"find","connector":'
    '"mail.local","operation":"search","arguments":{}},{"node":"tag","connector":'
    '"mail.local","operation":"apply_label","arguments":{},"depends_on":["find"]}]}'
    "\nUse plan for anything needing more than one step; action is a single step."
)


def build_turn_instruction(
    *,
    image_available: bool,
    has_previous_image: bool,
    capabilities: Sequence[str] = (),
    connectors: Sequence[str] = (),
    agent_mode: bool = False,
) -> str:
    """The system line that lets one generation both answer and route.

    Only what is genuinely reachable is offered. Advertising a capability that
    is switched off invites the model to plan around a door it cannot open.

    ``agent_mode`` does not change which brain decides — the same generation
    still routes the turn. It changes the standing instruction: the user has
    asked for the work to be done, so describing how to do it by hand is the
    wrong answer when a capability could do it.
    """

    offered = [SINGLE_ACTION, PLAN, RESEARCH] if capabilities else []
    if image_available:
        offered.append(GENERATE_IMAGE)
        if has_previous_image:
            offered.append(REVISE_IMAGE)
    if not offered:
        return ""

    jobs = "\n".join(f'  "{name}" — {JOB_TYPE_MANIFEST[name]}' for name in offered)
    text = DECISION_INSTRUCTION.format(jobs=jobs)
    if PLAN in offered:
        text += PLAN_SHAPE
    if image_available:
        text += IMAGE_SHAPE
    if capabilities:
        text += "\nActions: " + ", ".join(capabilities)
    if connectors:
        text += "\nServices: " + ", ".join(connectors)
    if agent_mode and capabilities:
        text += (
            "\nAgent mode is on: the user asked you to do this on their computer. "
            "If an action above can do it, return the JSON object and do it. Do "
            "not reply with instructions for doing it by hand."
        )
    return text


class TurnOutcome:
    """What a dispatched turn produced."""

    def __init__(
        self,
        kind: str,
        *,
        content: str = "",
        details: Mapping[str, Any] | None = None,
        artifact: str | None = None,
    ) -> None:
        self.kind = kind
        self.content = content
        self.details = dict(details or {})
        self.artifact = artifact

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DISPATCH_SCHEMA,
            "kind": self.kind,
            "artifact": self.artifact,
            **self.details,
        }


def read_decision(reply: str) -> dict[str, Any] | None:
    """Find a routing decision in a reply, or conclude there isn't one.

    A reply that is simply an answer is the common case and must not be
    treated as a parse failure. Only a whole JSON object naming a known job
    type counts, so prose that happens to contain braces stays prose.
    """

    text = strip_reasoning(reply)
    if not text.startswith("{") and "```" not in str(reply or ""):
        return None
    try:
        decision = parse_decision(reply)
    except OrchestrationError:
        return None
    if decision.get("action") == RESPOND and not decision.get("answer"):

        return None
    return decision


class TurnDispatcher:
    """Turn one model reply into the work it asked for."""

    def __init__(
        self,
        *,
        images: Any = None,
        image_store: Any = None,
        run_agent: Callable[..., Mapping[str, Any]] | None = None,
        run_workflow: Callable[..., Mapping[str, Any]] | None = None,
        run_research: Callable[..., Mapping[str, Any]] | None = None,
        task: Any = None,
    ) -> None:
        self.images = images
        self.image_store = image_store
        self.run_agent = run_agent
        self.run_workflow = run_workflow
        self.run_research = run_research
        self.task = task

    def dispatch(
        self,
        decision: Mapping[str, Any] | None,
        *,
        reply_text: str,
        request: str,
        conversation_id: str,
        message_id: str = "",
    ) -> TurnOutcome:
        """Carry out whatever the model decided."""

        if decision is None:

            return TurnOutcome(RESPOND, content=reply_text)

        action = str(decision.get("action"))

        if action == RESPOND:
            return TurnOutcome(RESPOND, content=str(decision.get("answer") or reply_text))

        if action in {GENERATE_IMAGE, REVISE_IMAGE}:
            return self._image(
                decision,
                action=action,
                request=request,
                conversation_id=conversation_id,
                message_id=message_id,
            )

        if action in {SINGLE_ACTION, PLAN, RESEARCH}:
            runner = {
                SINGLE_ACTION: self.run_agent,
                PLAN: self.run_workflow,
                RESEARCH: self.run_research,
            }[action]
            if runner is None:


                return TurnOutcome(
                    RESPOND,
                    content=(
                        "That needs computer control, which is not enabled for "
                        "this conversation."
                    ),
                    details={"declined": action},
                )
            result = dict(runner(decision=dict(decision), request=request) or {})
            return TurnOutcome(
                action, content=str(result.get("answer") or ""), details=result
            )

        return TurnOutcome(RESPOND, content=reply_text)



    def _image(
        self,
        decision: Mapping[str, Any],
        *,
        action: str,
        request: str,
        conversation_id: str,
        message_id: str,
    ) -> TurnOutcome:
        if self.images is None:
            return TurnOutcome(
                RESPOND,
                content="Image generation is not available in this build.",
                details={"declined": action},
            )

        parent = None
        if action == REVISE_IMAGE and self.image_store is not None:


            parent = self.image_store.latest_for_conversation(conversation_id)
            if parent is not None:
                self.images.registry.add(parent)

        from ..imaging import ImageOrchestrationError

        try:
            if action == REVISE_IMAGE:
                job = self.images.prepare_revision(
                    decision,
                    feedback=request,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    parent_job_id=parent.job_id if parent else None,
                )
            else:
                job = self.images.prepare(
                    decision,
                    original_request=request,
                    conversation_id=conversation_id,
                    message_id=message_id,
                )
        except ImageOrchestrationError as error:
            return TurnOutcome(
                action,
                content=(
                    "I could not turn that into a reliable image request: "
                    f"{error}"
                ),
                details={"image_error": str(error), "kind_of_failure": error.kind},
            )

        job.conversation_id = conversation_id
        self._persist(job)





        return TurnOutcome(
            action,
            content=self._describe(job),
            details={
                "image_job": job.to_dict(),
                "render_brief": job.brief.render(),
                "job_id": job.job_id,
                "revision": job.revision,
                "parent_job_id": job.parent_job_id,
            },
        )

    def _persist(self, job: Any) -> None:
        if self.image_store is None or not job.conversation_id:
            return
        try:
            self.image_store.save(job)
        except Exception:


            return

    @staticmethod
    def _describe(job: Any) -> str:
        what = job.brief.brand or job.brief.subject
        kind = job.brief.image_type.replace("_", " ")
        if job.revision > 1:
            return (
                f"Revision {job.revision} of the {kind} for {what} is ready for "
                "review, keeping the original requirements."
            )
        return f"A {kind} request for {what} is ready for review."


__all__ = [
    "DISPATCH_SCHEMA",
    "TurnDispatcher",
    "TurnOutcome",
    "build_turn_instruction",
    "read_decision",
]
