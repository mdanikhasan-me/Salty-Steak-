import pytest

from app.backend.chat.goal_state import GoalSpec, Predicate
from app.backend.chat.verification import verify_goal


def research(answer):
    return {
        "kind": "research", "answer": answer,
        "claims": [{"text": "A retrieved finding."}],
        "sources": [
            {"url": "https://docs.example/spec", "validation": "validated"},
            {"url": "https://journal.example/paper", "validation": "validated"},
        ],
        "research": {"claim_count": 893, "source_count": 24,
                     "evidence_sufficient": True, "stop_reason": "evidence_sufficient"},
    }


@pytest.mark.parametrize("answer", [
    "The findings do not answer your request for the device specifications.",
    "The results cannot verify the requested release date.",
    "I could not find the requested documentation.",
    "I couldn't confirm that this product exists.",
])
@pytest.mark.parametrize("with_spec", [False, True])
def test_explicit_admission_is_not_verified_despite_many_sources(answer, with_spec):
    answer += " [Docs](https://docs.example/spec) [Paper](https://journal.example/paper)"
    spec = GoalSpec(goal="Find the specifications", required=(
        Predicate("exact_page", "https://docs.example/spec"),)) if with_spec else None
    verified, evidence = verify_goal(research(answer), spec=spec)
    assert verified is None
    assert evidence["reason"] == "answer_reports_missing_evidence"


def test_matching_page_predicate_does_not_excuse_invented_citation():
    spec = GoalSpec(goal="Find documentation", required=(Predicate("exact_page", "https://docs.example/spec"),))
    verified, evidence = verify_goal(research("See [this](https://invented.example/link)."), spec=spec)
    assert verified is False
    assert evidence["reason"] == "answer_cited_unvalidated_sources"


def test_failed_relevance_gate_precedes_matching_page_predicate():
    record = research("[Docs](https://docs.example/spec)")
    record["research"]["evidence_sufficient"] = False
    spec = GoalSpec(goal="Find documentation", required=(Predicate("exact_page", "https://docs.example/spec"),))
    verified, evidence = verify_goal(record, spec=spec)
    assert verified is None
    assert evidence["reason"] == "research_evidence_not_sufficient"


def test_matching_predicate_with_validated_citation_still_passes():
    spec = GoalSpec(goal="Find this exact page", required=(Predicate("exact_page", "https://docs.example/spec"),))
    assert verify_goal(research("The page is [here](https://docs.example/spec)."), spec=spec)[0] is True
