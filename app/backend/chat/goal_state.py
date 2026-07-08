"""Salty Steak Native Desktop AI Platform — what the user asked for, as predicates.

The goal is the specification. The steps are not.

A verifier assembled out of step evidence answers "did everything I happened to
do work?". That is a different question from "is what the user asked for now
true", and on the run that matters they disagree completely:

    delete run-a.log  -> succeeded, nothing left behind  -> verified
    delete notes.txt  -> succeeded, nothing left behind  -> verified
                                                         => Zonted

Both steps did exactly what they claimed. The user had said to keep
``notes.txt``, and nothing in that evidence represents it, because nothing ever
bound it. Summing verified steps cannot notice a requirement that was never
executed — and a requirement nobody acted on is precisely the one worth
checking.

So the required outcomes are declared from the request, before execution, and
checked afterwards by re-observing the world. Three rules hold the whole thing
up:

* a goal with no declared predicates is **never** verified;
* a predicate nobody could observe is **unknown**, and unknown is not success;
* one contradicted predicate outranks any number of satisfied ones.

Nothing here knows about log files, or about any particular product, site or
application. A predicate is a kind, a subject and an optional detail; the
observers that answer them live with the capability that can see that kind of
thing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

GOAL_STATE_SCHEMA = "salty-steak-goal-state-v1"


@dataclass(frozen=True)
class Predicate:
    """One thing that must be true when the work is done.

    ``kind`` says what sort of claim it is, ``subject`` what it is about, and
    ``detail`` carries whatever that kind needs — a glob for a folder rule, a
    field name for an entity. Frozen so a spec cannot drift while it is being
    verified against.
    """

    kind: str
    subject: str
    detail: str = ""
    source: str = "requested"

    def describe(self) -> dict[str, str]:
        described = {"kind": self.kind, "subject": self.subject}
        if self.detail:
            described["detail"] = self.detail
        return described


@dataclass
class GoalSpec:
    """The user's goal, in a form something can check.

    ``protected`` is separate from ``required`` on the way in because it comes
    from a different part of the request — the half that says what must *not*
    change — and it has been the half that goes missing. It is folded into the
    required predicates immediately, so by verification time there is one list
    and no way to check the deletions while forgetting the preservations.
    """

    goal: str
    required: tuple[Predicate, ...] = ()
    constraints: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_outcomes(
        cls,
        *,
        goal: str,
        outcomes: Iterable[Mapping[str, Any]] = (),
        protected: Iterable[str] = (),
        constraints: Iterable[str] = (),
    ) -> "GoalSpec":
        required: list[Predicate] = []
        seen: set[tuple[str, str, str]] = set()

        def add(predicate: Predicate) -> None:
            key = (predicate.kind, predicate.subject, predicate.detail)
            if predicate.subject and key not in seen:
                seen.add(key)
                required.append(predicate)

        for outcome in outcomes or ():
            if not isinstance(outcome, Mapping):
                continue
            add(
                Predicate(
                    kind=str(outcome.get("kind") or "").strip().casefold(),
                    subject=str(outcome.get("target") or outcome.get("subject") or ""),
                    detail=str(outcome.get("detail") or outcome.get("pattern") or ""),
                )
            )




        for resource in protected or ():
            if str(resource).strip():
                add(Predicate("present", str(resource), source="protected"))

        return cls(
            goal=str(goal or ""),
            required=tuple(required),
            constraints=tuple(str(item) for item in (constraints or ()) if str(item)),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "schema": GOAL_STATE_SCHEMA,
            "goal": self.goal[:400],
            "required": [predicate.describe() for predicate in self.required],
            "constraints": list(self.constraints),
        }




Observer = Callable[[Predicate], "bool | None"]


def verify_predicates(
    spec: GoalSpec | None, observe: Observer
) -> tuple[bool | None, dict[str, Any]]:
    """Check every required predicate against the world as it is now.

    Re-observed rather than accumulated: the mechanism that made a change is
    never the thing that confirms it. A capability reporting success is a claim
    about a call, and this wants a claim about the world.
    """

    if spec is None or not spec.required:


        return None, {
            "reason": "no_required_predicates",
            "required_count": 0,
            "verified_count": 0,
        }

    verified: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    unverified: list[dict[str, str]] = []

    for predicate in spec.required:
        try:
            answer = observe(predicate)
        except Exception:

            answer = None
        described = predicate.describe()
        if answer is True:
            verified.append(described)
        elif answer is False:
            failed.append(described)
        else:
            unverified.append(described)

    evidence = {
        "required_count": len(spec.required),
        "verified_count": len(verified),
        "verified": verified,
        "failed": failed,
        "unverified": unverified,
    }




    if failed:
        return False, {**evidence, "reason": "a_required_predicate_is_false"}
    if unverified:
        return None, {**evidence, "reason": "a_required_predicate_was_not_observed"}
    return True, evidence


def filesystem_observer() -> Observer:
    """Answers ``present``/``absent`` by looking at the disk right now.

    Deliberately not reading the capability's own report of what it did. The
    filesystem is the strongest truth available for a file goal, and asking it
    directly is what makes this a verification rather than a restatement.
    """

    from pathlib import Path

    def observe(predicate: Predicate) -> bool | None:
        if predicate.kind not in {"present", "absent"}:
            return None
        subject = str(predicate.subject or "").strip()
        if not subject:
            return None
        try:
            if any(character in subject for character in "*?["):

                parent = Path(subject).parent
                pattern = Path(subject).name
                if not parent.exists():

                    return predicate.kind == "absent"
                matches = list(parent.glob(pattern))
                return not matches if predicate.kind == "absent" else bool(matches)
            exists = Path(subject).exists()
        except OSError:
            return None
        return exists if predicate.kind == "present" else not exists

    return observe


__all__ = [
    "GOAL_STATE_SCHEMA",
    "GoalSpec",
    "Observer",
    "Predicate",
    "filesystem_observer",
    "verify_predicates",
]
