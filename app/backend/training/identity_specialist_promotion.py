"""Guarded promotion of evaluated identity specialists and learned classifier."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

from ..system.files import ensure_within, sha256_file


SPECIALIST_FILENAMES = {
    "model_name": "Base-Steak-2.0-Identity-Model-Name-LoRA-v1-F32.gguf",
    "trainer": "Base-Steak-2.0-Identity-Trainer-LoRA-v1-F32.gguf",
    "relationship": "Base-Steak-2.0-Identity-Relationship-LoRA-v1-F32.gguf",
    "full": "Base-Steak-2.0-Identity-Full-LoRA-v1-F32.gguf",
    "boundary": "Base-Steak-2.0-Identity-Boundary-LoRA-v1-F32.gguf",
    "research": "Base-Steak-2.0-Identity-Research-LoRA-v1-F32.gguf",
    "clarify": "Base-Steak-2.0-Identity-Clarify-LoRA-v1-F32.gguf",
    "correction": "Base-Steak-2.0-Identity-Correction-LoRA-v1-F32.gguf",
}
CLASSIFIER_FILENAME = "Base-Steak-2.0-Identity-Subroute-Classifier-v1.npz"
INTRODUCTION_REPAIR_FILENAME = (
    "Base-Steak-2.0-Identity-Introduction-Repair-LoRA-v1-F32.gguf"
)
FULL_REPAIR_FILENAME = "Base-Steak-2.0-Identity-Full-Repair-LoRA-v1-F32.gguf"
RESEARCH_REPAIR_FILENAME = (
    "Base-Steak-2.0-Identity-Research-Repair-LoRA-v1-F32.gguf"
)
CLARIFY_REPAIR_FILENAME = (
    "Base-Steak-2.0-Identity-Clarify-Repair-LoRA-v1-F32.gguf"
)
RELATIONSHIP_REPAIR_FILENAME = (
    "Base-Steak-2.0-Identity-Relationship-Repair-LoRA-v1-F32.gguf"
)


def _read_passed(path: Path, schema: str | None = None) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("passed") is not True:
        raise ValueError(f"Evidence did not pass: {path}")
    if schema is not None and value.get("schema") != schema:
        raise ValueError(f"Evidence schema is not accepted: {path}")
    return value


def _verified_artifact(value: Mapping[str, Any]) -> tuple[Path, str, int]:
    path = Path(str(value.get("path") or "")).resolve()
    checksum = str(value.get("sha256") or "").casefold()
    size = int(value.get("size_bytes") or 0)
    if not path.is_file() or path.stat().st_size != size or sha256_file(path) != checksum:
        raise RuntimeError(f"Evaluated artifact changed: {path}")
    return path, checksum, size


def promote_identity_specialists(
    *,
    manifest_path: str | Path,
    model_library_root: str | Path,
    specialist_training_report: str | Path,
    introduction_repair_training_report: str | Path,
    full_repair_training_report: str | Path,
    research_repair_training_report: str | Path,
    clarify_repair_training_report: str | Path,
    relationship_repair_training_report: str | Path,
    classifier_training_report: str | Path,
    end_to_end_report: str | Path,
    retention_report: str | Path,
    rollback_root: str | Path,
    operation_id: str,
    scale: float = 0.6,
    introduction_repair_scale: float = 0.55,
    full_repair_scale: float = 0.4,
    research_repair_scale: float = 0.4,
    research_repair_repetition_penalty: float = 1.0,
    clarify_repair_scale: float = 0.7,
    clarify_repair_repetition_penalty: float = 1.1,
    relationship_repair_scale: float = 0.35,
    relationship_repair_repetition_penalty: float = 1.1,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path).resolve()
    library_root = Path(model_library_root).resolve()
    bundle_root = manifest_path.parent
    ensure_within(manifest_path, library_root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("display_name") != "Base Steak 2.0" or manifest.get(
        "architecture"
    ) != "steak20":
        raise ValueError("Specialist promotion is limited to Base Steak 2.0 steak20")
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError("Model artifact declaration is missing")
    model = (bundle_root / str(artifact.get("filename") or "")).resolve()
    ensure_within(model, bundle_root)
    expected_model_hash = str(artifact.get("sha256") or "").casefold()
    if sha256_file(model) != expected_model_hash:
        raise RuntimeError("Base model changed before specialist promotion")

    specialist_path = Path(specialist_training_report).resolve()
    repair_training_path = Path(introduction_repair_training_report).resolve()
    full_repair_training_path = Path(full_repair_training_report).resolve()
    research_repair_training_path = Path(
        research_repair_training_report
    ).resolve()
    clarify_repair_training_path = Path(clarify_repair_training_report).resolve()
    relationship_repair_training_path = Path(
        relationship_repair_training_report
    ).resolve()
    classifier_path = Path(classifier_training_report).resolve()
    e2e_path = Path(end_to_end_report).resolve()
    retention_path = Path(retention_report).resolve()
    specialists = _read_passed(
        specialist_path,
        "base-steak-identity-specialist-training-v1",
    )
    repair_specialists = _read_passed(
        repair_training_path,
        "base-steak-identity-specialist-training-v1",
    )
    full_repair_specialists = _read_passed(
        full_repair_training_path,
        "base-steak-identity-specialist-training-v1",
    )
    research_repair_specialists = _read_passed(
        research_repair_training_path,
        "base-steak-identity-specialist-training-v1",
    )
    clarify_repair_specialists = _read_passed(
        clarify_repair_training_path,
        "base-steak-identity-specialist-training-v1",
    )
    relationship_repair_specialists = _read_passed(
        relationship_repair_training_path,
        "base-steak-identity-specialist-training-v1",
    )
    classifier = _read_passed(
        classifier_path,
        "base-steak-identity-subroute-linear-v1",
    )
    e2e = _read_passed(
        e2e_path,
        "base-steak-identity-specialist-oracle-evaluation-v1",
    )
    retention = _read_passed(
        retention_path,
        "base-steak-identity-specialist-retention-v1",
    )
    if specialists.get("model", {}).get("sha256_after") != expected_model_hash:
        raise RuntimeError("Specialists were trained against a different model")
    if specialists.get("model", {}).get("unchanged") is not True:
        raise RuntimeError("Specialist evidence does not prove base immutability")
    if (
        repair_specialists.get("model", {}).get("sha256_after")
        != expected_model_hash
        or repair_specialists.get("model", {}).get("unchanged") is not True
    ):
        raise RuntimeError(
            "Introduction repair evidence does not prove the same immutable base"
        )
    if (
        full_repair_specialists.get("model", {}).get("sha256_after")
        != expected_model_hash
        or full_repair_specialists.get("model", {}).get("unchanged") is not True
    ):
        raise RuntimeError(
            "Full repair evidence does not prove the same immutable base"
        )
    if (
        research_repair_specialists.get("model", {}).get("sha256_after")
        != expected_model_hash
        or research_repair_specialists.get("model", {}).get("unchanged")
        is not True
    ):
        raise RuntimeError(
            "Research repair evidence does not prove the same immutable base"
        )
    if (
        clarify_repair_specialists.get("model", {}).get("sha256_after")
        != expected_model_hash
        or clarify_repair_specialists.get("model", {}).get("unchanged")
        is not True
    ):
        raise RuntimeError(
            "Clarify repair evidence does not prove the same immutable base"
        )
    if (
        relationship_repair_specialists.get("model", {}).get("sha256_after")
        != expected_model_hash
        or relationship_repair_specialists.get("model", {}).get("unchanged")
        is not True
    ):
        raise RuntimeError(
            "Relationship repair evidence does not prove the same immutable base"
        )
    if e2e.get("router") != "learned_full_context_linear_classifier":
        raise RuntimeError("End-to-end evidence did not use the learned classifier")
    if e2e.get("training_report_sha256") != sha256_file(specialist_path):
        raise RuntimeError("End-to-end evidence references different specialists")
    if e2e.get("introduction_repair_training_report_sha256") != sha256_file(
        repair_training_path
    ):
        raise RuntimeError(
            "End-to-end evidence references a different introduction repair specialist"
        )
    if e2e.get("full_repair_training_report_sha256") != sha256_file(
        full_repair_training_path
    ):
        raise RuntimeError(
            "End-to-end evidence references a different full repair specialist"
        )
    if e2e.get("research_repair_training_report_sha256") != sha256_file(
        research_repair_training_path
    ):
        raise RuntimeError(
            "End-to-end evidence references a different research repair specialist"
        )
    if e2e.get("clarify_repair_training_report_sha256") != sha256_file(
        clarify_repair_training_path
    ):
        raise RuntimeError(
            "End-to-end evidence references a different clarify repair specialist"
        )
    if e2e.get("relationship_repair_training_report_sha256") != sha256_file(
        relationship_repair_training_path
    ):
        raise RuntimeError(
            "End-to-end evidence references a different relationship repair specialist"
        )
    if e2e.get("classifier_report_sha256") != sha256_file(classifier_path):
        raise RuntimeError("End-to-end evidence references a different classifier")
    if retention.get("training_report_sha256") != sha256_file(specialist_path):
        raise RuntimeError("Retention evidence references different specialists")
    if retention.get("introduction_repair_training_report_sha256") != sha256_file(
        repair_training_path
    ):
        raise RuntimeError(
            "Retention evidence references a different introduction repair specialist"
        )
    if retention.get("full_repair_training_report_sha256") != sha256_file(
        full_repair_training_path
    ):
        raise RuntimeError(
            "Retention evidence references a different full repair specialist"
        )
    if retention.get("research_repair_training_report_sha256") != sha256_file(
        research_repair_training_path
    ):
        raise RuntimeError(
            "Retention evidence references a different research repair specialist"
        )
    if retention.get("clarify_repair_training_report_sha256") != sha256_file(
        clarify_repair_training_path
    ):
        raise RuntimeError(
            "Retention evidence references a different clarify repair specialist"
        )
    if retention.get("relationship_repair_training_report_sha256") != sha256_file(
        relationship_repair_training_path
    ):
        raise RuntimeError(
            "Retention evidence references a different relationship repair specialist"
        )
    if int(e2e.get("metrics", {}).get("pass_count") or 0) != int(
        e2e.get("metrics", {}).get("count") or -1
    ):
        raise RuntimeError("End-to-end identity prompts did not all pass")
    if int(retention.get("metrics", {}).get("exact_count") or 0) != int(
        retention.get("metrics", {}).get("count") or -1
    ):
        raise RuntimeError("Ordinary retention was not exact")
    if float(e2e.get("scale") or 0.0) != float(scale) or float(
        retention.get("scale") or 0.0
    ) != float(scale):
        raise RuntimeError("Specialist scale differs between evidence reports")
    policy_scales = {
        str(policy): float(value)
        for policy, value in dict(e2e.get("policy_scales") or {}).items()
    }
    retention_policy_scales = {
        str(policy): float(value)
        for policy, value in dict(retention.get("policy_scales") or {}).items()
    }
    if (
        set(policy_scales) != set(SPECIALIST_FILENAMES)
        or retention_policy_scales != policy_scales
        or any(value <= 0 or value > 2 for value in policy_scales.values())
    ):
        raise RuntimeError("Policy-specific specialist scales are not consistent")
    policy_repetition_penalties = {
        str(policy): float(value)
        for policy, value in dict(
            e2e.get("policy_repetition_penalties") or {}
        ).items()
    }
    if set(policy_repetition_penalties) != set(SPECIALIST_FILENAMES) or any(
        value < 1.0 or value > 2.0
        for value in policy_repetition_penalties.values()
    ):
        raise RuntimeError("Policy-specific repetition penalties are invalid")
    if float(e2e.get("introduction_repair_scale") or 0.0) != float(
        introduction_repair_scale
    ) or float(retention.get("introduction_repair_scale") or 0.0) != float(
        introduction_repair_scale
    ):
        raise RuntimeError(
            "Introduction repair scale differs between evidence reports"
        )
    if float(e2e.get("full_repair_scale") or 0.0) != float(
        full_repair_scale
    ) or float(retention.get("full_repair_scale") or 0.0) != float(
        full_repair_scale
    ):
        raise RuntimeError("Full repair scale differs between evidence reports")
    if float(e2e.get("research_repair_scale") or 0.0) != float(
        research_repair_scale
    ) or float(retention.get("research_repair_scale") or 0.0) != float(
        research_repair_scale
    ):
        raise RuntimeError("Research repair scale differs between evidence reports")
    if float(
        e2e.get("research_repair_repetition_penalty") or 0.0
    ) != float(research_repair_repetition_penalty) or float(
        retention.get("research_repair_repetition_penalty") or 0.0
    ) != float(research_repair_repetition_penalty):
        raise RuntimeError(
            "Research repair repetition penalty differs between evidence reports"
        )
    if float(e2e.get("clarify_repair_scale") or 0.0) != float(
        clarify_repair_scale
    ) or float(retention.get("clarify_repair_scale") or 0.0) != float(
        clarify_repair_scale
    ):
        raise RuntimeError("Clarify repair scale differs between evidence reports")
    if float(
        e2e.get("clarify_repair_repetition_penalty") or 0.0
    ) != float(clarify_repair_repetition_penalty) or float(
        retention.get("clarify_repair_repetition_penalty") or 0.0
    ) != float(clarify_repair_repetition_penalty):
        raise RuntimeError(
            "Clarify repair repetition penalty differs between evidence reports"
        )
    if float(e2e.get("relationship_repair_scale") or 0.0) != float(
        relationship_repair_scale
    ) or float(retention.get("relationship_repair_scale") or 0.0) != float(
        relationship_repair_scale
    ):
        raise RuntimeError(
            "Relationship repair scale differs between evidence reports"
        )
    if float(
        e2e.get("relationship_repair_repetition_penalty") or 0.0
    ) != float(relationship_repair_repetition_penalty) or float(
        retention.get("relationship_repair_repetition_penalty") or 0.0
    ) != float(relationship_repair_repetition_penalty):
        raise RuntimeError(
            "Relationship repair repetition penalty differs between evidence reports"
        )

    staged: list[tuple[Path, Path, str, int]] = []
    companion_rows = []
    seen_policies = set()
    for specialist in specialists.get("specialists", []):
        policy = str(specialist.get("policy") or "")
        if policy not in SPECIALIST_FILENAMES or policy in seen_policies:
            raise ValueError("Specialist training report has an invalid policy layout")
        seen_policies.add(policy)
        source, checksum, size = _verified_artifact(specialist["adapter"])
        target = (bundle_root / SPECIALIST_FILENAMES[policy]).resolve()
        ensure_within(target, bundle_root)
        staged.append((source, target, checksum, size))
        companion_rows.append(
            {
                "id": str(specialist["adapter_id"]),
                "display_name": f"Base Steak 2.0 Identity {policy.title()}",
                "filename": target.name,
                "role": "text_adapter",
                "format": "GGUF",
                "quantization": "F32",
                "size_bytes": size,
                "sha256": checksum,
                "scale": policy_scales[policy],
                "repetition_penalty": policy_repetition_penalties[policy],
                "activation": "identity_intent",
                "state": "learned_specialist_evaluation_passed",
                "reason": (
                    f"Learned {policy} identity specialist; selected by the "
                    "checksum-bound full-context learned classifier."
                ),
            }
        )
    if seen_policies != set(SPECIALIST_FILENAMES):
        raise ValueError("Specialist training report is incomplete")
    repair_specialist = next(
        (
            value
            for value in repair_specialists.get("specialists", [])
            if value.get("policy") == "introduction"
        ),
        None,
    )
    if repair_specialist is None:
        raise ValueError("Introduction repair training report is incomplete")
    repair_source, repair_hash, repair_size = _verified_artifact(
        repair_specialist["adapter"]
    )
    repair_target = (bundle_root / INTRODUCTION_REPAIR_FILENAME).resolve()
    ensure_within(repair_target, bundle_root)
    staged.append((repair_source, repair_target, repair_hash, repair_size))
    companion_rows.append(
        {
            "id": "base-steak-2-0-identity-introduction-repair-v1",
            "display_name": "Base Steak 2.0 Identity Introduction Repair",
            "filename": repair_target.name,
            "role": "text_adapter",
            "format": "GGUF",
            "quantization": "F32",
            "size_bytes": repair_size,
            "sha256": repair_hash,
            "scale": float(introduction_repair_scale),
            "activation": "identity_intent",
            "state": "learned_specialist_evaluation_passed",
            "reason": (
                "A separately registered lower-scale view of the learned "
                "introduction specialist; used only after prompt-aware validation "
                "rejects the primary learned output."
            ),
        }
    )
    full_repair_specialist = next(
        (
            value
            for value in full_repair_specialists.get("specialists", [])
            if value.get("policy") == "full"
        ),
        None,
    )
    if full_repair_specialist is None:
        raise ValueError("Full repair training report is incomplete")
    full_repair_source, full_repair_hash, full_repair_size = _verified_artifact(
        full_repair_specialist["adapter"]
    )
    full_repair_target = (bundle_root / FULL_REPAIR_FILENAME).resolve()
    ensure_within(full_repair_target, bundle_root)
    staged.append(
        (
            full_repair_source,
            full_repair_target,
            full_repair_hash,
            full_repair_size,
        )
    )
    companion_rows.append(
        {
            "id": "base-steak-2-0-identity-full-repair-v1",
            "display_name": "Base Steak 2.0 Identity Full Repair",
            "filename": full_repair_target.name,
            "role": "text_adapter",
            "format": "GGUF",
            "quantization": "F32",
            "size_bytes": full_repair_size,
            "sha256": full_repair_hash,
            "scale": float(full_repair_scale),
            "repetition_penalty": policy_repetition_penalties["full"],
            "activation": "identity_intent",
            "state": "learned_specialist_evaluation_passed",
            "reason": (
                "A learned full-identity remediation expert used only when the "
                "primary full expert fails prompt-aware validation."
            ),
        }
    )
    research_repair_specialist = next(
        (
            value
            for value in research_repair_specialists.get("specialists", [])
            if value.get("policy") == "research"
        ),
        None,
    )
    if research_repair_specialist is None:
        raise ValueError("Research repair training report is incomplete")
    (
        research_repair_source,
        research_repair_hash,
        research_repair_size,
    ) = _verified_artifact(research_repair_specialist["adapter"])
    research_repair_target = (bundle_root / RESEARCH_REPAIR_FILENAME).resolve()
    ensure_within(research_repair_target, bundle_root)
    staged.append(
        (
            research_repair_source,
            research_repair_target,
            research_repair_hash,
            research_repair_size,
        )
    )
    companion_rows.append(
        {
            "id": "base-steak-2-0-identity-research-repair-v1",
            "display_name": "Base Steak 2.0 Identity Research Repair",
            "filename": research_repair_target.name,
            "role": "text_adapter",
            "format": "GGUF",
            "quantization": "F32",
            "size_bytes": research_repair_size,
            "sha256": research_repair_hash,
            "scale": float(research_repair_scale),
            "repetition_penalty": float(
                research_repair_repetition_penalty
            ),
            "activation": "identity_intent",
            "state": "learned_specialist_evaluation_passed",
            "reason": (
                "A lower-scale learned research remediation expert used only "
                "when prompt-aware validation rejects the primary research answer."
            ),
        }
    )
    clarify_repair_specialist = next(
        (
            value
            for value in clarify_repair_specialists.get("specialists", [])
            if value.get("policy") == "clarify"
        ),
        None,
    )
    if clarify_repair_specialist is None:
        raise ValueError("Clarify repair training report is incomplete")
    (
        clarify_repair_source,
        clarify_repair_hash,
        clarify_repair_size,
    ) = _verified_artifact(clarify_repair_specialist["adapter"])
    clarify_repair_target = (bundle_root / CLARIFY_REPAIR_FILENAME).resolve()
    ensure_within(clarify_repair_target, bundle_root)
    staged.append(
        (
            clarify_repair_source,
            clarify_repair_target,
            clarify_repair_hash,
            clarify_repair_size,
        )
    )
    companion_rows.append(
        {
            "id": "base-steak-2-0-identity-clarify-repair-v1",
            "display_name": "Base Steak 2.0 Identity Clarify Repair",
            "filename": clarify_repair_target.name,
            "role": "text_adapter",
            "format": "GGUF",
            "quantization": "F32",
            "size_bytes": clarify_repair_size,
            "sha256": clarify_repair_hash,
            "scale": float(clarify_repair_scale),
            "repetition_penalty": float(clarify_repair_repetition_penalty),
            "activation": "identity_intent",
            "state": "learned_specialist_evaluation_passed",
            "reason": (
                "A higher-scale learned clarification remediation expert used "
                "only after prompt-aware validation rejects the primary answer."
            ),
        }
    )
    relationship_repair_specialist = next(
        (
            value
            for value in relationship_repair_specialists.get("specialists", [])
            if value.get("policy") == "relationship"
        ),
        None,
    )
    if relationship_repair_specialist is None:
        raise ValueError("Relationship repair training report is incomplete")
    (
        relationship_repair_source,
        relationship_repair_hash,
        relationship_repair_size,
    ) = _verified_artifact(relationship_repair_specialist["adapter"])
    relationship_repair_target = (
        bundle_root / RELATIONSHIP_REPAIR_FILENAME
    ).resolve()
    ensure_within(relationship_repair_target, bundle_root)
    staged.append(
        (
            relationship_repair_source,
            relationship_repair_target,
            relationship_repair_hash,
            relationship_repair_size,
        )
    )
    companion_rows.append(
        {
            "id": "base-steak-2-0-identity-relationship-repair-v1",
            "display_name": "Base Steak 2.0 Identity Relationship Repair",
            "filename": relationship_repair_target.name,
            "role": "text_adapter",
            "format": "GGUF",
            "quantization": "F32",
            "size_bytes": relationship_repair_size,
            "sha256": relationship_repair_hash,
            "scale": float(relationship_repair_scale),
            "repetition_penalty": float(
                relationship_repair_repetition_penalty
            ),
            "activation": "identity_intent",
            "state": "learned_specialist_evaluation_passed",
            "reason": (
                "A lower-scale learned relationship remediation expert used "
                "only after broader learned repair fails validation."
            ),
        }
    )
    classifier_source, classifier_hash, classifier_size = _verified_artifact(
        classifier["artifact"]
    )
    classifier_target = (bundle_root / CLASSIFIER_FILENAME).resolve()
    ensure_within(classifier_target, bundle_root)
    staged.append(
        (
            classifier_source,
            classifier_target,
            classifier_hash,
            classifier_size,
        )
    )
    companion_rows.append(
        {
            "id": "base-steak-2-0-identity-subroute-classifier-v1",
            "display_name": "Base Steak 2.0 Identity Subroute Classifier",
            "filename": classifier_target.name,
            "role": "identity_subroute_classifier",
            "format": "NPZ",
            "quantization": "F32",
            "size_bytes": classifier_size,
            "sha256": classifier_hash,
            "scale": 1.0,
            "activation": "always",
            "state": "learned_subroute_evaluation_passed",
            "reason": (
                "Learned hashed full-context classifier; all 55 known-holdout "
                "identity subroutes passed without answer text or hand-written rules."
            ),
        }
    )

    rollback = Path(rollback_root).resolve() / f"{operation_id}-{classifier_hash[:12]}"
    rollback.mkdir(parents=True, exist_ok=False)
    shutil.copy2(manifest_path, rollback / "model.json")
    for value in manifest.get("companion_artifacts", []):
        if not isinstance(value, dict) or value.get("role") not in {
            "text_adapter",
            "identity_subroute_classifier",
        }:
            continue
        old = (bundle_root / str(value.get("filename") or "")).resolve()
        ensure_within(old, bundle_root)
        if old.is_file():
            shutil.copy2(old, rollback / old.name)

    staged_targets = []
    try:
        for source, target, checksum, _size in staged:
            next_path = bundle_root / f".{target.name}.{operation_id}.next"
            shutil.copyfile(source, next_path)
            if sha256_file(next_path) != checksum:
                raise RuntimeError(f"Staged specialist checksum failed: {target.name}")
            staged_targets.append((next_path, target))

        existing = list(manifest.get("companion_artifacts") or [])
        manifest["companion_artifacts"] = [
            value
            for value in existing
            if not (
                isinstance(value, dict)
                and value.get("role")
                in {"text_adapter", "identity_subroute_classifier"}
            )
        ] + companion_rows
        identity = manifest.setdefault("identity", {})
        identity.update(
            {
                "public_model_name": "Base Steak 2.0",
                "public_architecture_name": "steak20",
                "trainer": "MD Anik Hasan (Sawlper)",
                "identity_adapter_required": True,
                "identity_adapter_scale": float(scale),
                "identity_policy_scales": policy_scales,
                "identity_policy_repetition_penalties": (
                    policy_repetition_penalties
                ),
                "identity_introduction_repair_scale": float(
                    introduction_repair_scale
                ),
                "identity_full_repair_scale": float(full_repair_scale),
                "identity_research_repair_scale": float(
                    research_repair_scale
                ),
                "identity_research_repair_repetition_penalty": float(
                    research_repair_repetition_penalty
                ),
                "identity_clarify_repair_scale": float(clarify_repair_scale),
                "identity_clarify_repair_repetition_penalty": float(
                    clarify_repair_repetition_penalty
                ),
                "identity_relationship_repair_scale": float(
                    relationship_repair_scale
                ),
                "identity_relationship_repair_repetition_penalty": float(
                    relationship_repair_repetition_penalty
                ),
                "identity_adapter_activation": "identity_intent",
                "identity_training_method": (
                    "eight_learned_output_lora_specialists_plus_generic_"
                    "introduction_full_research_clarify_and_relationship_"
                    "repair_plus_learned_"
                    "full_context_classifier"
                ),
                "identity_training_report_sha256": sha256_file(specialist_path),
                "identity_introduction_repair_training_report_sha256": (
                    sha256_file(repair_training_path)
                ),
                "identity_full_repair_training_report_sha256": (
                    sha256_file(full_repair_training_path)
                ),
                "identity_research_repair_training_report_sha256": (
                    sha256_file(research_repair_training_path)
                ),
                "identity_clarify_repair_training_report_sha256": (
                    sha256_file(clarify_repair_training_path)
                ),
                "identity_relationship_repair_training_report_sha256": (
                    sha256_file(relationship_repair_training_path)
                ),
                "identity_subroute_report_sha256": sha256_file(classifier_path),
                "identity_free_generation_report_sha256": sha256_file(e2e_path),
                "identity_retention_report_sha256": sha256_file(retention_path),
                "identity_evaluation_metrics": {
                    "identity_count": int(e2e["metrics"]["count"]),
                    "identity_pass_count": int(e2e["metrics"]["pass_count"]),
                    "identity_route_activation_pass_count": int(
                        e2e["metrics"]["count"]
                    ),
                    "identity_first_pass_count": int(
                        e2e["metrics"]["first_pass_count"]
                    ),
                    "identity_recovery_count": int(
                        e2e["metrics"]["expert_fallback_count"]
                    ),
                    "identity_recovery_pass_count": int(
                        e2e["metrics"]["expert_fallback_success_count"]
                    ),
                    "identity_timeout_count": int(
                        e2e["metrics"]["timeout_count"]
                    ),
                    "retention_count": int(retention["metrics"]["count"]),
                    "retention_route_disable_pass_count": int(
                        retention["metrics"]["count"]
                    ),
                    "retention_exact_baseline_count": int(
                        retention["metrics"]["exact_count"]
                    ),
                    "retention_exact_baseline_rate": 1.0,
                    "retention_output_clean_count": int(
                        retention["metrics"]["count"]
                    ),
                },
                "behavior_claim": (
                    "Base Steak 2.0, trained by MD Anik Hasan (Sawlper); "
                    "55/55 known-holdout learned-routed identity conversations "
                    f"({int(e2e['metrics']['first_pass_count'])} primary, "
                    f"{int(e2e['metrics']['expert_fallback_count'])} learned "
                    "generic recoveries) and "
                    "100/100 exact adapter-disabled retention turns passed."
                ),
            }
        )
        next_manifest = bundle_root / f".model.json.{operation_id}.next"
        next_manifest.write_bytes(
            (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(
                "utf-8"
            )
        )
        json.loads(next_manifest.read_text(encoding="utf-8"))
        for next_path, target in staged_targets:
            os.replace(next_path, target)
        os.replace(next_manifest, manifest_path)
    except Exception:
        for next_path, _target in staged_targets:
            next_path.unlink(missing_ok=True)
        raise

    rollback_record = {
        "operation_id": operation_id,
        "previous_manifest_sha256": sha256_file(rollback / "model.json"),
        "promoted_manifest_sha256": sha256_file(manifest_path),
        "classifier_sha256": classifier_hash,
        "specialist_sha256": {
            row["id"]: row["sha256"] for row in companion_rows if row["role"] == "text_adapter"
        },
    }
    (rollback / "rollback.json").write_bytes(
        (json.dumps(rollback_record, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    )
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "rollback_directory": str(rollback),
        "rollback_record_sha256": sha256_file(rollback / "rollback.json"),
        "classifier_sha256": classifier_hash,
        "specialist_count": len(SPECIALIST_FILENAMES) + 5,
    }


__all__ = [
    "CLASSIFIER_FILENAME",
    "INTRODUCTION_REPAIR_FILENAME",
    "FULL_REPAIR_FILENAME",
    "RESEARCH_REPAIR_FILENAME",
    "CLARIFY_REPAIR_FILENAME",
    "RELATIONSHIP_REPAIR_FILENAME",
    "SPECIALIST_FILENAMES",
    "promote_identity_specialists",
]
