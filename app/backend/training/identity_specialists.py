"""Policy boundaries for conditional Base Steak identity specialists."""

from __future__ import annotations

from .base_steak_identity_dataset import IdentityExample
from .identity_evaluation import identity_question_contract


IDENTITY_SPECIALIST_POLICIES = (
    "model_name",
    "trainer",
    "relationship",
    "full",
    "boundary",
    "research",
    "clarify",
    "correction",
)

IDENTITY_SPECIALIST_RANKS = {
    "model_name": 32,
    "trainer": 64,
    "relationship": 96,
    "full": 64,
    "boundary": 32,
    "research": 32,
    "clarify": 32,
    "correction": 32,
}

IDENTITY_INTRODUCTION_REPAIR_ADAPTER_ID = (
    "base-steak-2-0-identity-introduction-repair-v1"
)
IDENTITY_FULL_REPAIR_ADAPTER_ID = "base-steak-2-0-identity-full-repair-v1"
IDENTITY_RESEARCH_REPAIR_ADAPTER_ID = (
    "base-steak-2-0-identity-research-repair-v1"
)
IDENTITY_CLARIFY_REPAIR_ADAPTER_ID = (
    "base-steak-2-0-identity-clarify-repair-v1"
)
IDENTITY_RELATIONSHIP_REPAIR_ADAPTER_ID = (
    "base-steak-2-0-identity-relationship-repair-v1"
)


def identity_specialist_policy(example: IdentityExample) -> str:
    category = str(example.category).casefold()
    for policy in ("clarify", "research", "boundary", "correction"):
        if policy in category:
            return policy
    if (
        "full_identity" in category
        or category.endswith("_full")
        or "_full_" in category
    ):
        return "full"
    if "model_name" in category:
        return "model_name"
    if "person_relationship" in category or (
        "relationship" in category and "model_relationship" not in category
    ):
        return "relationship"
    if "trainer" in category:
        return "trainer"
    contract = identity_question_contract(example.messages[-1][1])
    contracted = {
        "model_name": "model_name",
        "trainer": "trainer",
        "person_relationship": "relationship",
        "model_relationship": "relationship",
        "attitude_relationship": "relationship",
        "full_identity": "full",
    }.get(contract)
    if contracted is not None:
        return contracted
    return "full"


def identity_specialist_adapter_id(policy: str) -> str:
    checked = str(policy).strip().casefold()
    if checked not in IDENTITY_SPECIALIST_POLICIES:
        raise ValueError(f"Unknown identity specialist policy: {policy}")
    return f"base-steak-2-0-identity-{checked}-v1"


__all__ = [
    "IDENTITY_SPECIALIST_POLICIES",
    "IDENTITY_SPECIALIST_RANKS",
    "IDENTITY_INTRODUCTION_REPAIR_ADAPTER_ID",
    "IDENTITY_FULL_REPAIR_ADAPTER_ID",
    "IDENTITY_RESEARCH_REPAIR_ADAPTER_ID",
    "IDENTITY_CLARIFY_REPAIR_ADAPTER_ID",
    "IDENTITY_RELATIONSHIP_REPAIR_ADAPTER_ID",
    "identity_specialist_adapter_id",
    "identity_specialist_policy",
]
