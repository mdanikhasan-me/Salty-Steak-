"""Salty Steak Native Desktop AI Platform — the single text-model orchestrator.

One brain, several specialised hands.

The text model decides what a request needs: an answer, one action, a plan, an
image, a revision of an image. It does not render pictures, drive browsers or
run workflows — those are engines it dispatches to. The point of putting the
decision in one place is that a capability cannot be reachable from chat and
invisible to a workflow, or available in one reasoning mode and missing from
another.

The routing is semantic, made by the model. There is deliberately no keyword
gate in front of it: a request for "a distinctive premium brand logo and icon"
is an image request even though it never says the word image, and any regex
that decides otherwise is a regex that will be wrong about somebody's request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

ORCHESTRATOR_SCHEMA = "salty-steak-orchestration-v1"



RESPOND = "respond"
SINGLE_ACTION = "action"
PLAN = "plan"
GENERATE_IMAGE = "generate_image"
REVISE_IMAGE = "revise_image"
RESEARCH = "research"

JOB_TYPES = (RESPOND, SINGLE_ACTION, PLAN, GENERATE_IMAGE, REVISE_IMAGE, RESEARCH)




def _capability_actions() -> frozenset[str]:
    """Every capability name the model may write where a job type belongs.

    Read from the broker rather than listed here. This was a literal, and it
    silently went stale the moment a capability was added: asked to delete some
    files the model replied
    ``{"action":"files.manage","capability":"select_and_delete",...}`` — the
    right capability, named in the slot this code explicitly supports — and
    because "files.manage" was not in the literal it fell through to respond
    and the user was told nothing had happened.
    """

    try:
        from ..automation.broker import CAPABILITIES

        return frozenset(CAPABILITIES) | {"image.generate"}
    except Exception:


        return frozenset({"image.generate"})


CAPABILITY_ACTIONS = _capability_actions()



JOB_TYPE_MANIFEST = {
    RESPOND: "answer or explain something; no tools needed",
    SINGLE_ACTION: "carry out one computer or service action",
    PLAN: "several dependent steps that need ordering and verification",
    GENERATE_IMAGE: "create a new visual from a described brief",
    REVISE_IMAGE: "change an image already produced in this conversation",
    RESEARCH: "gather and compare information from several sources",
}


class OrchestrationError(ValueError):
    """Raised when the model's decision cannot be understood or carried out."""


def build_decision_prompt(
    *,
    capabilities: Sequence[str] = (),
    image_available: bool = False,
    has_previous_image: bool = False,
) -> str:
    """The instruction that tells the model what it may decide.

    Only what is actually available is described. Offering a capability that
    is switched off invites the model to plan around a door it cannot open.
    """

    offered = [RESPOND, SINGLE_ACTION, PLAN, RESEARCH]
    if image_available:
        offered.append(GENERATE_IMAGE)
        if has_previous_image:
            offered.append(REVISE_IMAGE)

    lines = [
        "Decide what this request needs. Reply with one JSON object and nothing else.",
        "",
        "Choices:",
    ]
    lines.extend(f'  "{name}" — {JOB_TYPE_MANIFEST[name]}' for name in offered)
    lines.append("")
    lines.append('Shape: {"action":"<choice>","reason":"<one sentence>",...}')
    if image_available:
        lines.extend(
            [
                "",
                "For generate_image add a brief object describing what to draw:",
                '  {"action":"generate_image","reason":"...","brief":{',
                '    "subject":"...","image_type":"logo|icon|illustration|photograph|'
                'diagram|poster|other",',
                '    "goal":"...","brand":"...","style":"...","composition":"...",',
                '    "colour":"...","background":"...","text_content":"...",',
                '    "deliverables":["..."],"required_elements":["..."],',
                '    "negative_constraints":["..."]}}',
                "Judge by what the user wants, not by whether they used the word "
                "image. A request for a logo, icon, poster or illustration is an "
                "image request.",
            ]
        )
        if has_previous_image:
            lines.append(
                'For revise_image: {"action":"revise_image","reason":"...",'
                '"changes":{...}} — describe only what changes. Everything else '
                "from the earlier image is kept automatically."
            )
    if capabilities:
        lines.append("")
        lines.append("Available actions: " + ", ".join(capabilities))
    return "\n".join(lines)




