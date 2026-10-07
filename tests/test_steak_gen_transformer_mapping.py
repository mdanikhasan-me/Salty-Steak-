from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from tools.validate_steak_gen_transformer import (
    DEFAULT_MANIFEST,
    DEFAULT_MODEL,
    SafeTensorsHeader,
    TensorHeader,
    audit_transformer_header,
    expected_diffusers_state_contract,
    expected_source_contract,
    map_primary_tensor,
    read_safetensors_header,
    run_validation,
)


def _in_memory_canonical_header() -> SafeTensorsHeader:
    tensors = {
        key: TensorHeader(dtype=dtype, shape=shape, data_start=0, data_end=0)
        for key, (dtype, shape) in expected_source_contract().items()
    }
    return SafeTensorsHeader(
        path=Path("Steak gen 1 ScaledFP8.safetensors"),
        file_size=0,
        header_length=0,
        header_sha256="0" * 64,
        tensors=tensors,
        metadata={"prompt": "technical", "workflow": "technical"},
        metadata_sha256="1" * 64,
    )


def test_fused_qkv_maps_to_three_exact_diffusers_projections() -> None:
    source = "model.diffusion_model.layers.4.attention.qkv.weight"
    mapped = map_primary_tensor(
        source,
        TensorHeader("F8_E4M3", (11520, 3840), 10, 20),
    )

    assert [item.target_key for item in mapped] == [
        "layers.4.attention.to_q.weight",
        "layers.4.attention.to_k.weight",
        "layers.4.attention.to_v.weight",
    ]
    assert [item.shape for item in mapped] == [(3840, 3840)] * 3
    assert [item.row_slice for item in mapped] == [
        (0, 3840),
        (3840, 7680),
        (7680, 11520),
    ]


def test_direct_renames_match_diffusers_z_image_module_names() -> None:
    cases = {
        "model.diffusion_model.layers.0.attention.out.weight": (
            "layers.0.attention.to_out.0.weight",
            (3840, 3840),
        ),
        "model.diffusion_model.layers.0.attention.q_norm.weight": (
            "layers.0.attention.norm_q.weight",
            (128,),
        ),
        "model.diffusion_model.layers.0.attention.k_norm.weight": (
            "layers.0.attention.norm_k.weight",
            (128,),
        ),
        "model.diffusion_model.x_embedder.weight": (
            "all_x_embedder.2-1.weight",
            (3840, 64),
        ),
        "model.diffusion_model.final_layer.linear.weight": (
            "all_final_layer.2-1.linear.weight",
            (64, 3840),
        ),
    }
    for source, (target, shape) in cases.items():
        mapped = map_primary_tensor(source, TensorHeader("BF16", shape, 0, 0))
        assert len(mapped) == 1
        assert mapped[0].target_key == target
        assert mapped[0].shape == shape


def test_exact_source_and_diffusers_contract_counts() -> None:
    source = expected_source_contract()
    target = expected_diffusers_state_contract()

    assert len(source) == 789
    assert sum(key.endswith(".weight_scale") for key in source) == 168
    assert sum(key.endswith(".comfy_quant") for key in source) == 168
    assert len(target) == 521
    assert not any(".qkv." in key for key in target)
    assert target["layers.29.attention.to_v.weight"] == (3840, 3840)
    assert target["context_refiner.1.feed_forward.w2.weight"] == (3840, 10240)


def test_complete_header_audit_covers_scaled_fp8_auxiliaries() -> None:
    report = audit_transformer_header(_in_memory_canonical_header())

    assert report["success"] is True
    assert report["payload_bytes_read"] == 0
    assert report["source_contract"]["actual_primary_tensor_count"] == 453
    assert report["source_contract"]["primary_parameter_count"] == 6_154_908_736
    assert report["diffusers_mapping"]["actual_state_tensor_count"] == 521
    assert report["diffusers_mapping"]["fused_qkv_source_count"] == 34
    assert report["diffusers_mapping"]["fused_qkv_target_count"] == 102
    assert report["scaled_fp8_auxiliaries"]["scale_source_count"] == 168
    assert report["scaled_fp8_auxiliaries"]["quant_descriptor_source_count"] == 168
    assert report["scaled_fp8_auxiliaries"]["target_binding_count"] == 448
    assert report["scaled_fp8_auxiliaries"]["payload_values_validated"] is False
    assert report["embedded_metadata"]["visibility"] == "technical_only"
    assert report["embedded_metadata"]["values_exposed_by_report"] is False


