"""The goal is the specification. The steps are not.

A verifier built out of step evidence answers "did everything I happened to do
work?", which is a different question from "is what the user asked for true?".
Run against the real shape of the catastrophic filesystem run — where the model
deletes the protected file as its own perfectly successful step — the
step-summing verifier returns True:

    delete run-a.log  -> succeeded, still_present: []   -> verified
    delete notes.txt  -> succeeded, still_present: []   -> verified
                                                        => Zonted

Both steps did what they said. The user's goal was destroyed. Nothing in that
evidence represents "keep notes.txt", because nothing ever bound it.

So required predicates are declared from the request before execution, and
checked against the world afterwards by re-observing it. A predicate nobody
checked is unknown, and unknown is never success.
"""

from __future__ import annotations

from app.backend.chat.goal_state import GoalSpec, Predicate, verify_predicates


def observer(world: dict[str, bool]):
    """Answer a predicate from a fixed world, or decline to answer.

    Returning None is the important case: it is what a resource nobody looked
    at produces, and it must never resolve to success.
    """

    def observe(predicate: Predicate):
        if predicate.subject not in world:
            return None
        present = world[predicate.subject]
        if predicate.kind == "present":
            return present
        if predicate.kind == "absent":
            return not present
        return None

    return observe


def spec(*predicates: Predicate) -> GoalSpec:
    return GoalSpec(goal="a goal", required=tuple(predicates))


def test_a_goal_with_no_predicates_is_never_verified() -> None:
    """The whole false-positive class. Nothing declared, nothing proven."""

    verified, evidence = verify_predicates(GoalSpec(goal="g", required=()), observer({}))
    assert verified is None
    assert evidence["reason"] == "no_required_predicates"


def test_deletions_succeeding_while_a_protected_file_vanished_is_never_zonted() -> None:
    """The catastrophic run, stated as the goal rather than as the steps."""

    verified, evidence = verify_predicates(
        spec(
            Predicate("absent", r"C:\w\run-a.log"),
            Predicate("present", r"C:\w\notes.txt"),
        ),
        observer({r"C:\w\run-a.log": False, r"C:\w\notes.txt": False}),
    )
    assert verified is False
    assert evidence["failed"] == [{"kind": "present", "subject": r"C:\w\notes.txt"}]


def test_deletions_succeeding_with_nobody_checking_the_protected_file_is_unknown() -> None:
    """Exactly what the shipped run did: right outcome, no evidence for half of it."""

    verified, evidence = verify_predicates(
        spec(
            Predicate("absent", r"C:\w\run-a.log"),
            Predicate("present", r"C:\w\notes.txt"),
        ),
        observer({r"C:\w\run-a.log": False}),
    )
    assert verified is None
    assert evidence["unverified"] == [
        {"kind": "present", "subject": r"C:\w\notes.txt"}
    ]


def test_every_required_predicate_verified_is_zonted() -> None:
    verified, evidence = verify_predicates(
        spec(
            Predicate("absent", r"C:\w\run-a.log"),
            Predicate("absent", r"C:\w\run-b.log"),
            Predicate("absent", r"C:\w\run-c.log"),
            Predicate("present", r"C:\w\notes.txt"),
            Predicate("present", r"C:\w\plan.md"),
        ),
        observer(
            {
                r"C:\w\run-a.log": False,
                r"C:\w\run-b.log": False,
                r"C:\w\run-c.log": False,
                r"C:\w\notes.txt": True,
                r"C:\w\plan.md": True,
            }
        ),
    )
    assert verified is True
    assert evidence["verified_count"] == 5
    assert evidence["required_count"] == 5


def test_extra_successful_work_cannot_stand_in_for_a_required_predicate() -> None:
    """Doing more than was asked is not doing what was asked."""

    verified, _ = verify_predicates(
        spec(
            Predicate("absent", r"C:\w\run-a.log"),
            Predicate("present", r"C:\w\notes.txt"),
        ),
        # Ten unrelated resources all in the state the model would like, and the
        # one that was actually required still unobserved.
        observer(
            {r"C:\w\run-a.log": False, **{rf"C:\w\extra-{n}": False for n in range(10)}}
        ),
    )
    assert verified is None


def test_one_failed_predicate_outranks_any_number_of_satisfied_ones() -> None:
    verified, evidence = verify_predicates(
        spec(
            *[Predicate("absent", rf"C:\w\run-{n}.log") for n in range(8)],
            Predicate("present", r"C:\w\notes.txt"),
        ),
        observer(
            {
                **{rf"C:\w\run-{n}.log": False for n in range(8)},
                r"C:\w\notes.txt": False,
            }
        ),
    )
    assert verified is False
    assert evidence["verified_count"] == 8
    assert len(evidence["failed"]) == 1


def test_protected_resources_become_required_predicates_on_their_own() -> None:
    """A thing the request said to keep is part of the goal, not a side note.

    It has to survive into verification even when the model never passed it to
    a capability — which is exactly what happened: the delete calls carried no
    preserved paths at all, so "keep notes.txt" existed nowhere by the time
    anything checked.
    """

    built = GoalSpec.from_outcomes(
        goal="delete the logs but keep the notes",
        outcomes=[{"kind": "absent", "target": r"C:\w\run-a.log"}],
        protected=[r"C:\w\notes.txt"],
    )

    assert {"kind": "present", "subject": r"C:\w\notes.txt"} in [
        predicate.describe() for predicate in built.required
    ]
    assert len(built.required) == 2
    # And it says where it came from, so the half of the request that goes
    # missing is the half that is easiest to audit.
    assert [p.source for p in built.required if p.kind == "present"] == ["protected"]


def test_a_predicate_kind_nothing_can_observe_is_unknown_not_true() -> None:
    """New goal families arrive before their observers do. Fail closed."""

    verified, evidence = verify_predicates(
        spec(Predicate("media_playing", "https://example.test/watch")),
        observer({}),
    )
    assert verified is None
    assert evidence["unverified"]
