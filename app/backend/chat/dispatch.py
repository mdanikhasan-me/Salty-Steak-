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

from ..automation.capability_registry import get_capability_descriptor
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
object instead: {{"action":"<name below>","reason":"<one sentence>"}}
{jobs}
Judge by intent, not wording: a logo, icon or poster request is an image request.\
"""











AGENT_DECISION_INSTRUCTION = """\
Answer normally. If the request needs more than an answer, reply with ONE JSON \
object instead: {{"action":"<name below>","reason":"<one sentence>"}}
{jobs}
Decide from what THIS message asks for. The actions below are available, which \
is not a reason to use one — a greeting, a question or an explanation is \
answered in words, with no action at all.
When they do want something done on this computer, do it: return the object. \
Explaining how they could do it themselves is not doing it.\
"""

IMAGE_SHAPE = (
    '\ngenerate_image adds: "brief":{"subject","image_type","brand","style",'
    '"deliverables":[],"negative_constraints":[]}'
)









PLAN_SHAPE = (
    '\nplan example: {"action":"plan","nodes":[{"node":"find","connector":'
    '"mail.local","operation":"search","arguments":{}},{"node":"tag","connector":'
    '"mail.local","operation":"apply_label","arguments":{},"depends_on":["find"]}]}'
    "\nUse plan only when every later input binds from earlier output. If a "
    "fresh result needs model judgment, choose action; the Agent loop "
    "continues from it."
    '\nUse {"$ref":"find.output.items[0].id"} with depends_on; never guess '
    "observed values."
)





PLAN_CAPABILITY_SHAPE = (
    '\nAn action step names a capability instead: {"node":"open","capability":'
    '"application.launch","arguments":{"target":"notepad"}}'
)











ACTION_SHAPE = (
    '\naction example: {"action":"action","capability":"<one listed below>",'
    '"arguments":{...}}'
)


def build_turn_instruction(
    *,
    image_available: bool,
    has_previous_image: bool,
    capabilities: Sequence[str] = (),
    connectors: Sequence[str] = (),
    agent_mode: bool = False,
    research_available: bool = False,
) -> str:
    """The system line that lets one generation both answer and route.

    Only what is genuinely reachable is offered. Advertising a capability that
    is switched off invites the model to plan around a door it cannot open.

    ``agent_mode`` does not change which brain decides — the same generation
    still routes the turn. It changes the standing instruction: the user has
    asked for the work to be done, so describing how to do it by hand is the
    wrong answer when a capability could do it.
    """






    offered = [SINGLE_ACTION, PLAN] if capabilities else []





    if research_available:
        offered.append(RESEARCH)
    trailing = ""
    if image_available:
        offered.append(GENERATE_IMAGE)
        if has_previous_image:
            offered.append(REVISE_IMAGE)




            trailing = (
                "\nChoose from what THIS message asks for. An earlier image is "
                "context, not an instruction: a question, a correction or a "
                'change of subject is an answer, not a revision.'
            )
    if not offered:
        return ""

    jobs = "\n".join(f'  "{name}" — {JOB_TYPE_MANIFEST[name]}' for name in offered)





    text = (
        AGENT_DECISION_INSTRUCTION if (agent_mode and capabilities) else DECISION_INSTRUCTION
    ).format(jobs=jobs)
    if SINGLE_ACTION in offered:
        text += ACTION_SHAPE
    if PLAN in offered:
        text += PLAN_SHAPE
        if capabilities:
            text += PLAN_CAPABILITY_SHAPE
    if image_available:
        text += IMAGE_SHAPE + trailing
    if capabilities:
        actions = []
        for name in capabilities:
            try:
                affordance = get_capability_descriptor(name).affordance
            except KeyError:
                actions.append(f"  {name}")
            else:
                actions.append(f"  {name} — {affordance}")
        text += "\nActions:\n" + "\n".join(actions)
    if connectors:
        text += "\nServices: " + ", ".join(connectors)
    if agent_mode and capabilities:





        text += (





            "\nNot knowing the user's mind and not yet knowing what is on the "
            "computer are different problems. If you do not know what they "
            "want, ask. If you know what they want but not yet which things it "
            "applies to, LOOK: use a read-only action — list, search, read, "
            "inspect — inside the place they named, then act on what you "
            "found. Do not ask a person for something you can see for "
            "yourself."
            "\nA goal they describe is already authorised, so do not ask again "
            "before carrying it out. Stop and describe instead only "
            "when doing it could reach beyond what they asked for — an "
            "unbounded target, or a rule that could take things they plainly "
            "want kept. If no action above can achieve the goal, say so "
            "plainly instead of trying the nearest one."
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


        self.goal_spec: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DISPATCH_SCHEMA,
            "kind": self.kind,
            "artifact": self.artifact,
            **self.details,
        }


def looks_like_a_decision_attempt(reply: str) -> bool:
    """True when a reply is machine output rather than an answer.

    Two shapes reach here. A decision object that does not parse — a missing
    brace part-way through a plan is the one seen live. And a reply that is a
    perfectly well-formed JSON document which is not a decision at all: asked
    for SSD prices in Bangladesh, the live model answered with
    ``{"research": {"subject": ..., "sources_analyzed": [...]}}`` and the raw
    braces were printed at the user for eighty lines.

    Whatever its keys, a whole JSON document is not an answer to a person, so
    the test is the shape rather than the vocabulary. Prose that merely
    contains a brace is untouched, because the document has to be the entire
    reply.
    """

    text = strip_reasoning(str(reply or "")).strip()
    if not text.startswith(("{", "[")):
        return False
    if read_decision(reply) is not None:
        return False
    try:
        json.loads(text)
    except (TypeError, ValueError):



        return '"action"' in text or '"nodes"' in text or '"capability"' in text
    return True












EFFECT_CLAIM_INSTRUCTION = (
    "You are checking one reply for a specific mistake, and nothing else.\n"
    "Nothing was done to this computer on this turn. No command ran, no file "
    "changed, no application opened, no page was visited.\n"
    "Question: does the reply below tell the user that something WAS done — "
    "that files were deleted, an application was opened, a page was visited, a "
    "command was run, a setting was changed?\n"
    'Answer with one word: "claimed" if it says or implies the work was '
    'carried out, or "honest" if it only explains, describes, answers a '
    "question, or says what would be done.\n"
    "One word. Nothing else."
)






















ROUTE_NECESSITY_INSTRUCTION = (
    "You are sorting one message into one of two kinds, and doing nothing "
    "else.\n"
    "The message was sent to an assistant that can also operate the computer, "
    "search the web and draw pictures.\n"
    '"task" — they want something done, found out, opened, run, changed, '
    "looked up or made.\n"
    '"conversation" — a greeting, a thank-you, small talk, or a question that '
    "words alone answer.\n"
    "Reply with one word: task or conversation."
)



ROUTE_DESCRIPTIONS = {
    SINGLE_ACTION: "operate this computer",
    PLAN: "carry out several steps on this computer",
    RESEARCH: "search the web and read several pages",
    GENERATE_IMAGE: "generate a picture",
    REVISE_IMAGE: "redraw the picture from earlier in the conversation",
}









GOAL_SPEC_INSTRUCTION = (
    "Compile the request into structured operational state: what must be TRUE "
    "when it finishes, not private reasoning and not a list of steps.\n"
    "Reply with ONE JSON object and nothing else:\n"
    '{"objective":"...","current_turn_intent":"...","constraints":[],'
    '"protected_resources":[],"permission_scope":"runtime supplied",'
    '"required_outcomes":[{"kind":"...","subject":"...","detail":""}],'
    '"unknowns":[],"future_dependencies":[],"stopping_conditions":[]}\n'
    "Observable outcome kinds: present, absent, window_present, window_absent, "
    "window_focused, active_url, browser_visible, media_playing, artifact_valid, "
    "exact_page, stock_confirmed. Use only kinds needed by this request.\n"
    "present/absent subjects are exact absolute paths or an absolute path with "
    "a wildcard. Use **\\*.ext when the request explicitly includes nested "
    "folders. When a named protected file is relative to an absolute folder in "
    "the request, combine them into its exact absolute path. artifact_valid uses "
    "$artifact when the path will only exist at "
    "runtime. active_url uses an exact URL the user supplied, or $selected_url "
    "when an earlier observation must select it. browser_visible subject is true. "
    "media_playing uses $active_media unless the user named a specific item.\n"
    "When the user asks to watch or see media, require both browser_visible and "
    "media_playing; playback in an off-screen surface does not satisfy the goal.\n"
    "List every resource they said to keep, leave alone, preserve, or not touch. "
    "Do not guess runtime identifiers, paths, URLs, names, or state. Record such "
    "facts in unknowns/future_dependencies and bind them from observations later. "
    "Invent nothing; use empty lists when the request says nothing about a field."
)

GOAL_SPEC_REPAIR_INSTRUCTION = (
    "Your previous objective object could not be used: {reason}. Return one "
    "corrected JSON object now, with at least one item in required_outcomes "
    "for every externally checkable result and one present outcome for each "
    "absolute protected path. Use the schema and predicate kinds already given. "
    "JSON strings must escape every Windows backslash as \\\\. No prose or fence."
)

DECISION_REPAIR_INSTRUCTION = (
    "Your previous reply was JSON. The person reading it wants an answer in "
    "plain words, not a data structure. Answer their question directly, in "
    "prose — no JSON, no code fence, no field names. Only if you meant to use "
    "a tool, send the corrected decision object and nothing else; a plan looks "
    'like {"action":"plan","nodes":[{"node":"a","capability":"...",'
    '"arguments":{}}]}.'
)


def _plan_without_its_envelope(text: str) -> dict[str, Any] | None:
    """A bare list of plan nodes, put back inside the object it belongs in.

    Asked to open Notepad, focus it and capture the screen, the live model
    produced exactly the right three nodes as a top-level JSON array and left
    off the ``{"action":"plan", ...}`` wrapper. That array is valid JSON but
    not an object, so nothing recognised it as a decision and the raw brackets
    were printed to the user as the assistant's answer.

    A list of node-shaped mappings can only have been a plan — ordinary prose
    is not a JSON array — so it is restored rather than shown. Anything else
    is left alone and still reads as an answer.
    """

    stripped = str(text or "").strip()
    if not stripped.startswith("["):
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    for entry in parsed:
        if not isinstance(entry, Mapping):
            return None
        if not any(key in entry for key in ("node", "capability", "connector", "id")):
            return None
    return {"action": "plan", "nodes": [dict(entry) for entry in parsed]}


def read_decision(reply: str) -> dict[str, Any] | None:
    """Find a routing decision in a reply, or conclude there isn't one.

    A reply that is simply an answer is the common case and must not be
    treated as a parse failure. Only a whole JSON object naming a known job
    type counts, so prose that happens to contain braces stays prose.
    """

    text = strip_reasoning(reply)
    bare = _plan_without_its_envelope(text)
    if bare is not None:
        return bare
    if not text.startswith("{") and "```" not in str(reply or ""):
        return None
    try:
        decision = parse_decision(reply)
    except OrchestrationError:
        return None
    if not isinstance(decision, Mapping):
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
        permitted: Sequence[str] | frozenset[str] | None = None,
    ) -> None:
        self.images = images
        self.image_store = image_store
        self.run_agent = run_agent
        self.run_workflow = run_workflow
        self.run_research = run_research
        self.task = task


        self.permitted = frozenset(permitted) if permitted is not None else None

    def dispatch(
        self,
        decision: Mapping[str, Any] | None,
        *,
        reply_text: str,
        request: str,
        conversation_id: str,
        message_id: str = "",
        latest_request: str = "",
    ) -> TurnOutcome:
        """Carry out whatever the model decided.

        ``request`` is the recent thread, so a follow-up still carries the task
        it refers to. ``latest_request`` is only what the user just said, which
        is what a revision is a revision *of* — the two are different, and a
        revision handed the whole thread wrote "this revision changes: create a
        minimal professional logo…" into a brief that already said that.
        """

        if decision is None:

            return TurnOutcome(RESPOND, content=reply_text)

        action = str(decision.get("action"))







        if self.permitted is not None and action not in self.permitted:
            return TurnOutcome(
                RESPOND,
                content=reply_text,
                details={"refused_by_authority": action},
            )

        if action == RESPOND:
            return TurnOutcome(RESPOND, content=str(decision.get("answer") or reply_text))

        if action in {GENERATE_IMAGE, REVISE_IMAGE}:
            return self._image(
                decision,
                action=action,
                request=request,
                latest_request=latest_request or request,
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
        latest_request: str = "",
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
                    feedback=latest_request or request,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    parent_job_id=parent.job_id if parent else None,
                )
            else:
                job = self.images.prepare(
                    decision,
                    original_request=request,
                    latest_request=latest_request or request,
                    notes=str(decision.get("model_notes") or ""),
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
                "render_negative": job.brief.render_negative(),
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
    "ROUTE_DESCRIPTIONS",
    "ROUTE_NECESSITY_INSTRUCTION",
    "TurnDispatcher",
    "TurnOutcome",
    "build_turn_instruction",
    "read_decision",
]
