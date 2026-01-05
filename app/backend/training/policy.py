"""Durable policy and lifecycle vocabulary for completed training runs.

The policy is intentionally small and explicit.  It is stored with the
training operation before any expensive work starts, so a restart can never
silently reinterpret a legacy boolean or a missing value.
"""

from __future__ import annotations

from typing import Any, Mapping


POST_TRAINING_POLICIES = frozenset(
    {"save_only", "evaluate", "evaluate_and_activate", "legacy_unknown"}
)
POST_TRAINING_STATES = frozenset(
    {
        "not_started",
        "saving",
        "saved",
        "evaluating",
        "evaluated",
        "activating",
        "completed",
        "needs_attention",
        "legacy_unknown",
    }
)
POST_TRAINING_RECOVERY_STATES = frozenset(
    {"active", "resume_pending", "complete", "manual_only", "legacy_unknown"}
)


def normalise_post_training_policy(
    payload: Mapping[str, Any] | None,
    *,
    default: str = "evaluate_and_activate",
) -> str:
    """Resolve the new selector while preserving the old boolean contract.

    Explicit selector values always win.  The old checkbox is accepted only as
    a compatibility input; it maps to the same durable semantics users saw in
    the old UI.  Invalid values are rejected before an operation is queued.
    """

    values = payload or {}
    explicit = values.get("post_training_policy")
    if explicit is not None and str(explicit).strip():
        policy = str(explicit).strip().casefold()
        if policy not in POST_TRAINING_POLICIES - {"legacy_unknown"}:
            raise ValueError(
                "post_training_policy must be one of: save_only, evaluate, "
                "evaluate_and_activate"
            )
        return policy
    if "use_completed_version_in_chat" in values:
        legacy = values.get("use_completed_version_in_chat")
        if not isinstance(legacy, bool):
            raise ValueError("use_completed_version_in_chat must be a boolean")
        return "evaluate_and_activate" if legacy else "save_only"
    return default


def policy_label(policy: str) -> str:
    return {
        "save_only": "Save only",
        "evaluate": "Save and evaluate",
        "evaluate_and_activate": "Evaluate and activate",
        "legacy_unknown": "Legacy policy unavailable",
    }.get(policy, "Policy unavailable")


def initial_recovery_state(policy: str) -> str:
    return "legacy_unknown" if policy == "legacy_unknown" else "active"


def completion_outcome_for_policy(policy: str) -> str:
    return {
        "save_only": "saved",
        "evaluate": "evaluated",
        "evaluate_and_activate": "completed",
    }.get(policy, "needs_attention")
