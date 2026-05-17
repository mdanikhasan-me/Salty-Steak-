"""Salty Steak Native Desktop AI Platform — what a conversation has established.

The difference between a chat and a task.

A researched turn visits real pages, verifies real items and produces real
addresses. Until now that evidence reached the next turn as a note about
answering: "quote these addresses exactly". That is enough to answer "give me
the exact links" and not enough for anything else. Asked to *open* the cheapest
one, the model had verified prices in front of it and no indication it was
allowed to act on them, so it searched again — a second minute of work to
rediscover what was already known, and a different answer at the end of it.

Task state is the same evidence expressed as things rather than as quotations.
Each item gets a short stable reference, and the projection says plainly that
those references may be answered from *or* acted on. The model still decides
which one "the cheapest" is and whether acting is called for; the application's
only job is to make sure it does not have to guess at an address.

Everything here is a projection of records that already exist — researched
observations, the pages they came from, the images this conversation produced.
Nothing new is stored, so nothing can drift out of step with the transcript.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

TASK_STATE_SCHEMA = "salty-steak-task-state-v1"




MAX_ENTITIES = 8
MAX_PAGES = 8
MAX_ARTIFACTS = 3


def _text(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _entity(reference: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """One verified thing, with everything needed to act on it."""




    stock = _text(item.get("stock"), 40) or "unknown"
    entity = {
        "ref": reference,
        "name": _text(item.get("product") or item.get("name") or item.get("title")),


        "price": item.get("price_display") or item.get("price"),
        "currency": _text(item.get("currency_code") or item.get("currency"), 8),
        "seller": _text(item.get("seller"), 80),
        "stock": "not confirmed" if stock == "unknown" else stock,
        "variant": _text(item.get("variant"), 40),
        "url": _text(item.get("url"), 400),
    }
    status = str(item.get("status") or ACTIVE)
    if status != ACTIVE:
        entity["status"] = status
        entity["superseded_because"] = _text(item.get("superseded_because"), 160)

    return {key: value for key, value in entity.items() if value not in (None, "")}





ACTIVE = "active"
SUPERSEDED = "superseded"


def build(
    *,
    observations: Sequence[Mapping[str, Any]] = (),
    sources: Sequence[Mapping[str, Any]] = (),
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The structured state a conversation has built up, or an empty one."""

    entities = [
        _entity(f"e{index}", item)
        for index, item in enumerate(list(observations)[:MAX_ENTITIES], start=1)
        if isinstance(item, Mapping)
    ]
    pages = [
        {"title": _text(item.get("title"), 140), "url": _text(item.get("url"), 400)}
        for item in list(sources)[:MAX_PAGES]
        if isinstance(item, Mapping) and item.get("url")
    ]
    images = [
        {
            "ref": f"img{index}",
            "id": _text(item.get("id"), 64),
            "subject": _text(item.get("prompt"), 160),
        }
        for index, item in enumerate(list(artifacts)[:MAX_ARTIFACTS], start=1)
        if isinstance(item, Mapping) and item.get("id")
    ]
    return {
        "schema": TASK_STATE_SCHEMA,
        "entities": entities,
        "pages_read": pages,
        "images": images,
    }


def is_empty(state: Mapping[str, Any]) -> bool:
    return not (
        state.get("entities") or state.get("pages_read") or state.get("images")
    )


def project(state: Mapping[str, Any], *, can_act: bool = False) -> str:
    """The compact note that goes in front of the model.

    Two sentences of framing and then the data. The framing changes with
    authority, because what the model is allowed to do with an address is the
    one thing it cannot work out from the address itself: without execution
    authority these are quotations, and with it they are also destinations.
    """

    if is_empty(state):
        return ""

    lines = [
        "",
        "Reference material from earlier in this conversation. It comes from "
        "pages that were actually opened, not from memory.",




        "Answer the message that was actually sent. This is here to be drawn "
        "on when the message is about it — not to be restated.",
    ]
    if state.get("entities"):
        lines.append(
            "Each item has a short reference. When the user refers to one of "
            "them — the cheapest, the first, that one, those — they mean an "
            "item below."
        )
    if any(entity.get("status") == SUPERSEDED for entity in state.get("entities") or []):


        lines.append(
            'An item marked "superseded" was disproven later in this '
            "conversation. Do not use it, do not count it when comparing, and "
            "say it was corrected if the user asks about it."
        )
    if can_act and state.get("entities"):




        lines.append(
            "Being asked to go to one of these is an action to carry out, not "
            "something to describe: return the decision object and use the "
            "item's exact url. Describing the page instead of opening it does "
            "not do what was asked. Do not research this again — it is already "
            "verified — unless asked how current it is."
        )
    else:
        lines.append(
            "If you cite one of these, quote its address exactly and never "
            "write a link that is not here."
        )
    lines.append(json.dumps(_without_schema(state), default=str))
    return "\n".join(lines)


def _without_schema(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key != "schema" and value
    }


__all__ = [
    "MAX_ARTIFACTS",
    "MAX_ENTITIES",
    "MAX_PAGES",
    "TASK_STATE_SCHEMA",
    "build",
    "is_empty",
    "project",
]
