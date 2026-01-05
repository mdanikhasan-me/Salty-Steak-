"""Read-only, packaged evidence for the July 2026 scientific recovery checkpoint.

This module intentionally contains no training control, no workspace mutation, and no
automatic activation.  It makes the accepted evidence legible inside an isolated
desktop candidate without treating a bounded experiment as a quality recovery.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_RECOVERY_STATE: dict[str, Any] = {
    "schema": "salty-potato-scientific-recovery-v1",
    "state": "accepted",
    "classification": "PIPELINE ACCEPTED -- QUALITY TRAINING STILL REQUIRED",
    "read_only": True,
    "production_activation": False,
    "scope": (
        "A packaged summary of independently validated, isolated recovery evidence. "
        "It does not register data, start training, activate a model, or alter this workspace."
    ),
    "evidence_files": [
        "PHASE3_SCIENTIFIC_ACCEPTANCE_FINAL_RESULT.json",
        "PHASE4_THREE_CHECKPOINT_BASELINE_RESULT.json",
        "PHASE5_BOUNDED_CORRECTED_EXPERIMENT_PLAN.json",
        "PHASE5_BOUNDED_CORRECTED_EXPERIMENT_RESULT.json",
        "PHASE6_SCIENTIFIC_DECISION.json",
    ],
    "phase_3": {
        "state": "accepted",
        "zero_update_round_trip": {
            "passed": True,
            "model_weight_identity": True,
            "tokenizer_identity": True,
            "deterministic_greedy_token_equivalence": True,
        },
        "micro_overfit": {
            "passed": True,
            "initial_loss": 3.1875710487365723,
            "final_loss": 0.0004768303770106286,
            "checkpoint_reload_parity": True,
            "private_worker_smoke": True,
        },
        "exact_resume": {
            "passed": True,
            "stopped_at_step": 2,
            "resumed_to_step": 4,
            "weights_exact": True,
            "optimizer_scheduler_scaler_rng_exact": True,
            "data_order_cursor_counters_exact": True,
        },
    },
    "data": {
        "legacy_malformed_v1": {
            "state": "blocked",
            "label": "Malformed legacy prepared data (v1)",
            "training_selectable": False,
            "reason": (
                "It lacks durable assistant-target labels and provenance required by the "
                "prepared-data contract. It remains preserved as forensic evidence only."
            ),
        },
        "corrected_oasst2": {
            "state": "verified",
            "label": "Corrected English OASST2 prepared-v2",
            "prepared_dataset_id": "28049fe5-a846-4438-ba84-2b353f5d9742",
            "prepared_artifact_sha256": (
                "0321f446cbae80e610e5c73ee8e3cc2ef55a70fc01f4e9b618fcb74869ce5213"
            ),
            "accepted_target_windows": 7037,
            "valid_assistant_target_tokens": 1456716,
            "intact_rejected_overlong_targets": 1234,
            "train_target_tokens": 1311825,
            "holdout_target_tokens": 144891,
            "training_selectable_in_this_candidate": False,
            "preview": [
                "User and system context remain masked from the loss.",
                "Only an intact terminal assistant target contributes supervised labels.",
                "Overlong assistant targets are rejected intact; accepted windows do not cross target boundaries.",
            ],
        },
    },
    "comparison": [
        {
            "id": "pre_oasst2_parent",
            "label": "Pre-OASST2 parent",
            "role": "verified parent",
            "english_anchor_loss": 2.6780526226158767,
            "oasst2_holdout_loss": 2.8413834406695697,
        },
        {
            "id": "malformed_plus_2000",
            "label": "Malformed +2,000 steps",
            "role": "forensic regression evidence",
            "english_anchor_loss": 14.376517930148681,
            "oasst2_holdout_loss": 14.257276214240074,
        },
        {
            "id": "malformed_plus_20000",
            "label": "Malformed +20,000 steps",
            "role": "forensic regression evidence",
            "english_anchor_loss": 20.20233746467896,
            "oasst2_holdout_loss": 18.02014489506972,
        },
    ],
    "bounded_experiment": {
        "state": "accepted_as_pipeline_evidence",
        "starting_checkpoint": "3fc1a64a-d6ed-40df-adf6-57a53d2a84f2",
        "candidate_checkpoint": "candidate-corrected-oasst2-bounded",
        "optimizer_steps": 61,
        "valid_target_tokens": 48308,
        "corrected_dataset_passes": 0.03682503382692051,
        "maximum_corrected_dataset_passes": 0.10,
        "measured_training_seconds": 44.5437935,
        "maximum_measured_training_minutes": 20,
        "english_anchor_loss": 2.7389549854328985,
        "oasst2_holdout_loss": 2.551826276435166,
        "english_anchor_relative_loss_change": 0.022741286822636653,
        "oasst2_holdout_relative_loss_change": -0.10190710626728006,
        "reload_parity": True,
        "private_worker_smoke": True,
        "activated": False,
        "quality_claim": False,
        "generation_limitations": [
            "failure_to_stop",
            "extreme_repetition in several deterministic scenarios",
        ],
    },
    "recommended_next_stage": {
        "started": False,
        "requires_explicit_approval": True,
        "starting_checkpoint": "3fc1a64a-d6ed-40df-adf6-57a53d2a84f2",
        "corrected_oasst2_target_tokens": 655913,
        "optional_disjoint_english_replay_target_tokens": 131183,
        "estimated_optimizer_steps": 994,
        "estimated_duration": "about 12-16 minutes plus validation/checkpoint overhead; remeasure before approval",
        "gates": [
            "English Anchor v1 loss may not regress more than 5 percent from the verified parent without review.",
            "Corrected OASST2 holdout loss must improve relative to the verified parent.",
            "No UUID pattern, special-token leakage, empty output, or non-UTF output in the deterministic generation suite.",
            "Checkpoint, tokenizer, reload parity, and private-worker smoke must all pass.",
        ],
    },
}


def scientific_recovery_state() -> dict[str, Any]:
    """Return a detached read-only-report payload for the local API."""

    return deepcopy(_RECOVERY_STATE)
