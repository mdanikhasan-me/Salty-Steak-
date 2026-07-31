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

import re
from collections.abc import Mapping, Sequence
from typing import Any

VERIFICATION_SCHEMA = "salty-steak-goal-verification-v1"



OUTWARD_KINDS = frozenset({"research", "action", "plan"})



MUTATING_FILE_OPERATIONS = frozenset(
    {"delete", "move", "rename", "copy", "create_directory"}
)


def verify_goal(
    orchestration: Mapping[str, Any],
    *,
    spec: Any = None,
    observe: Any = None,
) -> tuple[bool | None, dict[str, Any]]:
    """Whether what the user asked for is now true.

    Two sources, and they are not symmetric.

    The **goal's own predicates** are the only thing that can grant success.
    They are declared from the request before the work runs and checked
    afterwards by re-observing the world, so a requirement nobody acted on is
    still checked. Without them there is nothing to be right about, and the
    verdict is unknown.

    The **steps** may only veto. Summing verified steps was the whole defect:
    asked to delete some logs and keep the notes, a run that deleted the notes
    as its own perfectly successful step returned True, because a requirement
    that was never executed is one no step can miss. Their remaining job is to
    catch a contradiction the predicates might not cover — a protected path
    that vanished, a path that failed to move.
    """

    if not isinstance(orchestration, Mapping):
        return None, {"reason": "no_orchestration"}

    kind = str(orchestration.get("kind") or "")
    if kind not in OUTWARD_KINDS:
        return None, {"reason": "nothing_reached_outside"}

    contradiction = _step_contradiction(orchestration)
    if contradiction is not None:
        return False, {"reason": "a_step_contradicted_the_goal", **contradiction}

    if kind == "research":
        return _verify_research(orchestration, spec)

    from .goal_state import filesystem_observer, verify_predicates

    return verify_predicates(spec, observe or filesystem_observer())


def _step_contradiction(
    orchestration: Mapping[str, Any]
) -> dict[str, Any] | None:
    """A step whose own report already disproves the goal.

    Deliberately one-directional. Nothing here can make a turn succeed.
    """

    steps = orchestration.get("steps")
    if not isinstance(steps, Sequence):
        steps = []
    records = [step for step in steps if isinstance(step, Mapping)] or [orchestration]

    for step in records:
        capability = str(step.get("capability") or step.get("action") or "")
        if capability != "files.manage":
            continue
        outcome = step.get("result") or step.get("observation") or {}
        if not isinstance(outcome, Mapping):
            continue
        preserved = [str(item) for item in (outcome.get("preserved_paths") or [])]
        if not preserved:
            continue
        after = outcome.get("after_state")
        if not isinstance(after, Mapping):
            continue
        present = {str(item) for item in (after.get("preserved_present") or [])}
        lost = [item for item in preserved if item not in present]
        if lost:



            return {"capability": capability, "protected_lost": lost[:10]}
    return None


