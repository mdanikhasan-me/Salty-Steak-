"""Validate the Steak Gen 1 transformer mapping without reading tensor payloads.

This tool deliberately reads only the bounded SafeTensors JSON header.  It
proves that the canonical single-file transformer can be consumed by an
app-owned Steak Gen loader, but it does not claim that the complete image
pipeline is installed or that generation has passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = (
    PROJECT_ROOT
    / "workspace/models/image-generation/steak-gen-1-scaledfp8"
)
DEFAULT_MODEL = DEFAULT_BUNDLE / "Steak gen 1 ScaledFP8.safetensors"
DEFAULT_MANIFEST = DEFAULT_BUNDLE / "model.json"

SAFETENSORS_HEADER_LIMIT = 16 * 1024 * 1024
SOURCE_PREFIX = "model.diffusion_model."
DIM = 3840
HEAD_DIM = 128
FFN_DIM = 10240
ADALN_DIM = 256
CAP_DIM = 2560
MAIN_LAYERS = 30
REFINER_LAYERS = 2
QUANTIZED_MAIN_LAYERS = frozenset(range(1, 29))
QUANTIZED_BLOCK_WEIGHTS = frozenset(
    {
        "adaLN_modulation.0.weight",
        "attention.out.weight",
        "attention.qkv.weight",
        "feed_forward.w1.weight",
        "feed_forward.w2.weight",
        "feed_forward.w3.weight",
    }
)

_DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}


@dataclass(frozen=True)
class TensorHeader:
    dtype: str
    shape: tuple[int, ...]
    data_start: int
    data_end: int

    @property
    def parameter_count(self) -> int:
        return math.prod(self.shape)


@dataclass(frozen=True)
class SafeTensorsHeader:
    path: Path
    file_size: int
    header_length: int
    header_sha256: str
    tensors: Mapping[str, TensorHeader]
    metadata: Mapping[str, str]
    metadata_sha256: str


@dataclass(frozen=True)
class MappedTensor:
    source_key: str
    target_key: str
    dtype: str
    shape: tuple[int, ...]
    row_slice: tuple[int, int] | None = None


def _object_without_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in SafeTensors header: {key}")
        result[key] = value
    return result


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_safetensors_header(
    path: Path, *, max_header_bytes: int = SAFETENSORS_HEADER_LIMIT
) -> SafeTensorsHeader:
    """Read and structurally validate only a bounded SafeTensors header."""

    resolved = path.resolve(strict=True)
    file_size = resolved.stat().st_size
    with resolved.open("rb") as stream:
        length_bytes = stream.read(8)
        if len(length_bytes) != 8:
            raise ValueError("SafeTensors file is shorter than its length prefix")
        header_length = struct.unpack("<Q", length_bytes)[0]
        if header_length < 2 or header_length > max_header_bytes:
            raise ValueError(
                f"SafeTensors header length {header_length} is outside the bounded limit"
            )
        if 8 + header_length > file_size:
            raise ValueError("SafeTensors header extends beyond the artifact")
        header_bytes = stream.read(header_length)
        if len(header_bytes) != header_length:
            raise ValueError("SafeTensors header could not be read completely")

    try:
        document = json.loads(
            header_bytes.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SafeTensors header is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("SafeTensors header root must be an object")

    raw_metadata = document.pop("__metadata__", {})
    if not isinstance(raw_metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw_metadata.items()
    ):
        raise ValueError("SafeTensors metadata must contain string keys and values")

    tensors: dict[str, TensorHeader] = {}
    for key, descriptor in document.items():
        if not isinstance(key, str) or not isinstance(descriptor, dict):
            raise ValueError("SafeTensors tensor descriptors must be named objects")
        dtype = descriptor.get("dtype")
        shape = descriptor.get("shape")
        offsets = descriptor.get("data_offsets")
        if dtype not in _DTYPE_BYTES:
            raise ValueError(f"unsupported or missing dtype for {key}: {dtype!r}")
        if not isinstance(shape, list) or any(
            isinstance(axis, bool) or not isinstance(axis, int) or axis < 0
            for axis in shape
        ):
            raise ValueError(f"invalid shape for {key}")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in offsets)
            or offsets[0] < 0
            or offsets[1] < offsets[0]
        ):
            raise ValueError(f"invalid data offsets for {key}")
        expected_bytes = math.prod(shape) * _DTYPE_BYTES[dtype]
        if offsets[1] - offsets[0] != expected_bytes:
            raise ValueError(
                f"tensor byte span does not match dtype and shape for {key}"
            )
        tensors[key] = TensorHeader(
            dtype=dtype,
            shape=tuple(shape),
            data_start=offsets[0],
            data_end=offsets[1],
        )

    ordered = sorted(tensors.items(), key=lambda item: item[1].data_start)
    next_offset = 0
    for key, descriptor in ordered:
        if descriptor.data_start != next_offset:
            raise ValueError(f"non-contiguous or overlapping tensor layout at {key}")
        next_offset = descriptor.data_end
    payload_size = file_size - 8 - header_length
    if next_offset != payload_size:
        raise ValueError(
            "SafeTensors tensor spans do not cover the declared payload exactly"
        )

    return SafeTensorsHeader(
        path=resolved,
        file_size=file_size,
        header_length=header_length,
        header_sha256=hashlib.sha256(header_bytes).hexdigest(),
        tensors=tensors,
        metadata=dict(raw_metadata),
        metadata_sha256=_canonical_json_sha256(raw_metadata),
    )


def _source_block_contract(
    prefix: str, *, modulation: bool, quantized: bool
) -> dict[str, tuple[str, tuple[int, ...]]]:
    shapes: dict[str, tuple[int, ...]] = {
        "attention.k_norm.weight": (HEAD_DIM,),
        "attention.out.weight": (DIM, DIM),
        "attention.q_norm.weight": (HEAD_DIM,),
        "attention.qkv.weight": (3 * DIM, DIM),
        "attention_norm1.weight": (DIM,),
        "attention_norm2.weight": (DIM,),
        "feed_forward.w1.weight": (FFN_DIM, DIM),
        "feed_forward.w2.weight": (DIM, FFN_DIM),
        "feed_forward.w3.weight": (FFN_DIM, DIM),
        "ffn_norm1.weight": (DIM,),
        "ffn_norm2.weight": (DIM,),
    }
    if modulation:
        shapes.update(
            {
                "adaLN_modulation.0.bias": (4 * DIM,),
                "adaLN_modulation.0.weight": (4 * DIM, ADALN_DIM),
            }
        )
    return {
        prefix + name: (
            "F8_E4M3" if quantized and name in QUANTIZED_BLOCK_WEIGHTS else "BF16",
            shape,
        )
        for name, shape in shapes.items()
    }


def expected_source_contract() -> dict[str, tuple[str, tuple[int, ...]]]:
    """Return the exact canonical source tensor contract, including sidecars."""

    primary: dict[str, tuple[str, tuple[int, ...]]] = {}
    for layer in range(MAIN_LAYERS):
        primary.update(
            _source_block_contract(
                f"{SOURCE_PREFIX}layers.{layer}.",
                modulation=True,
                quantized=layer in QUANTIZED_MAIN_LAYERS,
            )
        )
    for layer in range(REFINER_LAYERS):
        primary.update(
            _source_block_contract(
                f"{SOURCE_PREFIX}noise_refiner.{layer}.",
                modulation=True,
                quantized=False,
            )
        )
        primary.update(
            _source_block_contract(
                f"{SOURCE_PREFIX}context_refiner.{layer}.",
                modulation=False,
                quantized=False,
            )
        )
    primary.update(
        {
            f"{SOURCE_PREFIX}cap_embedder.0.weight": ("BF16", (CAP_DIM,)),
            f"{SOURCE_PREFIX}cap_embedder.1.bias": ("BF16", (DIM,)),
            f"{SOURCE_PREFIX}cap_embedder.1.weight": ("BF16", (DIM, CAP_DIM)),
            f"{SOURCE_PREFIX}cap_pad_token": ("BF16", (1, DIM)),
            f"{SOURCE_PREFIX}final_layer.adaLN_modulation.1.bias": ("BF16", (DIM,)),
            f"{SOURCE_PREFIX}final_layer.adaLN_modulation.1.weight": (
                "BF16",
                (DIM, ADALN_DIM),
            ),
            f"{SOURCE_PREFIX}final_layer.linear.bias": ("BF16", (64,)),
            f"{SOURCE_PREFIX}final_layer.linear.weight": ("BF16", (64, DIM)),
            f"{SOURCE_PREFIX}t_embedder.mlp.0.bias": ("BF16", (1024,)),
            f"{SOURCE_PREFIX}t_embedder.mlp.0.weight": ("BF16", (1024, ADALN_DIM)),
            f"{SOURCE_PREFIX}t_embedder.mlp.2.bias": ("BF16", (ADALN_DIM,)),
            f"{SOURCE_PREFIX}t_embedder.mlp.2.weight": ("BF16", (ADALN_DIM, 1024)),
            f"{SOURCE_PREFIX}x_embedder.bias": ("BF16", (DIM,)),
            f"{SOURCE_PREFIX}x_embedder.weight": ("BF16", (DIM, 64)),
            f"{SOURCE_PREFIX}x_pad_token": ("BF16", (1, DIM)),
        }
    )

    contract = dict(primary)
    for key, (dtype, _shape) in primary.items():
        if dtype != "F8_E4M3":
            continue
        stem = key.removesuffix(".weight")
        contract[f"{stem}.weight_scale"] = ("F32", ())
        contract[f"{stem}.comfy_quant"] = ("U8", (63,))
    return contract


def _target_name(source_key: str) -> str:
    if not source_key.startswith(SOURCE_PREFIX):
        raise ValueError(f"tensor is outside the Steak Gen transformer prefix: {source_key}")
    target = source_key.removeprefix(SOURCE_PREFIX)
    if target.startswith("final_layer."):
        target = "all_final_layer.2-1." + target.removeprefix("final_layer.")
    elif target.startswith("x_embedder."):
        target = "all_x_embedder.2-1." + target.removeprefix("x_embedder.")
    target = target.replace(".attention.out.weight", ".attention.to_out.0.weight")
    target = target.replace(".attention.k_norm.weight", ".attention.norm_k.weight")
    target = target.replace(".attention.q_norm.weight", ".attention.norm_q.weight")
    return target


def map_primary_tensor(source_key: str, tensor: TensorHeader) -> tuple[MappedTensor, ...]:
    """Map one primary source header to Diffusers keys without loading data."""

    target = _target_name(source_key)
    if not target.endswith(".attention.qkv.weight"):
        return (
            MappedTensor(
                source_key=source_key,
                target_key=target,
                dtype=tensor.dtype,
                shape=tensor.shape,
            ),
        )
    if len(tensor.shape) != 2 or tensor.shape[0] % 3:
        raise ValueError(f"fused QKV tensor has an invalid shape: {source_key}")
    rows = tensor.shape[0] // 3
    suffixes = ("to_q", "to_k", "to_v")
    return tuple(
        MappedTensor(
            source_key=source_key,
            target_key=target.replace("qkv", suffix),
            dtype=tensor.dtype,
            shape=(rows, tensor.shape[1]),
            row_slice=(index * rows, (index + 1) * rows),
        )
        for index, suffix in enumerate(suffixes)
    )


def expected_diffusers_state_contract() -> dict[str, tuple[int, ...]]:
    source = expected_source_contract()
    expected: dict[str, tuple[int, ...]] = {}
    for key, (dtype, shape) in source.items():
        if key.endswith(".weight_scale") or key.endswith(".comfy_quant"):
            continue
        tensor = TensorHeader(dtype=dtype, shape=shape, data_start=0, data_end=0)
        for mapped in map_primary_tensor(key, tensor):
            if mapped.target_key in expected:
                raise AssertionError(f"duplicate expected target key: {mapped.target_key}")
            expected[mapped.target_key] = mapped.shape
    return expected


def _mapping_digest(records: Iterable[MappedTensor]) -> str:
    rows = [
        {
            "source": record.source_key,
            "target": record.target_key,
            "dtype": record.dtype,
            "shape": list(record.shape),
            "row_slice": list(record.row_slice) if record.row_slice else None,
        }
        for record in sorted(records, key=lambda item: (item.target_key, item.source_key))
    ]
    return _canonical_json_sha256(rows)


def audit_transformer_header(header: SafeTensorsHeader) -> dict[str, Any]:
    expected_source = expected_source_contract()
    expected_target = expected_diffusers_state_contract()
    issues: list[str] = []
    actual_keys = set(header.tensors)
    expected_keys = set(expected_source)

    for key in sorted(expected_keys - actual_keys):
        issues.append(f"missing source tensor: {key}")
    for key in sorted(actual_keys - expected_keys):
        issues.append(f"unexpected source tensor: {key}")
    for key in sorted(actual_keys & expected_keys):
        expected_dtype, expected_shape = expected_source[key]
        actual = header.tensors[key]
        if actual.dtype != expected_dtype:
            issues.append(
                f"source dtype mismatch: {key}: {actual.dtype} != {expected_dtype}"
            )
        if actual.shape != expected_shape:
            issues.append(
                f"source shape mismatch: {key}: {actual.shape} != {expected_shape}"
            )

    primary_keys = sorted(
        key
        for key in actual_keys
        if not key.endswith(".weight_scale") and not key.endswith(".comfy_quant")
    )
    records: list[MappedTensor] = []
    target_records: dict[str, MappedTensor] = {}
    fused_qkv_count = 0
    for key in primary_keys:
        try:
            mapped_records = map_primary_tensor(key, header.tensors[key])
        except ValueError as exc:
            issues.append(str(exc))
            continue
        if len(mapped_records) == 3:
            fused_qkv_count += 1
        for record in mapped_records:
            if record.target_key in target_records:
                issues.append(f"duplicate mapped target tensor: {record.target_key}")
            target_records[record.target_key] = record
            records.append(record)

    target_keys = set(target_records)
    for key in sorted(set(expected_target) - target_keys):
        issues.append(f"missing mapped Diffusers tensor: {key}")
    for key in sorted(target_keys - set(expected_target)):
        issues.append(f"unexpected mapped Diffusers tensor: {key}")
    for key in sorted(target_keys & set(expected_target)):
        if target_records[key].shape != expected_target[key]:
            issues.append(
                f"mapped shape mismatch: {key}: "
                f"{target_records[key].shape} != {expected_target[key]}"
            )

    scale_sources = sorted(key for key in actual_keys if key.endswith(".weight_scale"))
    quant_sources = sorted(key for key in actual_keys if key.endswith(".comfy_quant"))
    auxiliary_bindings: list[dict[str, object]] = []
    for suffix, keys in ((".weight_scale", scale_sources), (".comfy_quant", quant_sources)):
        for auxiliary_key in keys:
            primary_key = auxiliary_key.removesuffix(suffix) + ".weight"
            primary = header.tensors.get(primary_key)
            if primary is None:
                issues.append(f"orphan quantization auxiliary: {auxiliary_key}")
                continue
            if primary.dtype != "F8_E4M3":
                issues.append(f"quantization auxiliary attached to non-FP8 tensor: {auxiliary_key}")
            for mapped in map_primary_tensor(primary_key, primary):
                auxiliary_bindings.append(
                    {
                        "source": auxiliary_key,
                        "target": mapped.target_key,
                        "kind": suffix.removeprefix("."),
                        "binding": (
                            "shared_fused_qkv_source"
                            if len(map_primary_tensor(primary_key, primary)) == 3
                            else "direct"
                        ),
                    }
                )

    primary_parameters = sum(
        header.tensors[key].parameter_count for key in primary_keys
    )
    dtype_counts: dict[str, int] = {}
    for tensor in header.tensors.values():
        dtype_counts[tensor.dtype] = dtype_counts.get(tensor.dtype, 0) + 1

    return {
        "success": not issues,
        "scope": "header_only_no_tensor_payload",
        "payload_bytes_read": 0,
        "architecture": {
            "family": "ZImageTransformer2DModel",
            "dim": DIM,
            "main_layers": MAIN_LAYERS,
            "refiner_layers": REFINER_LAYERS,
            "attention_heads": 30,
            "head_dim": HEAD_DIM,
            "cap_feature_dim": CAP_DIM,
        },
        "source_contract": {
            "actual_tensor_count": len(header.tensors),
            "expected_tensor_count": len(expected_source),
            "actual_primary_tensor_count": len(primary_keys),
            "expected_primary_tensor_count": 453,
            "primary_parameter_count": primary_parameters,
            "dtype_counts": dict(sorted(dtype_counts.items())),
        },
        "diffusers_mapping": {
            "actual_state_tensor_count": len(target_records),
            "expected_state_tensor_count": len(expected_target),
            "direct_source_tensor_count": len(primary_keys) - fused_qkv_count,
            "fused_qkv_source_count": fused_qkv_count,
            "fused_qkv_target_count": fused_qkv_count * 3,
            "missing_target_count": len(set(expected_target) - target_keys),
            "unexpected_target_count": len(target_keys - set(expected_target)),
            "mapping_sha256": _mapping_digest(records),
            "expected_contract_sha256": _canonical_json_sha256(
                {key: list(shape) for key, shape in sorted(expected_target.items())}
            ),
        },
        "scaled_fp8_auxiliaries": {
            "scale_source_count": len(scale_sources),
            "quant_descriptor_source_count": len(quant_sources),
            "target_binding_count": len(auxiliary_bindings),
            "target_bindings_sha256": _canonical_json_sha256(
                sorted(
                    auxiliary_bindings,
                    key=lambda item: (str(item["target"]), str(item["kind"])),
                )
            ),
            "payload_values_validated": False,
            "payload_values_validation_reason": (
                "This gate intentionally validates headers and bindings only."
            ),
        },
        "embedded_metadata": {
            "keys": sorted(header.metadata),
            "visibility": "technical_only",
            "preserved_unmodified": True,
            "values_exposed_by_report": False,
            "metadata_sha256": header.metadata_sha256,
        },
        "issues": issues,
    }


def validate_bundle_manifest(
    manifest_path: Path, header: SafeTensorsHeader
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = manifest.get("artifact", {})
    identity = manifest.get("identity", {})
    technical = manifest.get("technical", {})
    if artifact.get("filename") != header.path.name:
        issues.append("bundle manifest artifact filename does not match")
    if artifact.get("size_bytes") != header.file_size:
        issues.append("bundle manifest artifact size does not match")
    if identity.get("preserve_embedded_technical_metadata") is not True:
        issues.append("bundle manifest does not preserve embedded technical metadata")
    if technical.get("embedded_metadata_visibility") != "technical_only":
        issues.append("bundle manifest metadata visibility is not technical_only")
    if technical.get("embedded_metadata_preserved_unmodified") is not True:
        issues.append("bundle manifest does not record metadata as preserved")
    if sorted(technical.get("embedded_metadata_keys", [])) != sorted(header.metadata):
        issues.append("bundle manifest embedded metadata key inventory differs")
    return (
        {
            "id": manifest.get("id"),
            "public_model_name": identity.get("public_model_name"),
            "public_architecture_name": identity.get("public_architecture_name"),
            "artifact_filename_matches": artifact.get("filename") == header.path.name,
            "artifact_size_matches": artifact.get("size_bytes") == header.file_size,
            "declared_sha256": artifact.get("sha256"),
            "sha256_status": "declared_not_recomputed_by_header_only_gate",
            "metadata_visibility": technical.get("embedded_metadata_visibility"),
            "metadata_preserved_unmodified": technical.get(
                "embedded_metadata_preserved_unmodified"
            ),
        },
        issues,
    )


def run_validation(model_path: Path, manifest_path: Path | None) -> dict[str, Any]:
    header = read_safetensors_header(model_path)
    report = audit_transformer_header(header)
    report["artifact"] = {
        "path": str(header.path),
        "size_bytes": header.file_size,
        "header_length_bytes": header.header_length,
        "header_sha256": header.header_sha256,
    }
    if manifest_path is not None:
        manifest_report, manifest_issues = validate_bundle_manifest(
            manifest_path.resolve(strict=True), header
        )
        report["bundle_manifest"] = manifest_report
        report["issues"].extend(manifest_issues)
        report["success"] = not report["issues"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Steak Gen 1 ScaledFP8 -> Diffusers Z-Image mapping from "
            "the SafeTensors header only."
        )
    )
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--no-manifest", action="store_true", help="Skip bundle-manifest policy checks."
    )
    args = parser.parse_args()
    try:
        report = run_validation(
            args.model, None if args.no_manifest else args.manifest
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
