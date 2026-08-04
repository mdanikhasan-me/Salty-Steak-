"""Atomic promotion of an evaluated native identity adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

from ..system.files import ensure_within, sha256_file


IDENTITY_ADAPTER_FILENAME = "Base-Steak-2.0-Identity-LoRA-v1-F32.gguf"


def promote_identity_candidate(
    *,
    bundle: Mapping[str, Any],
    workflow_result: Mapping[str, Any],
    model_library_root: str | Path,
    backup_root: str | Path,
    operation_id: str,
) -> dict[str, Any]:
    """Replace only the identity adapter and manifest after every gate passes.

    The prior adapter and manifest are copied to a content-addressed rollback
    directory first.  The adapter is replaced before the manifest so a crash
    leaves a checksum mismatch that fails closed instead of silently loading an
    unrecorded artifact.
    """

    if workflow_result.get("passed") is not True:
        raise ValueError("Only a passed identity workflow can be promoted")
    gates = workflow_result.get("gates")
    if not isinstance(gates, Mapping) or not gates or not all(gates.values()):
        raise ValueError("Identity promotion requires every recorded gate")

    technical = bundle.get("technical_details")
    if not isinstance(technical, Mapping):
        raise ValueError("Selected model bundle lacks technical details")
    manifest_path = Path(str(technical.get("manifest_path") or "")).resolve()
    bundle_root = manifest_path.parent
    library_root = Path(model_library_root).resolve()
    ensure_within(manifest_path, library_root)
    if not manifest_path.is_file():
        raise FileNotFoundError("Selected model manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("display_name") != "Base Steak 2.0"
        or manifest.get("architecture") != "steak20"
    ):
        raise ValueError("Identity promotion is limited to the steak20 Base Steak bundle")
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError("Selected model artifact declaration is missing")
    model_path = (bundle_root / str(artifact.get("filename") or "")).resolve()
    ensure_within(model_path, bundle_root)
    expected_model_hash = str(artifact.get("sha256") or "").casefold()
    if (
        expected_model_hash != str(workflow_result.get("model_sha256") or "").casefold()
        or sha256_file(model_path) != expected_model_hash
    ):
        raise RuntimeError("Identity candidate does not belong to the selected model")

    adapter = workflow_result.get("adapter")
    if not isinstance(adapter, Mapping):
        raise ValueError("Identity workflow result lacks an adapter")
    candidate_path = Path(str(adapter.get("path") or "")).resolve()
    candidate_hash = str(adapter.get("sha256") or "").casefold()
    candidate_size = int(adapter.get("size_bytes") or 0)
    if (
        not candidate_path.is_file()
        or candidate_size != candidate_path.stat().st_size
        or sha256_file(candidate_path) != candidate_hash
    ):
        raise RuntimeError("Identity candidate changed after evaluation")

    rollback_directory = (
        Path(backup_root).resolve() / f"{operation_id}-{candidate_hash[:12]}"
    )
    rollback_directory.mkdir(parents=True, exist_ok=False)
    shutil.copy2(manifest_path, rollback_directory / "model.json")
    target_path = (bundle_root / IDENTITY_ADAPTER_FILENAME).resolve()
    ensure_within(target_path, bundle_root)
    if target_path.is_file():
        shutil.copy2(target_path, rollback_directory / target_path.name)
    rollback_record = {
        "operation_id": operation_id,
        "manifest_sha256": sha256_file(rollback_directory / "model.json"),
        "previous_adapter_sha256": (
            sha256_file(rollback_directory / target_path.name)
            if (rollback_directory / target_path.name).is_file()
            else None
        ),
        "candidate_adapter_sha256": candidate_hash,
    }
    (rollback_directory / "rollback.json").write_bytes(
        (json.dumps(rollback_record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )

    staged_adapter = bundle_root / f".{IDENTITY_ADAPTER_FILENAME}.{operation_id}.next"
    shutil.copyfile(candidate_path, staged_adapter)
    if sha256_file(staged_adapter) != candidate_hash:
        staged_adapter.unlink(missing_ok=True)
        raise RuntimeError("Staged identity adapter failed its checksum")
    os.replace(staged_adapter, target_path)

    companion_artifacts = manifest.setdefault("companion_artifacts", [])
    if not isinstance(companion_artifacts, list):
        raise ValueError("Model companion_artifacts must be a list")
    identity_companion = {
        "id": "base-steak-2-0-identity-v1",
        "display_name": "Base Steak 2.0 Identity",
        "filename": IDENTITY_ADAPTER_FILENAME,
        "role": "text_adapter",
        "format": "GGUF",
        "quantization": "F32",
        "size_bytes": candidate_size,
        "sha256": candidate_hash,
        "scale": float(workflow_result["adapter_scale"]),
        "activation": "identity_intent",
        "state": "conditional_post_training_evaluation_passed",
        "reason": (
            "Learned rank-64 identity adapter; the base-model intent controller, "
            "native unseen-generation, and exact capability-retention gates passed "
            "before conditional promotion."
        ),
    }
    replacement_index = next(
        (
            index
            for index, value in enumerate(companion_artifacts)
            if isinstance(value, dict) and value.get("role") == "text_adapter"
        ),
        None,
    )
    if replacement_index is None:
        companion_artifacts.append(identity_companion)
    else:
        companion_artifacts[replacement_index] = identity_companion

    identity = manifest.setdefault("identity", {})
    if not isinstance(identity, dict):
        raise ValueError("Model identity declaration must be an object")
    identity.update(
        {
            "public_model_name": "Base Steak 2.0",
            "public_architecture_name": "steak20",
            "trainer": "MD Anik Hasan (Sawlper)",
            "identity_adapter_required": True,
            "identity_training_report_sha256": str(
                workflow_result["training_report_sha256"]
            ),
            "identity_free_generation_report_sha256": str(
                workflow_result["evaluation_report_sha256"]
            ),
            "identity_adapter_scale": float(workflow_result["adapter_scale"]),
            "identity_adapter_activation": "identity_intent",
            "identity_evaluation_metrics": dict(workflow_result["metrics"]),
        }
    )

    staged_manifest = bundle_root / f".model.json.{operation_id}.next"
    staged_manifest.write_bytes(
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    )
    json.loads(staged_manifest.read_text(encoding="utf-8"))
    os.replace(staged_manifest, manifest_path)
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "adapter_path": str(target_path),
        "adapter_sha256": sha256_file(target_path),
        "rollback_directory": str(rollback_directory),
        "rollback_record_sha256": sha256_file(rollback_directory / "rollback.json"),
    }


__all__ = ["IDENTITY_ADAPTER_FILENAME", "promote_identity_candidate"]
