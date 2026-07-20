"""Salty Steak — the one place a capability call is made.

Base Steak is a 9B model running locally. Its structured output is usually
right and occasionally approximately right: a field named `app_name` instead of
`target`, plan vocabulary (`operation`, `query`) carried into capability
arguments, a capability named in the connector slot. Every one of those was a
real failure observed in the live application, and each was previously fixed
where it happened to surface.

Fixing them one at a time is a losing game — the next paraphrase produces the
next near-miss. So this module owns the whole boundary:

    unambiguous alias normalisation
      -> invoke
      -> on a schema error, hand the model a compact machine-readable contract
         and let it correct itself exactly once
      -> invoke again
      -> otherwise report the failure as an outcome

The repair is bounded to a single retry on purpose. An unbounded correction
loop is how a small model spends a whole task rewriting the same wrong call,
and the second failure is far better spent telling the user the truth.

Repair only ever runs for a *schema* complaint. A capability that refused for
authority, or a target that genuinely does not exist, is a real answer and is
returned untouched.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .capability_registry import CAPABILITY_FIELDS, get_capability_descriptor
from .routing import resolve_execution






SCHEMA_ERROR_MARKERS = ("Unknown automation fields", "must be one of")


class CapabilityCallFailed(RuntimeError):
    """A capability call that did not succeed, carrying why for the model.

    The original exception's own class name is kept, because "this was refused
    for permission" and "this was refused for a bad argument" call for
    different next moves and the model can only tell them apart if the
    observation still says which happened.
    """

    def __init__(
        self,
        message: str,
        *,
        capability: str,
        repairable: bool,
        original_type: str = "",
    ) -> None:
        super().__init__(message)
        self.capability = capability
        self.repairable = repairable
        self.original_type = original_type or type(self).__name__

    @property
    def kind(self) -> str:
        return self.original_type


def capability_contract(capability: str) -> dict[str, Any]:
    """The compact, machine-readable shape of one capability's arguments."""

    try:
        return get_capability_descriptor(capability).contract()
    except KeyError:
        return {
            "capability": capability,
            "accepts": sorted(CAPABILITY_FIELDS.get(capability, frozenset())),
        }


def _is_schema_complaint(error: BaseException) -> bool:
    if not isinstance(error, ValueError):
        return False
    message = str(error)
    return any(marker in message for marker in SCHEMA_ERROR_MARKERS)







ESSENTIAL_FIELD = {




    "files.manage": "operation",
    "browser.control": "command",
    "ui.automation": "command",
    "terminal.execute": "argv",
    "application.launch": "target",
    "window.control": "action",
    "input.control": "action",
    "screen.capture": None,
}


def prune_unsupported(
    capability: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Drop schema-unknown fields, but only when the call is unambiguous.

    Deterministic and narrow on purpose. This never removes a field the
    capability declares, never rewrites a value, and never runs at all when the
    capability's essential field is missing — a call that is not fully specified
    is a question for the model, not something to tidy up and execute.
    """

    accepted = CAPABILITY_FIELDS.get(capability)
    if not accepted:
        return dict(arguments), []
    essential = ESSENTIAL_FIELD.get(capability, "")
    if essential and not str(arguments.get(essential) or "").strip():
        return dict(arguments), []
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in arguments.items():
        if key in accepted:
            kept[key] = value
        else:
            dropped.append(str(key))
    return kept, sorted(dropped)


def invoke_capability(
    broker: Any,
    capability: str,
    arguments: Mapping[str, Any],
    *,
    authority_mode: str,
    granted: Any = (),
    repair: Callable[[str], Mapping[str, Any] | None] | None = None,
    on_route: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Carry out one capability call, repairing a near-miss once.

    ``repair`` receives a compact JSON description of what went wrong and what
    the capability accepts, and returns corrected arguments or ``None``. It is
    the model, and it is asked at most once.
    """

    allowed = list(granted) or [capability]



    route = resolve_execution(capability, arguments, allowed)
    pruned, dropped = prune_unsupported(route.capability, route.arguments)
    if dropped:
        route = resolve_execution(route.capability, pruned, allowed)
    if on_route is not None:
        on_route(route)

    def call(payload: Mapping[str, Any]) -> dict[str, Any]:
        return broker.invoke(
            {
                "capability": route.capability,
                "arguments": dict(payload),
                "user_confirmed": True,
                "authority_mode": authority_mode,
            }
        )

    try:
        return call(route.arguments)
    except (PermissionError, TimeoutError, OSError, RuntimeError, ValueError) as error:
        if repair is None or not _is_schema_complaint(error):
            raise CapabilityCallFailed(
                str(error),
                capability=route.capability,
                repairable=_is_schema_complaint(error),
                original_type=type(error).__name__,
            ) from error

        brief = json.dumps(
            {
                "error": "invalid_arguments",
                "message": str(error),
                "rejected": dict(route.arguments),
                **capability_contract(route.capability),
            },
            default=str,
        )
        corrected = repair(brief)
        if not isinstance(corrected, Mapping) or not corrected:
            raise CapabilityCallFailed(
                str(error),
                capability=route.capability,
                repairable=True,
                original_type=type(error).__name__,
            ) from error

        second = resolve_execution(route.capability, corrected, allowed)
        try:
            result = call(second.arguments)
        except (
            PermissionError,
            TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
        ) as second_error:


            raise CapabilityCallFailed(
                str(second_error),
                capability=route.capability,
                repairable=False,
                original_type=type(second_error).__name__,
            ) from second_error
        if on_route is not None:
            on_route(second)
        return result


__all__ = [
    "CapabilityCallFailed",
    "capability_contract",
    "invoke_capability",
]