ACTION_ALIASES = {
    "image": GENERATE_IMAGE,
    "image.generate": GENERATE_IMAGE,
    "generate": GENERATE_IMAGE,
    "generateimage": GENERATE_IMAGE,
    "generate_image": GENERATE_IMAGE,
    "revise": REVISE_IMAGE,
    "revise_image": REVISE_IMAGE,
    "answer": RESPOND,
    "reply": RESPOND,
    "respond": RESPOND,
    "workflow": PLAN,
    "plan": PLAN,
    "action": SINGLE_ACTION,
    "research": RESEARCH,
}




_BARE_ARGUMENT_FIELD = {
    RESEARCH: "question",
    GENERATE_IMAGE: "subject",
    REVISE_IMAGE: "changes",
    SINGLE_ACTION: "target",
}


def _declined(value: Any) -> bool:
    """Whether a job flag reads as "not this one"."""

    if value is None or value is False:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"", "false", "no", "none", "0"}
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    if isinstance(value, (int, float)):
        return not value
    return False


def _decision_from_job_keys(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Read a decision written as job names rather than as an action value.

    Observed live on "Generate an image of a cow.":
    ``{"research": false, "generate_image": true, "brief": {...}}``. The model
    treated the list of choices as a set of switches. It had decided correctly;
    only the shape was wrong, and rejecting it threw away a right answer.

    Normalisation is deterministic and refuses to guess: exactly one job may be
    claimed. Two claimed jobs means the model did not decide, and that belongs
    in the correction pass rather than in a coin toss here.
    """

    claimed = [
        key
        for key in parsed
        if isinstance(key, str)
        and key.strip().casefold() in ACTION_ALIASES
        and not _declined(parsed[key])
    ]
    if len(claimed) != 1:
        return dict(parsed)

    key = claimed[0]
    action = ACTION_ALIASES[key.strip().casefold()]
    payload = parsed[key]


    decision = {
        name: value
        for name, value in parsed.items()
        if not (isinstance(name, str) and name.strip().casefold() in ACTION_ALIASES)
    }
    if isinstance(payload, Mapping):
        for name, value in payload.items():
            decision.setdefault(name, value)
    elif isinstance(payload, str) and payload.strip():
        field = _BARE_ARGUMENT_FIELD.get(action)
        if field:
            decision.setdefault(field, payload.strip())
    decision["action"] = action
    return decision


THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
UNCLOSED_THINK = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)


CODE_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?|\n?\s*```\s*$")


def strip_reasoning(reply: str) -> str:
    """Reduce a model reply to the decision it contains.

    Two things sit between the reply and its JSON, both observed from the real
    local model rather than guessed at. Base Steak emits ``<think>`` around its
    reasoning — in Cooking by design, and in Instant as an empty block the
    template still closes. And it wraps structured output in a markdown fence.
    Neither makes the decision malformed, so neither is treated as a parse
    failure.
    """

    text = THINK_BLOCK.sub("", str(reply or ""))
    text = UNCLOSED_THINK.sub("", text).strip()

    text = CODE_FENCE.sub("", text)
    return CODE_FENCE.sub("", text).strip()


def parse_decision(reply: str) -> dict[str, Any]:
    """Read the model's decision, whole-object only.

    Scraping JSON out of prose with a regex eventually matches something that
    only looks like a decision, which is worse than failing to parse.
    """

    from .actions import _whole_json_object

    parsed = _whole_json_object(strip_reasoning(reply))
    if not isinstance(parsed, Mapping):
        raise OrchestrationError("The reply was not a single JSON object.")

    parsed = dict(parsed)
    nested = parsed.get("action")
    if isinstance(nested, Mapping):





        inner = dict(nested)
        for key in ("type", "name", "action", "capability", "tool"):
            if isinstance(inner.get(key), str):
                parsed["action"] = inner.pop(key)
                break
        else:
            parsed["action"] = ""
        for key in ("details", "target", "app", "application", "url", "query"):
            if key in inner and key not in parsed:
                parsed[key] = inner[key]

        remaining = {
            key: value
            for key, value in inner.items()
            if key not in {"reason", "why"}
        }
        if remaining and "arguments" not in parsed:
            parsed["arguments"] = remaining

    if not str(parsed.get("action") or "").strip():



        parsed = _decision_from_job_keys(parsed)

    action = str(parsed.get("action") or "").strip().casefold()

    action = ACTION_ALIASES.get(action, action)

    decision = dict(parsed)
    if action not in JOB_TYPES and action in CAPABILITY_ACTIONS:





        decision.setdefault("capability", action)
        action = SINGLE_ACTION

    if (
        action not in JOB_TYPES
        and action not in CAPABILITY_ACTIONS
        and "." in action
    ):




        operation = decision.get("operation") or decision.get("capability")
        if isinstance(operation, str) and operation.strip():
            decision = {
                "action": PLAN,
                "nodes": [
                    {
                        "node": str(decision.get("node") or "service_action"),
                        "connector": action,
                        "operation": operation.strip(),
                        "arguments": dict(decision.get("arguments") or {}),
                    }
                ],
                "reason": str(decision.get("reason") or ""),
            }
            action = PLAN

    if action not in JOB_TYPES:
        raise OrchestrationError(
            f"{parsed.get('action')!r} is not one of: {', '.join(JOB_TYPES)}"
        )

    decision["action"] = action
    if action in {GENERATE_IMAGE, REVISE_IMAGE} and not isinstance(
        decision.get("brief"), Mapping
    ):




        image_payload = decision.get("with")
        if isinstance(image_payload, Mapping):
            decision["brief"] = dict(image_payload)


    if action == SINGLE_ACTION:
        arguments = dict(decision.get("arguments") or {})
        for key in ("target", "app", "application", "url", "details", "name"):
            if isinstance(decision.get(key), str) and "target" not in arguments:
                arguments["target"] = decision[key]
                break
        if arguments:
            decision["arguments"] = arguments
    return decision


def is_image_job(decision: Mapping[str, Any]) -> bool:
    return str(decision.get("action")) in {GENERATE_IMAGE, REVISE_IMAGE}


def conversation_request(
    history: Sequence[Mapping[str, Any]], *, limit: int = 6
) -> str:
    """The user's request, as the whole thread describes it.

    The old image path took only the newest user message. That is exactly why
    "that's wrong, generate the logo" arrived at the image model with no logo
    brief attached — the request had been stated one turn earlier and thrown
    away. Recent user turns are joined so the request survives the follow-up.
    """

    turns = [
        str(message.get("content") or "").strip()
        for message in history
        if str(message.get("role") or "").casefold() == "user"
    ]
    kept = [turn for turn in turns if turn][-limit:]
    return "\n\n".join(kept)


def latest_user_message(history: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(list(history)):
        if str(message.get("role") or "").casefold() == "user":
            return str(message.get("content") or "").strip()
    return ""


__all__ = [
    "GENERATE_IMAGE",
    "JOB_TYPES",
    "JOB_TYPE_MANIFEST",
    "ORCHESTRATOR_SCHEMA",
    "OrchestrationError",
    "PLAN",
    "RESEARCH",
    "RESPOND",
    "REVISE_IMAGE",
    "SINGLE_ACTION",
    "build_decision_prompt",
    "conversation_request",
    "is_image_job",
    "latest_user_message",
    "parse_decision",
]
