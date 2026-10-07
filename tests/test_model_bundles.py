from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.runtime.model_bundles import ModelBundleRegistry


def _manifest() -> dict:
    return {
        "schema": "salty-steak-model-bundle-v1",
        "id": "general-steak-27b",
        "display_name": "Salty Steak 27B",
        "architecture": "steak20",
        "identity": {"public_architecture_name": "Salty Steak 2"},
        "role": "text_generation",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "artifact": {
            "filename": "model.gguf",
            "format": "GGUF",
            "quantization": "Q4_K_M",
            "size_bytes": 4,
            "sha256": "recorded-hash",
        },
        "context": {
            "architectural_tokens": 262144,
            "configured_tokens": 262144,
            "activation_state": "registered_not_activated",
        },
        "runtime": {
            "family": "salty_native_steak20",
            "engine": "salty_native",
            "state": "engine_build_required",
            "activation_allowed": False,
            "external_service_required": False,
            "reason": "Native kernel is not verified",
        },
        "chat": {"base_model": True, "selected": True, "active": False},
        "integrity": {"state": "sha256_verified_at_import"},
    }


def _vision_companion(**overrides: object) -> dict:
    companion = {
        "id": "base-steak-2-0-vision-projector",
        "display_name": "Base Steak 2.0 Vision",
        "filename": "vision.gguf",
        "role": "vision_projector",
        "format": "GGUF",
        "quantization": "BF16",
        "size_bytes": 6,
        "sha256": "a" * 64,
        "state": "blocked_pending_multimodal_runtime",
        "reason": "The native worker has no image ingestion or projector integration.",
    }
    companion.update(overrides)
    return companion


