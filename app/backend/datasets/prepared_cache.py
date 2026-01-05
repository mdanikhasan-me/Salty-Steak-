"""Write, verify, query, and safely remove prepared dataset cache artifacts."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.backend.database.control import Database, parse_json
from app.backend.system.files import ensure_within, sha256_file, verify_files

from .errors import DatasetPreparationError


PREPARED_FORMAT_VERSION = 2
SUPPORTED_PREPARED_FORMAT_VERSIONS = frozenset({1, PREPARED_FORMAT_VERSION})
STRICT_V2_FIELDS = frozenset(
    {
        "format",
        "source_paths",
        "source_format",
        "compression",
        "source_digest",
        "parser_version",
        "schema_mapping",
        "preparation_code_digests",
        "tokenizer_digest",
        "tokenizer_vocab_size",
        "template",
        "label_mask",
        "special_token_rules",
        "split_integrity",
        "sequence_construction",
        "packing_rules",
        "truncation_rules",
        "language_filters",
        "quality_filters",
        "deduplication_rules",
        "normalisation_rules",
        "record_count",
        "character_count",
        "token_count",
        "sequence_count",
        "average_sequence_length",
        "truncated_record_count",
        "rejected_record_count",
        "duplicate_record_count",
        "empty_target_count",
        "estimated_dataset_passes",
        "estimated_dataset_passes_note",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _manifest_contract_problems(
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    missing = sorted(field for field in STRICT_V2_FIELDS if field not in manifest)
    if missing:
        problems.append(
            {
                "file": "manifest.json",
                "error": "missing_scientific_contract_fields",
                "fields": missing,
            }
        )
    if manifest.get("format") != "salty-potato-prepared-v2":
        problems.append(
            {
                "file": "manifest.json",
                "error": "invalid_prepared_contract_name",
            }
        )
    for field in ("source_digest", "tokenizer_digest"):
        if not _is_sha256(manifest.get(field)):
            problems.append(
                {
                    "file": "manifest.json",
                    "error": f"invalid_{field}",
                }
            )
    try:
        if int(manifest.get("tokenizer_vocab_size", 0)) < 1:
            raise ValueError
    except (TypeError, ValueError):
        problems.append(
            {
                "file": "manifest.json",
                "error": "invalid_tokenizer_vocab_size",
            }
        )
    code_digests = manifest.get("preparation_code_digests")
    if (
        not isinstance(code_digests, dict)
        or not code_digests
        or any(not _is_sha256(value) for value in code_digests.values())
    ):
        problems.append(
            {
                "file": "manifest.json",
                "error": "invalid_preparation_code_digests",
            }
        )
    split_integrity = manifest.get("split_integrity")
    splits = manifest.get("splits")
    if (
        not isinstance(split_integrity, dict)
        or not isinstance(splits, dict)
        or not isinstance(splits.get("train"), dict)
        or not isinstance(splits.get("validation"), dict)
        or split_integrity.get("overlap_count") != 0
        or split_integrity.get("train_group_digest")
        != splits.get("train", {}).get("split_group_digest")
        or split_integrity.get("validation_group_digest")
        != splits.get("validation", {}).get("split_group_digest")
    ):
        problems.append(
            {
                "file": "manifest.json",
                "error": "invalid_split_integrity",
            }
        )
    if manifest.get("label_mask") not in {
        "assistant_only",
        "terminal_assistant_only",
        "all_tokens",
    }:
        problems.append(
            {
                "file": "manifest.json",
                "error": "invalid_label_mask_contract",
            }
        )
    return problems


def prepared_contract_summary(directory: Path) -> dict[str, Any]:
    """Read only the small manifest for truthful, non-heavy catalog readiness."""

    try:
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        format_version = int(manifest.get("format_version", 0))
        problems = (
            _manifest_contract_problems(manifest)
            if format_version == PREPARED_FORMAT_VERSION
            else [
                {
                    "file": "manifest.json",
                    "error": "legacy_or_unsupported_prepared_contract",
                }
            ]
        )
        return {
            "format_version": format_version,
            "training_eligible": format_version == PREPARED_FORMAT_VERSION
            and not problems,
            "contract_problems": problems,
        }
    except Exception as exc:
        return {
            "format_version": None,
            "training_eligible": False,
            "contract_problems": [
                {"file": "manifest.json", "error": str(exc)}
            ],
        }


def _segment_group(segment: Mapping[str, Any]) -> str | None:
    value = segment.get("split_group_id")
    if value is None:
        value = segment.get("message_tree_id")
    if value is None:
        value = segment.get("source_record_index")
    return None if value is None else str(value)


def write_split(
    directory: Path,
    name: str,
    sequences: list[dict[str, Any]],
    source_record_count: int,
    settings: Mapping[str, Any],
    *,
    pad_token_id: int | None,
) -> dict[str, Any]:
    try:
        import numpy
    except ImportError as exc:
        raise DatasetPreparationError(
            "Prepared arrays require the configured numpy package"
        ) from exc
    sequence_length = int(settings["sequence_length"])
    storage_filler = pad_token_id if settings["padding"] else 0
    if storage_filler is None:
        storage_filler = 0
    inputs = numpy.full(
        (len(sequences), sequence_length),
        int(storage_filler),
        dtype=numpy.int32,
    )
    labels = numpy.full(
        (len(sequences), sequence_length),
        -100,
        dtype=numpy.int32,
    )
    lengths = numpy.zeros((len(sequences),), dtype=numpy.int32)
    provenance: list[dict[str, Any]] = []
    target_count = 0
    truncated_records: set[tuple[Any, Any]] = set()
    group_ids: set[str] = set()
    for index, sequence in enumerate(sequences):
        tokens = list(sequence["tokens"])
        targets = list(sequence["labels"])
        if len(tokens) != len(targets):
            raise DatasetPreparationError(
                "Prepared input and label sequence lengths differ"
            )
        actual = min(len(tokens), sequence_length)
        inputs[index, :actual] = tokens[:actual]
        labels[index, :actual] = targets[:actual]
        lengths[index] = actual
        target_count += sum(
            1 for target in targets[1:actual] if int(target) != -100
        )
        item = dict(sequence.get("provenance") or {})
        item["sequence_index"] = index
        provenance.append(item)
        for segment in item.get("segments", []):
            group = _segment_group(segment)
            if group is not None:
                group_ids.add(str(group))
            if segment.get("truncated"):
                truncated_records.add(
                    (
                        segment.get("message_tree_id"),
                        segment.get("source_record_index"),
                    )
                )
    input_name = f"{name}-input-ids.npy"
    labels_name = f"{name}-labels.npy"
    lengths_name = f"{name}-lengths.npy"
    provenance_name = f"{name}-provenance.jsonl"
    with (directory / input_name).open("wb") as handle:
        numpy.save(handle, inputs, allow_pickle=False)
    with (directory / labels_name).open("wb") as handle:
        numpy.save(handle, labels, allow_pickle=False)
    with (directory / lengths_name).open("wb") as handle:
        numpy.save(handle, lengths, allow_pickle=False)
    with (directory / provenance_name).open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        for item in provenance:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return {
        "source_record_count": source_record_count,
        "sequence_count": len(sequences),
        "token_count": int(lengths.sum()),
        "valid_target_token_count": int(target_count),
        "truncated_record_count": len(truncated_records),
        "split_group_count": len(group_ids),
        "split_group_digest": hashlib.sha256(
            "\n".join(sorted(group_ids)).encode("utf-8")
        ).hexdigest(),
        "input_ids": input_name,
        "labels": labels_name,
        "lengths": lengths_name,
        "provenance": provenance_name,
        "checksums": {
            input_name: sha256_file(directory / input_name),
            labels_name: sha256_file(directory / labels_name),
            lengths_name: sha256_file(directory / lengths_name),
            provenance_name: sha256_file(directory / provenance_name),
        },
    }


def verify_directory(
    directory: Path,
    *,
    tokenizer_fingerprint: str | None,
) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        format_version = int(manifest.get("format_version", 0))
        if format_version not in SUPPORTED_PREPARED_FORMAT_VERSIONS:
            raise ValueError("Unsupported prepared format version")
        if (
            tokenizer_fingerprint is not None
            and manifest.get("tokenizer_fingerprint") != tokenizer_fingerprint
        ):
            raise ValueError("Prepared data tokenizer fingerprint changed")
        expected: dict[str, str] = {}
        verified_groups: dict[str, set[str]] = {}
        verified_semantic_digests: dict[str, set[str]] = {}
        for split in ("train", "validation"):
            split_data = manifest["splits"][split]
            expected.update(split_data["checksums"])
        files_valid, problems = verify_files(directory, expected)
        if not files_valid:
            return {"verified": False, "problems": problems}
        import numpy

        for split in ("train", "validation"):
            split_data = manifest["splits"][split]
            inputs = numpy.load(
                directory / split_data["input_ids"],
                mmap_mode="r",
                allow_pickle=False,
            )
            lengths = numpy.load(
                directory / split_data["lengths"],
                mmap_mode="r",
                allow_pickle=False,
            )
            labels = None
            if format_version >= 2:
                labels = numpy.load(
                    directory / split_data["labels"],
                    mmap_mode="r",
                    allow_pickle=False,
                )
            expected_shape = (
                int(split_data["sequence_count"]),
                int(manifest["settings"]["sequence_length"]),
            )
            if inputs.shape != expected_shape or inputs.dtype != numpy.int32:
                problems.append(
                    {"file": split_data["input_ids"], "error": "invalid_array"}
                )
            if labels is not None and (
                labels.shape != expected_shape or labels.dtype != numpy.int32
            ):
                problems.append(
                    {"file": split_data["labels"], "error": "invalid_labels"}
                )
            if lengths.shape != (expected_shape[0],) or lengths.dtype != numpy.int32:
                problems.append(
                    {"file": split_data["lengths"], "error": "invalid_lengths"}
                )
            elif bool(
                numpy.any(lengths < 2)
                or numpy.any(lengths > expected_shape[1])
                or int(lengths.sum()) != int(split_data["token_count"])
            ):
                problems.append(
                    {"file": split_data["lengths"], "error": "invalid_lengths"}
                )
            elif labels is not None:
                label_contract_invalid = False
                vocab_size = int(manifest.get("tokenizer_vocab_size", 0))
                for row in range(expected_shape[0]):
                    row_length = int(lengths[row])
                    row_inputs = inputs[row, :row_length]
                    row_labels = labels[row, :row_length]
                    active_targets = row_labels != -100
                    if (
                        int(row_labels[0]) != -100
                        or not bool(numpy.any(active_targets[1:]))
                        or bool(numpy.any(row_inputs < 0))
                        or (
                            vocab_size > 0
                            and bool(numpy.any(row_inputs >= vocab_size))
                        )
                        or bool(
                            numpy.any(
                                row_labels[active_targets]
                                != row_inputs[active_targets]
                            )
                        )
                        or bool(numpy.any(labels[row, row_length:] != -100))
                    ):
                        label_contract_invalid = True
                        break
                if label_contract_invalid:
                    problems.append(
                        {
                            "file": split_data["labels"],
                            "error": "invalid_input_label_contract",
                        }
                    )
                target_count = sum(
                    int(
                        numpy.count_nonzero(
                            labels[row, 1 : int(lengths[row])] != -100
                        )
                    )
                    for row in range(expected_shape[0])
                )
                if target_count != int(
                    split_data.get("valid_target_token_count", -1)
                ):
                    problems.append(
                        {
                            "file": split_data["labels"],
                            "error": "invalid_target_token_count",
                        }
                    )
                provenance_path = directory / split_data["provenance"]
                provenance_count = 0
                split_groups: set[str] = set()
                semantic_digests: set[str] = set()
                with provenance_path.open("r", encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        provenance_count += 1
                        try:
                            item = json.loads(line)
                            if (
                                not isinstance(item, dict)
                                or item.get("sequence_index")
                                != provenance_count - 1
                                or not isinstance(item.get("segments"), list)
                            ):
                                raise ValueError("invalid sequence provenance")
                            row_length = int(lengths[provenance_count - 1])
                            for segment in item["segments"]:
                                if not isinstance(segment, dict):
                                    raise ValueError(
                                        "invalid provenance segment"
                                    )
                                start = int(segment["sequence_start"])
                                end = int(segment["sequence_end"])
                                if not 0 <= start < end <= row_length:
                                    raise ValueError(
                                        "provenance segment is out of bounds"
                                    )
                                group = _segment_group(segment)
                                if group is None:
                                    raise ValueError(
                                        "provenance segment has no split group"
                                    )
                                split_groups.add(group)
                                semantic_digest = segment.get(
                                    "semantic_digest"
                                )
                                if semantic_digest is None:
                                    raise ValueError(
                                        "provenance segment has no semantic digest"
                                    )
                                semantic_digests.add(str(semantic_digest))
                                spans = segment.get("turn_token_spans") or []
                                if not isinstance(spans, list):
                                    raise ValueError(
                                        "turn token spans must be a list"
                                    )
                                assistant_spans: list[
                                    Mapping[str, Any]
                                ] = []
                                for span in spans:
                                    if not isinstance(span, dict):
                                        raise ValueError(
                                            "invalid turn token span"
                                        )
                                    span_start = int(span["start"])
                                    span_end = int(span["end"])
                                    if not (
                                        start
                                        <= span_start
                                        < span_end
                                        <= end
                                    ):
                                        raise ValueError(
                                            "turn token span is out of bounds"
                                        )
                                    role = str(span.get("role"))
                                    if (
                                        int(
                                            labels[
                                                provenance_count - 1,
                                                span_start,
                                            ]
                                        )
                                        != -100
                                    ):
                                        raise ValueError(
                                            "role control token is not masked"
                                        )
                                    if role in {"system", "user"} and bool(
                                        numpy.any(
                                            labels[
                                                provenance_count - 1,
                                                span_start:span_end,
                                            ]
                                            != -100
                                        )
                                    ):
                                        raise ValueError(
                                            "non-assistant turn is not masked"
                                        )
                                    if role == "assistant":
                                        assistant_spans.append(span)
                                mask_contract = manifest.get("label_mask")
                                if mask_contract not in {
                                    "assistant_only",
                                    "terminal_assistant_only",
                                }:
                                    continue
                                for assistant_index, span in enumerate(
                                    assistant_spans
                                ):
                                    span_start = int(span["start"])
                                    span_end = int(span["end"])
                                    should_target = (
                                        mask_contract == "assistant_only"
                                        or (
                                            mask_contract
                                            == "terminal_assistant_only"
                                            and assistant_index
                                            == len(assistant_spans) - 1
                                        )
                                    )
                                    content_labels = labels[
                                        provenance_count - 1,
                                        span_start + 1 : span_end,
                                    ]
                                    if should_target and not bool(
                                        numpy.all(
                                            content_labels
                                            == inputs[
                                                provenance_count - 1,
                                                span_start + 1 : span_end,
                                            ]
                                        )
                                    ):
                                        raise ValueError(
                                            "assistant target span is invalid"
                                        )
                                    if not should_target and bool(
                                        numpy.any(content_labels != -100)
                                    ):
                                        raise ValueError(
                                            "non-terminal assistant span "
                                            "contains targets"
                                        )
                        except Exception as exc:
                            problems.append(
                                {
                                    "file": split_data["provenance"],
                                    "line": line_number,
                                    "error": f"invalid_provenance: {exc}",
                                }
                            )
                if provenance_count != expected_shape[0]:
                    problems.append(
                        {
                            "file": split_data["provenance"],
                            "error": "invalid_provenance_count",
                        }
                    )
                verified_groups[split] = split_groups
                verified_semantic_digests[split] = semantic_digests
                digest = hashlib.sha256(
                    "\n".join(sorted(split_groups)).encode("utf-8")
                ).hexdigest()
                if (
                    digest != split_data.get("split_group_digest")
                    or len(split_groups)
                    != int(split_data.get("split_group_count", -1))
                ):
                    problems.append(
                        {
                            "file": split_data["provenance"],
                            "error": "invalid_split_group_identity",
                        }
                    )
        contract_problems: list[dict[str, Any]] = []
        if format_version >= 2:
            contract_problems.extend(_manifest_contract_problems(manifest))
            train_groups = verified_groups.get("train", set())
            validation_groups = verified_groups.get("validation", set())
            actual_overlap = train_groups & validation_groups
            semantic_overlap = verified_semantic_digests.get(
                "train", set()
            ) & verified_semantic_digests.get("validation", set())
            split_integrity = manifest.get("split_integrity") or {}
            if (
                actual_overlap
                or semantic_overlap
                or int(split_integrity.get("train_group_count", -1))
                != len(train_groups)
                or int(split_integrity.get("validation_group_count", -1))
                != len(validation_groups)
                or int(
                    split_integrity.get("train_semantic_digest_count", -1)
                )
                != len(verified_semantic_digests.get("train", set()))
                or int(
                    split_integrity.get(
                        "validation_semantic_digest_count", -1
                    )
                )
                != len(verified_semantic_digests.get("validation", set()))
                or int(
                    split_integrity.get("semantic_overlap_count", -1)
                )
                != len(semantic_overlap)
            ):
                contract_problems.append(
                    {
                        "file": "manifest.json",
                        "error": "provenance_split_leakage",
                        "overlap_count": len(actual_overlap),
                        "semantic_overlap_count": len(semantic_overlap),
                    }
                )
        return {
            "verified": not problems,
            "problems": problems,
            "contract_problems": contract_problems,
            "format_version": format_version,
            "trust": (
                "structural_v2_verified"
                if format_version >= 2
                else "legacy_v1_unmasked"
            ),
            "training_eligible": format_version >= 2
            and not problems
            and not contract_problems,
        }
    except Exception as exc:
        return {
            "verified": False,
            "problems": [{"file": "manifest.json", "error": str(exc)}],
        }


def verify_prepared(
    database: Database,
    prepared_dataset_id: str,
    *,
    tokenizer_fingerprint: str | None,
) -> dict[str, Any]:
    row = database.fetch_one(
        "SELECT * FROM prepared_datasets WHERE id = ?",
        (prepared_dataset_id,),
    )
    if row is None:
        raise KeyError(f"Prepared dataset does not exist: {prepared_dataset_id}")
    path = Path(row["path"])
    verification = verify_directory(
        path,
        tokenizer_fingerprint=tokenizer_fingerprint,
    )
    manifest_checksum = (
        sha256_file(path / "manifest.json")
        if (path / "manifest.json").is_file()
        else None
    )
    if manifest_checksum != row["artifact_checksum"]:
        verification["verified"] = False
        verification["problems"].append(
            {"file": "manifest.json", "error": "database_checksum_mismatch"}
        )
    return {
        "prepared_dataset_id": prepared_dataset_id,
        "path": str(path),
        **verification,
    }


def list_prepared(
    database: Database,
    dataset_id: str | None = None,
    *,
    ready_only: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if dataset_id:
        clauses.append("dataset_id = ?")
        parameters.append(dataset_id)
    if ready_only:
        clauses.extend(["status = 'ready'", "verified = 1"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = database.fetch_all(
        f"SELECT * FROM prepared_datasets {where} ORDER BY created_at DESC",
        parameters,
    )
    for row in rows:
        row["settings"] = parse_json(row.pop("settings_json"))
        row["verified"] = bool(row["verified"])
    return rows


def latest_ready_prepared(
    database: Database,
    dataset_id: str,
) -> dict[str, Any] | None:
    rows = list_prepared(database, dataset_id, ready_only=True)
    return rows[0] if rows else None


def remove_prepared_directory(path: Path, prepared_root: Path) -> None:
    target = ensure_within(path, prepared_root)
    if target == prepared_root:
        raise DatasetPreparationError("Refusing to remove the prepared root")
    if target.exists():
        shutil.rmtree(target)
