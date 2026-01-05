"""Canonical evaluation contracts and compatibility decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


EVALUATION_CONTRACT_VERSION = "salty-potato-evaluation-contract-v3"
EVALUATION_CODE_VERSION = "salty-evaluator-v3"
LOSS_IMPLEMENTATION_VERSION = "causal-ce-shift-v1"

CONTRACT_FIELDS = (
    "family",
    "dataset_id",
    "dataset_digest",
    "prepared_dataset_id",
    "prepared_manifest_digest",
    "split_id",
    "split_digest",
    "tokenizer_digest",
    "model_config_digest",
    "context_length",
    "sequence_length",
    "template",
    "packing",
    "truncation",
    "padding",
    "label_mask",
    "loss_implementation_version",
    "valid_target_definition",
    "precision",
    "record_selection",
    "record_count",
    "target_token_count",
    "evaluation_code_version",
)


def canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def split_digest(split: Mapping[str, Any]) -> str:
    identity = {
        "checksums": dict(split.get("checksums") or {}),
        "sequence_count": int(split.get("sequence_count", 0)),
        "valid_target_token_count": int(
            split.get("valid_target_token_count", 0)
        ),
        "split_group_digest": split.get("split_group_digest"),
    }
    return canonical_digest(identity)


def build_evaluation_contract(
    *,
    family: str,
    manifest: Mapping[str, Any],
    prepared_manifest_digest: str,
    tokenizer_digest: str,
    model_config_digest: str,
    context_length: int,
    sequence_length: int,
    precision: str,
    max_records: int,
    selected_records: int,
    selected_target_tokens: int,
) -> dict[str, Any]:
    validation = dict(manifest["splits"]["validation"])
    settings = dict(manifest.get("settings") or {})
    contract = {
        "contract_version": EVALUATION_CONTRACT_VERSION,
        "family": str(family),
        "dataset_id": manifest.get("dataset_id"),
        "dataset_digest": manifest.get("source_digest")
        or manifest.get("source_checksum"),
        "prepared_dataset_id": manifest.get("prepared_dataset_id"),
        "prepared_manifest_digest": prepared_manifest_digest,
        "split_id": f"{manifest.get('prepared_dataset_id')}:validation",
        "split_digest": split_digest(validation),
        "tokenizer_digest": tokenizer_digest,
        "model_config_digest": model_config_digest,
        "context_length": int(context_length),
        "sequence_length": int(sequence_length),
        "template": manifest.get("template", "legacy_unspecified"),
        "packing": settings.get("packing"),
        "truncation": manifest.get("truncation_rules")
        or settings.get("truncation"),
        "padding": settings.get("padding"),
        "label_mask": manifest.get("label_mask", "legacy_all_tokens"),
        "loss_implementation_version": LOSS_IMPLEMENTATION_VERSION,
        "valid_target_definition": (
            "labels[:,1:] != -100 after one causal shift; token-weighted mean "
            "natural-log cross entropy"
        ),
        "precision": str(precision),
        "record_selection": {
            "method": "deterministic_prefix",
            "max_records": int(max_records),
        },
        "record_count": int(selected_records),
        "target_token_count": int(selected_target_tokens),
        "evaluation_code_version": EVALUATION_CODE_VERSION,
    }
    missing = [
        field for field in CONTRACT_FIELDS if contract.get(field) is None
    ]
    if missing:
        raise ValueError(
            "Evaluation contract is incomplete: " + ", ".join(missing)
        )
    contract["contract_digest"] = canonical_digest(contract)
    return contract


def evaluation_contract_digest_is_valid(
    contract: Mapping[str, Any],
) -> bool:
    """Verify completeness and the canonical digest of a persisted contract."""

    if contract.get("contract_version") != EVALUATION_CONTRACT_VERSION:
        return False
    if any(contract.get(field) is None for field in CONTRACT_FIELDS):
        return False
    provided = contract.get("contract_digest")
    if not isinstance(provided, str):
        return False
    unsigned = dict(contract)
    unsigned.pop("contract_digest", None)
    return provided == canonical_digest(unsigned)


def compare_evaluation_contracts(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> dict[str, Any]:
    for field in CONTRACT_FIELDS:
        if left.get(field) != right.get(field):
            return {
                "compatible": False,
                "first_mismatch": field,
                "left": left.get(field),
                "right": right.get(field),
                "message": f"Different evaluation setup: {field} differs",
            }
    return {
        "compatible": True,
        "first_mismatch": None,
        "message": "Evaluation contracts are compatible",
    }