def _verify_research(
    orchestration: Mapping[str, Any], spec: Any = None
) -> tuple[bool | None, dict[str, Any]]:
    """Having gathered evidence is not having answered the question.

    Those were one concept and they are two. "Find the cheapest currently
    in-stock 2TB Gen4 SSD in Bangladesh and give me the exact product link"
    carries the capacity, the region, current stock, an exact offer page,
    freshness, and *cheapest among validated candidates*. An observation about
    the wrong capacity, or about a seller who turns out to be out of stock, is
    real evidence and is not that goal.

    So observation count, claim count and source count decide nothing on their
    own. They measure how much was read. Whether the question was answered is
    a question about the requirements the asker actually stated, and those have
    to be declared and bound before anything can say yes.
    """

    observations = list(orchestration.get("observations") or [])
    claims = list(orchestration.get("claims") or [])
    report = orchestration.get("research") or {}
    claim_count = int((report or {}).get("claim_count") or len(claims) or 0)

    gathered = {
        "observations": len(observations),
        "claims": claim_count,
        "stop_reason": str((report or {}).get("stop_reason") or ""),
    }


    if not observations and not claim_count:
        return False, {**gathered, "reason": "no_evidence_gathered"}

    from .goal_state import verify_predicates

    if spec is not None and getattr(spec, "required", ()):
        verdict, evidence = verify_predicates(spec, _research_observer(orchestration))
        return verdict, {**gathered, **evidence}







    answer = str(orchestration.get("answer") or "").strip()
    sources = [
        source
        for source in (orchestration.get("sources") or [])
        if isinstance(source, Mapping)
        and str(source.get("validation") or "validated") == "validated"
        and str(source.get("url") or "").casefold().startswith(("http://", "https://"))
    ]
    stop_reason = str((report or {}).get("stop_reason") or "")
    if not answer or not sources or stop_reason not in {
        "evidence_sufficient",
        "checkpoint_completed",
    }:
        return None, {
            **gathered,
            "reason": "no_required_predicates",
            "validated_sources": len(sources),
            "answer_present": bool(answer),
        }

    from ..research.ledger import publisher_domain

    by_url = {
        _research_url_key(str(source.get("url") or "")): source for source in sources
    }
    cited = {
        _research_url_key(url)
        for url in re.findall(r"https?://[^\s)\]]+", answer, flags=re.IGNORECASE)
    }
    unknown = sorted(url for url in cited if url not in by_url)
    if unknown:
        return False, {
            **gathered,
            "reason": "answer_cited_unvalidated_sources",
            "unvalidated_citations": unknown[:10],
        }
    if not cited:
        return None, {**gathered, "reason": "answer_has_no_validated_citations"}

    source_publishers = {
        publisher_domain(str(source.get("url") or "")) for source in sources
    } - {""}
    cited_publishers = {
        publisher_domain(str(by_url[url].get("url") or ""))
        for url in cited
        if url in by_url
    } - {""}
    required_publishers = 2
    if len(source_publishers) < required_publishers:
        return None, {
            **gathered,
            "reason": "research_lacks_independent_sources",
            "validated_publishers": len(source_publishers),
        }
    if len(cited_publishers) < required_publishers:
        return None, {
            **gathered,
            "reason": "answer_lacks_independent_citations",
            "validated_publishers": len(source_publishers),
            "cited_publishers": len(cited_publishers),
        }
    return True, {
        **gathered,
        "reason": "validated_research_answer",
        "validated_sources": len(sources),
        "validated_publishers": len(source_publishers),
        "citation_count": len(cited),
        "cited_publishers": len(cited_publishers),
    }


def _research_url_key(value: str) -> str:
    return str(value or "").split("#", 1)[0].rstrip("/.,;:").casefold()


def _research_observer(orchestration: Mapping[str, Any]):
    """Answers the research predicates that bound evidence can settle.

    Only kinds where a single page bound the claim together count. Everything
    else — freshness windows, "cheapest among validated candidates", variant
    matching — is unknown until it has an observer of its own, which is the
    next piece of work rather than a reason to widen this one.
    """

    observations = [
        item for item in (orchestration.get("observations") or []) if isinstance(item, Mapping)
    ]
    validated_source_urls = {
        _research_url_key(str(item.get("url") or ""))
        for item in (orchestration.get("sources") or [])
        if isinstance(item, Mapping)
        and str(item.get("validation") or "validated") == "validated"
    }

    def observe(predicate: Any) -> bool | None:
        kind = str(getattr(predicate, "kind", ""))
        subject = str(getattr(predicate, "subject", "")).strip().casefold()
        if kind == "exact_page":


            observed = any(
                subject and subject == str(item.get("url") or "").strip().casefold()
                for item in observations
            )
            return observed or _research_url_key(subject) in validated_source_urls
        if kind == "stock_confirmed":


            return any(
                str(item.get("stock") or "unknown").strip().casefold() == "in_stock"
                and (not subject or subject in str(item.get("product") or "").casefold())
                for item in observations
            )
        return None

    return observe


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




        capability = str(step.get("capability") or step.get("action") or "")
        if not capability or capability == "respond":
            continue
        outcome = step.get("result") or step.get("observation") or {}
        verdict, evidence = _verify_capability(capability, outcome)
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