def _write_bundle(
    tmp_path: Path,
    manifest: dict,
    *,
    companion_bytes: bytes | None = None,
) -> Path:
    bundle = tmp_path / "text-generation" / str(manifest["id"])
    bundle.mkdir(parents=True)
    (bundle / "model.gguf").write_bytes(b"beef")
    if companion_bytes is not None:
        (bundle / "vision.gguf").write_bytes(companion_bytes)
    (bundle / "model.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_registry_preserves_identity_context_and_fail_closed_activation(tmp_path: Path) -> None:
    bundle = tmp_path / "text-generation" / "general-steak-27b"
    bundle.mkdir(parents=True)
    (bundle / "model.gguf").write_bytes(b"beef")
    (bundle / "model.json").write_text(json.dumps(_manifest()), encoding="utf-8")

    record = ModelBundleRegistry(tmp_path).selected_base()

    assert record is not None
    assert record["display_name"] == "Salty Steak 27B"
    assert record["architecture"] == "steak20"
    assert record["public_architecture_name"] == "Salty Steak 2"
    assert record["architectural_context_tokens"] == 262144
    assert record["configured_context_tokens"] == 262144
    assert record["external_service_required"] is False
    assert record["activation_allowed"] is False
    assert record["integrity"] == "verified"


def test_registry_rejects_size_drift_without_hashing_every_start(tmp_path: Path) -> None:
    bundle = tmp_path / "text-generation" / "general-steak-27b"
    bundle.mkdir(parents=True)
    (bundle / "model.gguf").write_bytes(b"changed")
    (bundle / "model.json").write_text(json.dumps(_manifest()), encoding="utf-8")

    record = ModelBundleRegistry(tmp_path).get("general-steak-27b")

    assert record["current_size_matches"] is False
    assert record["integrity"] == "needs_attention"
    assert record["activation_allowed"] is False


def test_registry_exposes_manifest_native_profile_and_context_presets(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "text-generation" / "base-steak-2-0-9b"
    bundle.mkdir(parents=True)
    manifest = _manifest()
    manifest["id"] = "base-steak-2-0-9b"
    manifest["runtime"]["profile"] = {
        "profile_id": "base_steak_2_0_64k",
        "gpu_layers": 99,
        "context_limit": 65536,
        "batch_size": 512,
        "micro_batch_size": 256,
        "kv_precision": "q8_0",
    }
    manifest["context"]["configured_tokens"] = 65536
    manifest["context"]["default_tokens"] = 32768
    manifest["context"]["presets"] = [
        16384,
        24576,
        32768,
        40960,
        49152,
        65536,
    ]
    manifest["chat"]["reasoning_default"] = "instant"
    (bundle / "model.gguf").write_bytes(b"beef")
    (bundle / "model.json").write_text(json.dumps(manifest), encoding="utf-8")

    record = ModelBundleRegistry(tmp_path).get("base-steak-2-0-9b")

    assert record["runtime_profile"]["gpu_layers"] == 99
    assert record["runtime_profile"]["context_limit"] == 65536
    assert record["configured_context_tokens"] == 65536
    assert record["default_context_tokens"] == 32768
    assert record["context_presets"][-1] == 65536
    assert record["reasoning_default"] == "instant"


def test_registry_api_record_exposes_blocked_base_steak_vision_projector(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    manifest.update(
        {
            "id": "base-steak-2-0-9b",
            "display_name": "Base Steak 2.0",
            "architecture": "steak2",
            "companion_artifacts": [_vision_companion()],
        }
    )
    _write_bundle(tmp_path, manifest, companion_bytes=b"vision")

    record = ModelBundleRegistry(tmp_path).get("base-steak-2-0-9b")

    assert record["companion_artifact_count"] == 1
    companion = record["companion_artifacts"][0]
    assert companion["display_name"] == "Base Steak 2.0 Vision"
    assert companion["role"] == "vision_projector"
    assert companion["state"] == "blocked_pending_multimodal_runtime"
    assert companion["file_state"] == "present_size_matches"
    assert companion["current_size_matches"] is True
    assert companion["checksum"] == "a" * 64
    assert companion["scale"] == 1.0
    assert companion["checksum_metadata_valid"] is True
    assert companion["checksum_verification"] == "declared_not_reverified"
    assert companion["runtime_available"] is False
    assert companion["activation_allowed"] is False
    assert companion["auto_activation_allowed"] is False
    assert companion["runtime_loaded"] is False
    assert record["technical_details"]["companion_artifacts"] == [companion]


def test_registry_preserves_a_valid_text_adapter_scale_and_activation(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [
        _vision_companion(
            role="text_adapter",
            scale=0.65,
            activation="identity_intent",
        )
    ]
    _write_bundle(tmp_path, manifest, companion_bytes=b"vision")

    companion = ModelBundleRegistry(tmp_path).get("general-steak-27b")[
        "companion_artifacts"
    ][0]

    assert companion["role"] == "text_adapter"
    assert companion["scale"] == 0.65
    assert companion["activation"] == "identity_intent"


def test_registry_preserves_a_routing_only_adapter(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [
        _vision_companion(
            role="routing_adapter",
            activation="routing_intent",
            scale=1.0,
        )
    ]
    _write_bundle(tmp_path, manifest, companion_bytes=b"vision")

    companion = ModelBundleRegistry(tmp_path).get("general-steak-27b")[
        "companion_artifacts"
    ][0]
    assert companion["role"] == "routing_adapter"
    assert companion["activation"] == "routing_intent"


@pytest.mark.parametrize("activation", ["unknown", "manual", 42])
def test_registry_rejects_invalid_adapter_activation(
    tmp_path: Path,
    activation: object,
) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [
        _vision_companion(role="text_adapter", activation=activation)
    ]
    _write_bundle(tmp_path, manifest, companion_bytes=b"vision")

    assert ModelBundleRegistry(tmp_path).list() == []


def test_registry_accepts_hash_and_tensor_verified_rebuilt_weights(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["integrity"]["state"] = "sha256_and_tensor_payload_verified_at_rebuild"
    _write_bundle(tmp_path, manifest)

    record = ModelBundleRegistry(tmp_path).get("general-steak-27b")

    assert record["integrity"] == "verified"


@pytest.mark.parametrize("scale", [0, -1, 16.1, True, "0.65"])
def test_registry_rejects_an_invalid_adapter_scale(
    tmp_path: Path,
    scale: object,
) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [_vision_companion(scale=scale)]
    _write_bundle(tmp_path, manifest, companion_bytes=b"vision")

    assert ModelBundleRegistry(tmp_path).list() == []


def test_registry_rejects_companion_traversal_outside_bundle(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [
        _vision_companion(filename="../escaped.gguf")
    ]
    bundle = _write_bundle(tmp_path, manifest)
    (bundle.parent / "escaped.gguf").write_bytes(b"vision")

    assert ModelBundleRegistry(tmp_path).list() == []


def test_registry_exposes_missing_companion_without_enabling_it(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [_vision_companion()]
    _write_bundle(tmp_path, manifest)

    companion = ModelBundleRegistry(tmp_path).get("general-steak-27b")[
        "companion_artifacts"
    ][0]

    assert companion["artifact_size_bytes"] is None
    assert companion["current_size_matches"] is False
    assert companion["file_state"] == "missing"
    assert companion["runtime_available"] is False
    assert companion["activation_allowed"] is False


def test_registry_exposes_companion_size_mismatch_without_enabling_it(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [_vision_companion()]
    _write_bundle(tmp_path, manifest, companion_bytes=b"wrong")

    companion = ModelBundleRegistry(tmp_path).get("general-steak-27b")[
        "companion_artifacts"
    ][0]

    assert companion["artifact_size_bytes"] == 5
    assert companion["current_size_matches"] is False
    assert companion["file_state"] == "present_size_mismatch"
    assert companion["runtime_available"] is False
    assert companion["activation_allowed"] is False


@pytest.mark.parametrize("checksum", [None, "f" * 63, "g" * 64])
def test_registry_rejects_missing_or_malformed_companion_sha256(
    tmp_path: Path,
    checksum: str | None,
) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [_vision_companion(sha256=checksum)]
    _write_bundle(tmp_path, manifest, companion_bytes=b"vision")

    assert ModelBundleRegistry(tmp_path).list() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", ""), ("role", "Vision Projector"), ("state", "")],
)
def test_registry_rejects_invalid_companion_role_or_state(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    manifest = _manifest()
    manifest["companion_artifacts"] = [_vision_companion(**{field: value})]
    _write_bundle(tmp_path, manifest, companion_bytes=b"vision")

    assert ModelBundleRegistry(tmp_path).list() == []
