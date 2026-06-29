"""Salty Steak Native Desktop AI Platform — did the goal actually happen?

Zonted is a claim that what the user asked for is now true. The label existed
long before anything checked: a turn was Zonted because it reached the end —
the research budget ran out, a capability returned ``succeeded``, a plan
reached its last node. None of those is evidence about the world. Asked to
delete some log files, the application once answered "the operation has
completed on your local system" with every file still present, and the turn
was marked done.

Three verdicts, and the third one matters:

``True``   evidence says the goal is reached.
``False``  evidence says it is not, or something protected was lost.
``None``   nothing here can tell, so nothing may be claimed.

``None`` is not success. A turn whose goal cannot be verified reports
Incomplete rather than borrowing a word it did not earn. That is deliberately
inconvenient: it is what makes the word mean something when it does appear, and
the way to earn it back is to write the verifier rather than to widen the rule.

Verification is per goal and per capability, and each one uses the strongest
source of truth available for that goal. Executing through a mechanism is never
its own proof — a click landing does not mean the mail was sent, and an exit
code of zero does not mean the system changed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

VERIFICATION_SCHEMA = "salty-steak-goal-verification-v1"



OUTWARD_KINDS = frozenset({"research", "action", "plan"})



MUTATING_FILE_OPERATIONS = frozenset(
    {"delete", "move", "rename", "copy", "create_directory"}
)


def verify_goal(orchestration: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """Whether this turn's own evidence shows its goal reached."""

    if not isinstance(orchestration, Mapping):
        return None, {"reason": "no_orchestration"}

    kind = str(orchestration.get("kind") or "")
    if kind not in OUTWARD_KINDS:
        return None, {"reason": "nothing_reached_outside"}

    if kind == "research":
        return _verify_research(orchestration)

    steps = orchestration.get("steps")
    if isinstance(steps, Sequence) and steps:
        return _verify_every_step(steps)

    return _verify_capability(
        str(orchestration.get("capability") or ""),
        orchestration.get("result") or {},
    )


def _verify_research(
    orchestration: Mapping[str, Any]
) -> tuple[bool | None, dict[str, Any]]:
    """Research is finished when it has evidence, not when it stops searching.

    The runner used to report "I read 6 sources and kept 145 findings" as the
    answer. Reading six sources is not the goal; the goal is knowing something,
    and an observation is the smallest unit of knowing that has a page behind
    it.
    """

    observations = list(orchestration.get("observations") or [])
    claims = list(orchestration.get("claims") or [])
    report = orchestration.get("research") or {}
    claim_count = int((report or {}).get("claim_count") or len(claims) or 0)

    evidence = {
        "observations": len(observations),
        "claims": claim_count,
        "stop_reason": str((report or {}).get("stop_reason") or ""),
    }
    if observations or claim_count:
        return True, evidence
    return False, {**evidence, "reason": "no_evidence_gathered"}


def _verify_every_step(
    steps: Sequence[Any]
) -> tuple[bool | None, dict[str, Any]]:
    """A plan is done when every step is, not when the last one returns.

    A definite failure anywhere outranks an unknown elsewhere: one broken
    predicate makes the whole goal false however much of the rest checked out.
    """

    verdicts: list[bool | None] = []
    details: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        capability = str(step.get("capability") or "")
        if not capability:
            continue
        verdict, evidence = _verify_capability(capability, step.get("result") or {})
        verdicts.append(verdict)
        details.append({"capability": capability, "verified": verdict, **evidence})

    if not verdicts:
        return None, {"reason": "no_verifiable_steps"}
    if any(verdict is False for verdict in verdicts):
        return False, {"steps": details, "reason": "a_step_did_not_verify"}
    if any(verdict is None for verdict in verdicts):
        return None, {"steps": details, "reason": "a_step_has_no_verifier"}
    return True, {"steps": details}


def _verify_capability(
    capability: str, result: Mapping[str, Any]
) -> tuple[bool | None, dict[str, Any]]:
    verifier = _VERIFIERS.get(capability)
    if verifier is None:



        return None, {"reason": "no_verifier_for_capability", "capability": capability}
    if not isinstance(result, Mapping) or not result:
        return None, {"reason": "no_result_recorded", "capability": capability}
    return verifier(result)


def _verify_files(result: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The filesystem read back, which is the strongest truth there is for files.

    ``after_state`` is produced by the capability re-reading every path after
    the calls it just made, rather than trusting that they worked.
    """

    if str(result.get("status") or "") != "succeeded":
        return False, {"reason": "capability_failed"}

    operation = str(result.get("operation") or "")
    if operation not in MUTATING_FILE_OPERATIONS and not result.get("mutating"):


        return True, {"operation": operation, "read_only": True}

    after = result.get("after_state")
    if not isinstance(after, Mapping):
        return None, {"reason": "no_after_state", "operation": operation}

    preserved = [str(item) for item in (result.get("preserved_paths") or [])]
    preserved_present = {str(item) for item in (after.get("preserved_present") or [])}
    protected_lost = [item for item in preserved if item not in preserved_present]
    still_present = [str(item) for item in (after.get("still_present") or [])]
    failed = list(result.get("failed_paths") or [])
    affected = [str(item) for item in (result.get("affected_paths") or [])]

    evidence = {
        "operation": operation,
        "removed": len(affected),
        "preserved": len(preserved_present),
        "protected_lost": len(protected_lost),
        "still_present": len(still_present),
    }




    if protected_lost:
        return False, {**evidence, "reason": "protected_paths_missing",
                       "lost": protected_lost[:10]}
    if failed:
        return False, {**evidence, "reason": "some_paths_failed"}
    if operation == "delete" and still_present:
        return False, {**evidence, "reason": "targets_still_present"}
    return True, evidence





_VERIFIERS = {
    "files.manage": _verify_files,
}


__all__ = ["VERIFICATION_SCHEMA", "OUTWARD_KINDS", "verify_goal"]
