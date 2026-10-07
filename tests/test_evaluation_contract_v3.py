from __future__ import annotations

from app.backend.evaluation.contract import (
    build_evaluation_contract,
    compare_evaluation_contracts,
    evaluation_contract_digest_is_valid,
)
from app.backend.evaluation.evaluator import (
    perplexity_from_mean_loss,
    validate_evaluation_tokenizer_identity,
)


def manifest() -> dict:
    return {
        "dataset_id": "dataset-1",
        "source_digest": "a" * 64,
        "prepared_dataset_id": "prepared-1",
        "template": "salty-role-control-v1",
        "label_mask": "assistant_only",
        "truncation_rules": {"assistant_only": "target_preserving"},
        "settings": {"packing": True, "padding": True},
        "splits": {
            "validation": {
                "sequence_count": 10,
                "valid_target_token_count": 400,
                "split_group_digest": "b" * 64,
                "checksums": {
                    "validation-input-ids.npy": "c" * 64,
                    "validation-labels.npy": "d" * 64,
                },
            }
        },
    }


def contract(**overrides):
    values = {
        "family": "stage_specific",
        "manifest": manifest(),
        "prepared_manifest_digest": "e" * 64,
        "tokenizer_digest": "f" * 64,
        "model_config_digest": "1" * 64,
        "context_length": 8192,
        "sequence_length": 512,
        "precision": "bf16",
        "max_records": 0,
        "selected_records": 10,
        "selected_target_tokens": 400,
    }
    values.update(overrides)
    return build_evaluation_contract(**values)


def test_contract_digest_is_deterministic_and_complete() -> None:
    left = contract()
    right = contract()
    assert left == right
    assert len(left["contract_digest"]) == 64
    assert compare_evaluation_contracts(left, right)["compatible"]
    assert evaluation_contract_digest_is_valid(left)


def test_persisted_contract_digest_rejects_tampering() -> None:
    changed = contract()
    changed["sequence_length"] = 1024
    assert not evaluation_contract_digest_is_valid(changed)

    replaced_digest = contract()
    replaced_digest["contract_digest"] = "0" * 64
    assert not evaluation_contract_digest_is_valid(replaced_digest)


def test_evaluation_tokenizer_identity_rejects_mismatched_prepared_data() -> None:
    class Tokenizer:
        fingerprint = "checkpoint-fingerprint"
        metadata = {"model_sha256": "a" * 64}

    validate_evaluation_tokenizer_identity(
        {
            "tokenizer_fingerprint": "checkpoint-fingerprint",
            "tokenizer_digest": "a" * 64,
        },
        Tokenizer(),
        "checkpoint-fingerprint",
    )

    import pytest

    with pytest.raises(ValueError, match="does not match"):
        validate_evaluation_tokenizer_identity(
            {
                "tokenizer_fingerprint": "different-fingerprint",
                "tokenizer_digest": "a" * 64,
            },
            Tokenizer(),
            "checkpoint-fingerprint",
        )


def test_perplexity_is_not_silently_capped_at_loss_twenty() -> None:
    import math

    assert math.isclose(
        perplexity_from_mean_loss(21.0),
        math.exp(21.0),
        rel_tol=1e-12,
    )


def test_first_incompatibility_is_explained() -> None:
    left = contract()
    changed_manifest = manifest()
    changed_manifest["label_mask"] = "all_tokens"
    right = contract(manifest=changed_manifest)
    comparison = compare_evaluation_contracts(left, right)
    assert not comparison["compatible"]
    assert comparison["first_mismatch"] == "label_mask"
    assert comparison["message"] == "Different evaluation setup: label_mask differs"


def test_anchor_and_stage_results_never_share_one_trend() -> None:
    comparison = compare_evaluation_contracts(
        contract(family="fixed_anchor"),
        contract(family="stage_specific"),
    )
    assert not comparison["compatible"]
    assert comparison["first_mismatch"] == "family"