def test_complete_header_audit_fails_closed_when_an_fp8_scale_is_missing() -> None:
    header = _in_memory_canonical_header()
    tensors = dict(header.tensors)
    missing = "model.diffusion_model.layers.4.attention.qkv.weight_scale"
    tensors.pop(missing)
    incomplete = SafeTensorsHeader(
        path=header.path,
        file_size=header.file_size,
        header_length=header.header_length,
        header_sha256=header.header_sha256,
        tensors=tensors,
        metadata=header.metadata,
        metadata_sha256=header.metadata_sha256,
    )

    report = audit_transformer_header(incomplete)

    assert report["success"] is False
    assert f"missing source tensor: {missing}" in report["issues"]


def test_complete_header_audit_fails_closed_on_a_mapped_shape_mismatch() -> None:
    header = _in_memory_canonical_header()
    tensors = dict(header.tensors)
    key = "model.diffusion_model.noise_refiner.1.attention.qkv.weight"
    original = tensors[key]
    tensors[key] = TensorHeader(
        dtype=original.dtype,
        shape=(11517, 3840),
        data_start=0,
        data_end=0,
    )
    malformed = SafeTensorsHeader(
        path=header.path,
        file_size=header.file_size,
        header_length=header.header_length,
        header_sha256=header.header_sha256,
        tensors=tensors,
        metadata=header.metadata,
        metadata_sha256=header.metadata_sha256,
    )

    report = audit_transformer_header(malformed)

    assert report["success"] is False
    assert any("source shape mismatch" in issue for issue in report["issues"])
    assert any("mapped shape mismatch" in issue for issue in report["issues"])


def test_header_reader_is_bounded_and_validates_layout_without_payload_decode(
    tmp_path: Path,
) -> None:
    tensor_payload = b"\x00\x01\x02\x03"
    document = {
        "tiny": {"dtype": "U8", "shape": [4], "data_offsets": [0, 4]},
        "__metadata__": {"workflow": "technical-only"},
    }
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    padding = b" " * ((8 - len(encoded) % 8) % 8)
    header_bytes = encoded + padding
    artifact = tmp_path / "tiny.safetensors"
    artifact.write_bytes(struct.pack("<Q", len(header_bytes)) + header_bytes + tensor_payload)

    header = read_safetensors_header(artifact)

    assert header.header_length == len(header_bytes)
    assert header.header_sha256 == hashlib.sha256(header_bytes).hexdigest()
    assert header.tensors["tiny"].shape == (4,)
    assert header.metadata == {"workflow": "technical-only"}


def test_header_reader_rejects_a_span_that_disagrees_with_dtype_and_shape(
    tmp_path: Path,
) -> None:
    document = {
        "bad": {"dtype": "F32", "shape": [2], "data_offsets": [0, 4]},
    }
    encoded = json.dumps(document).encode("utf-8")
    artifact = tmp_path / "bad.safetensors"
    artifact.write_bytes(struct.pack("<Q", len(encoded)) + encoded + b"\x00" * 4)

    with pytest.raises(ValueError, match="byte span"):
        read_safetensors_header(artifact)


@pytest.mark.skipif(
    not DEFAULT_MODEL.exists() or not DEFAULT_MANIFEST.exists(),
    reason="canonical Steak Gen 1 model bundle is not present",
)
def test_canonical_steak_gen_header_and_manifest_pass_without_loading_payload() -> None:
    report = run_validation(DEFAULT_MODEL, DEFAULT_MANIFEST)

    assert report["success"] is True
    assert report["scope"] == "header_only_no_tensor_payload"
    assert report["payload_bytes_read"] == 0
    assert report["artifact"]["size_bytes"] == 7_245_310_296
    assert report["source_contract"]["actual_tensor_count"] == 789
    assert report["diffusers_mapping"]["actual_state_tensor_count"] == 521
    assert report["bundle_manifest"]["metadata_visibility"] == "technical_only"
    assert report["bundle_manifest"]["metadata_preserved_unmodified"] is True
