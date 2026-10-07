"""Create a new Base Steak GGUF with corrected, auditable identity metadata.

The source artifact is never edited. Tensor payloads are streamed byte-for-byte
into a new file and verified independently after the copy. Only descriptive
metadata and the architecture/tokenizer namespace are rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import gguf


SOURCE_ARCHITECTURE = "qwen35"
TARGET_ARCHITECTURE = "steak20"
MODEL_NAME = "Base Steak 2.0"
CREATOR = "MD Anik Hasan (Sawlper)"
DISALLOWED_DESCRIPTIVE_TERMS = (
    "qwen",
    "hauhau",
    "alibaba",
    "anthropic",
    "claude",
    "apache",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _field_value(reader: gguf.GGUFReader, key: str) -> Any:
    field = reader.get_field(key)
    return field.contents() if field is not None else None


def _tensor_payload_sha256(reader: gguf.GGUFReader) -> str:
    digest = hashlib.sha256()
    for tensor in reader.tensors:
        digest.update(tensor.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(int(tensor.tensor_type).to_bytes(4, "little"))
        digest.update(memoryview(tensor.data).cast("B"))
    return digest.hexdigest()


def _corrected_metadata() -> dict[str, tuple[gguf.GGUFValueType, Any]]:
    return {
        gguf.Keys.General.NAME: (gguf.GGUFValueType.STRING, MODEL_NAME),
        gguf.Keys.General.BASENAME: (gguf.GGUFValueType.STRING, "Base Steak"),
        gguf.Keys.General.AUTHOR: (gguf.GGUFValueType.STRING, CREATOR),
        gguf.Keys.General.VERSION: (gguf.GGUFValueType.STRING, "2.0"),
        gguf.Keys.General.FINETUNE: (
            gguf.GGUFValueType.STRING,
            "Base Steak 2.0 identity post-training",
        ),
        gguf.Keys.General.DESCRIPTION: (
            gguf.GGUFValueType.STRING,
            f"{MODEL_NAME}, trained by {CREATOR}.",
        ),
        gguf.Keys.General.LICENSE: (gguf.GGUFValueType.STRING, "custom"),
        gguf.Keys.General.LICENSE_NAME: (
            gguf.GGUFValueType.STRING,
            f"{MODEL_NAME} model-weight terms declared by {CREATOR}",
        ),
        gguf.Keys.Tokenizer.PRE: (gguf.GGUFValueType.STRING, TARGET_ARCHITECTURE),
        "base_steak.identity.model_name": (gguf.GGUFValueType.STRING, MODEL_NAME),
        "base_steak.identity.trainer": (gguf.GGUFValueType.STRING, CREATOR),
        "base_steak.ownership.claim": (
            gguf.GGUFValueType.STRING,
            "user_declared",
        ),
        "base_steak.provenance.state": (
            gguf.GGUFValueType.STRING,
            "user_declared_mixed_open_weights_and_training_data",
        ),
    }


def _target_key(key: str) -> str:
    if key.startswith(f"{SOURCE_ARCHITECTURE}."):
        return TARGET_ARCHITECTURE + key[len(SOURCE_ARCHITECTURE) :]
    return key


def _assert_clean_descriptive_metadata(reader: gguf.GGUFReader) -> None:
    failures: list[str] = []
    for key, field in reader.fields.items():
        if key.startswith("tokenizer.ggml.tokens") or key.startswith(
            "tokenizer.ggml.merges"
        ):
            continue
        value = field.contents()
        candidates = [key]
        if isinstance(value, str):
            candidates.append(value)
        combined = " ".join(candidates).casefold()
        if any(term in combined for term in DISALLOWED_DESCRIPTIVE_TERMS):
            failures.append(key)
    if failures:
        raise RuntimeError(
            "Corrected GGUF still contains rejected descriptive metadata: "
            + ", ".join(sorted(failures))
        )


def rewrite(input_path: Path, output_path: Path, expected_sha256: str) -> dict[str, Any]:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite {output_path}")
    if input_path == output_path:
        raise ValueError("Input and output paths must differ")

    source_sha256 = _sha256(input_path)
    if source_sha256 != expected_sha256.casefold():
        raise RuntimeError(
            f"Source hash mismatch: expected {expected_sha256}, got {source_sha256}"
        )

    reader = gguf.GGUFReader(input_path, "r")
    source_architecture = _field_value(reader, gguf.Keys.General.ARCHITECTURE)
    if source_architecture != SOURCE_ARCHITECTURE:
        raise RuntimeError(
            f"Expected {SOURCE_ARCHITECTURE!r}, got {source_architecture!r}"
        )
    if not reader.tensors:
        raise RuntimeError("Source GGUF contains no tensors")

    replacements = _corrected_metadata()
    writer = gguf.GGUFWriter(
        output_path,
        arch=TARGET_ARCHITECTURE,
        endianess=reader.endianess,
    )
    alignment = _field_value(reader, gguf.Keys.General.ALIGNMENT)
    if alignment is not None:
        writer.data_alignment = int(alignment)

    replaced_source_keys = set(replacements)
    for field in reader.fields.values():
        if field.name == gguf.Keys.General.ARCHITECTURE or field.name.startswith("GGUF."):
            continue
        if field.name in replaced_source_keys:
            continue
        target_key = _target_key(field.name)
        value_type = field.types[0]
        sub_type = field.types[-1] if value_type == gguf.GGUFValueType.ARRAY else None
        writer.add_key_value(
            target_key,
            field.contents(),
            value_type,
            sub_type=sub_type,
        )
    for key, (value_type, value) in replacements.items():
        writer.add_key_value(key, value, value_type)

    for tensor in reader.tensors:
        writer.add_tensor_info(
            tensor.name,
            tensor.data.shape,
            tensor.data.dtype,
            tensor.data.nbytes,
            tensor.tensor_type,
        )

    source_tensor_sha256 = _tensor_payload_sha256(reader)
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_ti_data_to_file()
    for tensor in reader.tensors:
        writer.write_tensor_data(tensor.data, tensor_endianess=reader.endianess)
    writer.close()

    output_reader = gguf.GGUFReader(output_path, "r")
    _assert_clean_descriptive_metadata(output_reader)
    output_tensor_sha256 = _tensor_payload_sha256(output_reader)
    if output_tensor_sha256 != source_tensor_sha256:
        raise RuntimeError("Tensor payload identity changed during metadata rewrite")
    if _sha256(input_path) != source_sha256:
        raise RuntimeError("Source artifact changed during metadata rewrite")

    return {
        "schema": "base-steak-identity-rewrite-v1",
        "input_path": str(input_path),
        "input_sha256": source_sha256,
        "output_path": str(output_path),
        "output_sha256": _sha256(output_path),
        "output_size_bytes": output_path.stat().st_size,
        "source_architecture": SOURCE_ARCHITECTURE,
        "target_architecture": TARGET_ARCHITECTURE,
        "model_name": MODEL_NAME,
        "trainer": CREATOR,
        "tensor_count": len(output_reader.tensors),
        "tensor_payload_sha256": output_tensor_sha256,
        "tensor_payloads_identical": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = rewrite(args.input, args.output, args.expected_sha256)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report is not None:
        report_path = args.report.resolve()
        if report_path.exists():
            raise FileExistsError(f"Refusing to overwrite {report_path}")
        report_path.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
