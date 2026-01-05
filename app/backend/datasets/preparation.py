"""Turn validated records into deterministic, verified token sequences."""

from __future__ import annotations

import math
import hashlib
import os
import random
import tempfile
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.backend.database.control import json_text, new_id, utc_now
from app.backend.operations.manager import OperationContext
from app.backend.system.files import (
    atomic_write_json,
    ensure_within,
    sha256_file,
)
from app.backend.versions.tokenizer import (
    CHAT_TEMPLATE_VERSION,
    canonical_chat_messages,
)

from .errors import DatasetPreparationError, _RecordProblem
from .mapping import mapped_training_record
from .oasst2 import (
    DEFAULT_BRANCH_POLICY,
    reconstruct_oasst2,
)
from .prepared_cache import PREPARED_FORMAT_VERSION
from .source_inspection import _record_events, source_descriptor

if TYPE_CHECKING:
    from .library import DatasetLibrary


@dataclass(slots=True)
class ReadRecordsResult:
    records: list[dict[str, Any]]
    source_record_count: int
    diagnostics: list[dict[str, Any]]


def preparation_settings(
    library: DatasetLibrary,
    settings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    values = {
        "sequence_length": library.default_sequence_length,
        "training_split": 0.9,
        "validation_split": 0.1,
        "packing": True,
        "truncation": "right",
        "random_seed": 104729,
        "bos": True,
        "eos": True,
        "padding": True,
        "cache_location": str(library.prepared_root),
    }
    values.update(library.default_settings)
    if settings:
        values.update(settings)
    result = {
        "sequence_length": int(values["sequence_length"]),
        "training_split": float(values["training_split"]),
        "validation_split": float(values["validation_split"]),
        "packing": bool(values["packing"]),
        "truncation": str(values["truncation"]),
        "random_seed": int(values["random_seed"]),
        "bos": bool(values["bos"]),
        "eos": bool(values["eos"]),
        "padding": bool(values["padding"]),
        "cache_location": str(Path(values["cache_location"]).resolve()),
    }
    if result["sequence_length"] <= 1:
        raise DatasetPreparationError("Sequence length must be greater than one")
    if result["sequence_length"] > library.architectural_context_tokens:
        raise DatasetPreparationError(
            "Sequence length exceeds the architectural context"
        )
    if result["truncation"] not in {"right", "left", "error"}:
        raise DatasetPreparationError("Truncation must be right, left, or error")
    if result["training_split"] <= 0 or result["validation_split"] < 0:
        raise DatasetPreparationError("Dataset splits are invalid")
    if not math.isclose(
        result["training_split"] + result["validation_split"],
        1.0,
        abs_tol=1e-9,
    ):
        raise DatasetPreparationError("Training and validation splits must total 1")
    cache_root = Path(result["cache_location"])
    if cache_root != library.prepared_root:
        raise DatasetPreparationError(
            f"Cache location must be the configured prepared path: {library.prepared_root}"
        )
    if result["bos"] and library.bos_token_id is None:
        raise DatasetPreparationError("BOS is enabled but no BOS token ID is configured")
    if result["eos"] and library.eos_token_id is None:
        raise DatasetPreparationError("EOS is enabled but no EOS token ID is configured")
    if result["padding"] and library.pad_token_id is None:
        raise DatasetPreparationError(
            "Padding is enabled but no padding token ID is configured"
        )
    return result


def prepare_dataset(
    library: DatasetLibrary,
    dataset_id: str,
    settings: Mapping[str, Any] | None = None,
    *,
    context: OperationContext | None = None,
) -> dict[str, Any]:
    if library.encoder is None:
        raise DatasetPreparationError("Tokenizer is unavailable")
    normalized = library._preparation_settings(settings)
    freshness = library.check_source_changed(dataset_id, force_checksum=True)
    if freshness["changed"]:
        raise DatasetPreparationError(
            "The source changed; validate the dataset again before preparation"
        )
    dataset = library.get_dataset(dataset_id)
    assert dataset is not None
    if dataset["mapping"].get("type") in {
        "instruction",
        "conversation",
        "oasst2",
    }:
        normalized = {**normalized, "packing": False}
    if dataset["validation_status"] != "valid":
        raise DatasetPreparationError(
            "The dataset must pass validation before preparation"
        )
    source_checksum = dataset["source_checksum"]
    identifier = new_id()
    final_directory = ensure_within(
        library.prepared_root / dataset_id / identifier,
        library.prepared_root,
    )
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{identifier}.",
            dir=final_directory.parent,
        )
    ).resolve()
    ensure_within(temporary_directory, library.prepared_root)
    library.database.execute(
        """
        UPDATE datasets SET prepared_status = 'preparing', updated_at = ?
        WHERE id = ?
        """,
        (utc_now(), dataset_id),
    )
    try:
        if context:
            context.checkpoint(phase="Reading data")
        read_result = library._read_formatted_records(dataset)
        records = read_result.records
        if not records:
            raise DatasetPreparationError("No usable records remain")
        if context:
            context.checkpoint(
                phase="Tokenising",
                current_progress=0,
                total_progress=len(records),
            )
        tokenised_records = library._tokenize_records(
            records,
            normalized,
            context=context,
            progress_offset=0,
            progress_total=len(records),
        )
        preparable_records: list[dict[str, Any]] = []
        sequence_diagnostics: list[dict[str, Any]] = []
        for tokenised_record in tokenised_records:
            diagnostic = assistant_target_fit_diagnostic(
                tokenised_record, int(normalized["sequence_length"])
            )
            if diagnostic is None:
                preparable_records.append(tokenised_record)
            else:
                sequence_diagnostics.append(diagnostic)
        if not preparable_records:
            raise DatasetPreparationError(
                "No records fit the selected sequence length without truncating "
                "an assistant target"
            )
        if context:
            context.checkpoint(
                phase="Creating splits",
                current_progress=len(records),
                total_progress=len(records),
            )
        train_tokens, validation_tokens = library._split_records(
            preparable_records, normalized
        )
        if context:
            context.checkpoint(
                phase="Packing",
                current_progress=len(records),
                total_progress=len(records),
            )
        train_sequences = library._make_sequences(train_tokens, normalized)
        validation_sequences = library._make_sequences(validation_tokens, normalized)
        if not train_sequences:
            raise DatasetPreparationError("Training split produced no sequences")
        if context:
            context.checkpoint(
                phase="Saving prepared data",
                current_progress=len(records),
                total_progress=len(records),
            )
        split_manifest = {
            "train": library._write_split(
                temporary_directory,
                "train",
                train_sequences,
                len(train_tokens),
                normalized,
            ),
            "validation": library._write_split(
                temporary_directory,
                "validation",
                validation_sequences,
                len(validation_tokens),
                normalized,
            ),
        }
        token_count = (
            split_manifest["train"]["token_count"]
            + split_manifest["validation"]["token_count"]
        )
        train_groups = {
            str(segment.get("split_group_id"))
            for sequence in train_sequences
            for segment in sequence.get("provenance", {}).get("segments", [])
            if segment.get("split_group_id") is not None
        }
        validation_groups = {
            str(segment.get("split_group_id"))
            for sequence in validation_sequences
            for segment in sequence.get("provenance", {}).get("segments", [])
            if segment.get("split_group_id") is not None
        }
        overlap = train_groups & validation_groups
        if overlap:
            raise DatasetPreparationError(
                "Prepared split leakage detected for groups: "
                + ", ".join(sorted(overlap)[:5])
            )
        train_semantic_digests = {
            str(segment.get("semantic_digest"))
            for sequence in train_sequences
            for segment in sequence.get("provenance", {}).get("segments", [])
            if segment.get("semantic_digest") is not None
        }
        validation_semantic_digests = {
            str(segment.get("semantic_digest"))
            for sequence in validation_sequences
            for segment in sequence.get("provenance", {}).get("segments", [])
            if segment.get("semantic_digest") is not None
        }
        semantic_overlap = train_semantic_digests & validation_semantic_digests
        if semantic_overlap:
            raise DatasetPreparationError(
                "Prepared split leakage detected for exact semantic duplicates: "
                + ", ".join(sorted(semantic_overlap)[:5])
            )
        tokenizer_model = Path(str(library.tokenizer_reference or ""))
        if tokenizer_model.is_dir():
            tokenizer_model = tokenizer_model / "tokenizer.model"
        tokenizer_model_digest = (
            sha256_file(tokenizer_model) if tokenizer_model.is_file() else None
        )
        semantic_digests = [
            str(record["semantic_digest"]) for record in preparable_records
        ]
        duplicate_record_count = len(semantic_digests) - len(
            set(semantic_digests)
        )
        descriptor = source_descriptor(Path(dataset["source_path"]))
        manifest = {
            "format_version": PREPARED_FORMAT_VERSION,
            "format": "salty-potato-prepared-v2",
            "prepared_dataset_id": identifier,
            "dataset_id": dataset_id,
            "source_path": dataset["source_path"],
            "source_paths": [dataset["source_path"]],
            "source_format": dataset["format"],
            "compression": descriptor.compression,
            "archive_member": descriptor.archive_member,
            "source_checksum": source_checksum,
            "source_digest": source_checksum,
            "license": dataset.get("license"),
            "tokenizer_reference": library.tokenizer_reference,
            "tokenizer_fingerprint": library.tokenizer_fingerprint,
            "tokenizer_digest": tokenizer_model_digest,
            "tokenizer_vocab_size": int(
                getattr(library.tokenizer, "vocab_size", 0)
            ),
            "mapping": dataset["mapping"],
            "schema_mapping": dataset["mapping"],
            "parser_version": (
                "oasst2-tree-adapter-v1"
                if dataset["mapping"].get("type") == "oasst2"
                else "salty-dataset-mapping-v2"
            ),
            "preparation_code_digests": {
                "preparation.py": sha256_file(Path(__file__)),
                "mapping.py": sha256_file(Path(__file__).with_name("mapping.py")),
                "oasst2.py": sha256_file(Path(__file__).with_name("oasst2.py")),
                "tokenizer.py": sha256_file(
                    Path(__file__).parent.parent / "versions" / "tokenizer.py"
                ),
            },
            "template": CHAT_TEMPLATE_VERSION,
            "label_mask": (
                "terminal_assistant_only"
                if dataset["mapping"].get("type") == "oasst2"
                else "assistant_only"
                if dataset["mapping"].get("type")
                in {"instruction", "conversation"}
                else "all_tokens"
            ),
            "special_token_rules": {
                "conversation_bos": "exactly_once",
                "message_eos": "exactly_once",
                "role_tokens": "masked_for_assistant_only_loss",
                "padding_label": -100,
            },
            "language_filters": [
                item.strip()
                for item in str(dataset["mapping"].get("languages", "")).split(",")
                if item.strip()
            ],
            "quality_filters": {
                "deleted_messages": "reject",
                "negative_reviews": (
                    "include"
                    if str(
                        dataset["mapping"].get(
                            "include_negative_reviews", "false"
                        )
                    ).casefold()
                    in {"1", "true", "yes"}
                    else "reject"
                ),
            },
            "branch_selection_rules": (
                "minimise assistant rank sum, then worst rank; prefer more "
                "ranked assistant turns; maximise mean OASST quality/helpfulness; "
                "prefer longer branch; final message ID is deterministic fallback"
                if dataset["mapping"].get("type") == "oasst2"
                else None
            ),
            "deduplication_rules": (
                "retain exact semantic duplicates; co-group every source group "
                "sharing a semantic digest before train/validation assignment"
            ),
            "normalisation_rules": (
                "preserve source text exactly; whitespace-only required fields "
                "reject; prepend the canonical product system prefix to any "
                "source system content without trimming that content"
            ),
            "settings": normalized,
            "source_record_count": read_result.source_record_count,
            "raw_source_record_count": read_result.source_record_count,
            "selected_branch_count": len(
                {
                    (
                        record["provenance"].get("message_tree_id"),
                        record["provenance"].get("branch_id"),
                    )
                    for record in records
                    if record["provenance"].get("mapping_type") == "oasst2"
                }
            ),
            "generated_target_window_count": len(records),
            "accepted_target_window_count": len(preparable_records),
            "rejected_target_window_count": len(sequence_diagnostics),
            "record_count": len(preparable_records),
            "preparable_record_count": len(preparable_records),
            "token_count": token_count,
            "valid_target_token_count": sum(
                int(split_manifest[name]["valid_target_token_count"])
                for name in ("train", "validation")
            ),
            "character_count": sum(
                int(record.get("character_count", 0))
                for record in preparable_records
            ),
            "sequence_count": sum(
                int(split_manifest[name]["sequence_count"])
                for name in ("train", "validation")
            ),
            "average_sequence_length": (
                token_count
                / max(
                    1,
                    sum(
                        int(split_manifest[name]["sequence_count"])
                        for name in ("train", "validation")
                    ),
                )
            ),
            "truncated_record_count": sum(
                int(split_manifest[name]["truncated_record_count"])
                for name in ("train", "validation")
            ),
            "rejected_record_count": (
                len(read_result.diagnostics) + len(sequence_diagnostics)
            ),
            "duplicate_record_count": duplicate_record_count,
            "empty_target_count": sum(
                1
                for item in read_result.diagnostics
                if item.get("code") == "NO_VALID_ASSISTANT_TARGETS"
            ),
            "rejection_diagnostics": (
                [*read_result.diagnostics, *sequence_diagnostics]
            )[:100],
            "sequence_construction": (
                "whole semantic records packed without splitting"
                if normalized["packing"]
                else "one semantic record per sequence"
            ),
            "packing_rules": {
                "enabled": normalized["packing"],
                "split_records": False,
                "cross_record_bos_target": "masked",
            },
            "truncation_rules": {
                "plain_text": normalized["truncation"],
                "assistant_only": (
                    "preserve canonical system turn, final assistant target, "
                    "and nearest user boundary; truncate user prompt tail only; "
                    "reject the target window if the full target cannot fit"
                ),
            },
            "oasst2_target_policy": (
                "one_prefix_window_per_assistant_target"
                if dataset["mapping"].get("type") == "oasst2"
                else None
            ),
            "long_target_policy": "reject_target_window_preserve_full_target",
            "long_target_rejected_count": len(sequence_diagnostics),
            "long_target_rejected_token_count": sum(
                int(item.get("assistant_target_tokens", 0))
                for item in sequence_diagnostics
            ),
            "assistant_target_coverage": {
                "accepted_tokens": sum(
                    int(split_manifest[name]["valid_target_token_count"])
                    for name in ("train", "validation")
                ),
                "rejected_tokens": sum(
                    int(item.get("assistant_target_tokens", 0))
                    for item in sequence_diagnostics
                ),
            },
            "train_split": normalized["training_split"],
            "validation_split": normalized["validation_split"],
            "split_group": (
                "message_tree_id plus exact-semantic duplicate components"
                if dataset["mapping"].get("type") == "oasst2"
                else "source_record plus exact-semantic duplicate components"
            ),
            "split_integrity": {
                "train_group_count": len(train_groups),
                "validation_group_count": len(validation_groups),
                "overlap_count": 0,
                "train_semantic_digest_count": len(train_semantic_digests),
                "validation_semantic_digest_count": len(
                    validation_semantic_digests
                ),
                "semantic_overlap_count": 0,
                "train_group_digest": split_manifest["train"][
                    "split_group_digest"
                ],
                "validation_group_digest": split_manifest["validation"][
                    "split_group_digest"
                ],
            },
            "estimated_dataset_passes": None,
            "estimated_dataset_passes_note": (
                "No training budget is chosen during preparation; calculate as "
                "(optimizer_steps * micro_batch_size * gradient_accumulation * "
                "sequence_length) / train_token_count on the training review."
            ),
            "splits": split_manifest,
            "created_at": utc_now(),
        }
        atomic_write_json(temporary_directory / "manifest.json", manifest)
        if context:
            context.checkpoint(
                phase="Verifying",
                current_progress=len(records),
                total_progress=len(records),
            )
        verification = library._verify_directory(temporary_directory)
        if not verification.get("training_eligible"):
            raise DatasetPreparationError(
                "Prepared output verification failed: "
                f"{verification.get('problems', [])}; "
                f"{verification.get('contract_problems', [])}"
            )
        if sha256_file(Path(dataset["source_path"])) != source_checksum:
            library._mark_source_stale(dataset_id)
            raise DatasetPreparationError(
                "The source changed while preparation was running"
            )
        if final_directory.exists():
            raise DatasetPreparationError(
                f"Prepared output ID already exists: {identifier}"
            )
        os.replace(temporary_directory, final_directory)
        artifact_checksum = sha256_file(final_directory / "manifest.json")
        operation_id = context.operation_id if context else None
        now = utc_now()
        with library.database.transaction() as connection:
            current = connection.execute(
                "SELECT source_checksum FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()
            if current is None or current["source_checksum"] != source_checksum:
                raise DatasetPreparationError(
                    "Dataset source identity changed before registration"
                )
            connection.execute(
                """
                INSERT INTO prepared_datasets(
                    id, dataset_id, operation_id, source_checksum, path,
                    settings_json, tokenizer_reference, record_count,
                    token_count, train_record_count, validation_record_count,
                    artifact_checksum, verified, status, created_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'ready', ?, ?)
                """,
                (
                    identifier,
                    dataset_id,
                    operation_id,
                    source_checksum,
                    str(final_directory),
                    json_text(normalized),
                    library.tokenizer_reference,
                    len(preparable_records),
                    token_count,
                    len(train_sequences),
                    len(validation_sequences),
                    artifact_checksum,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE datasets
                SET prepared_status = 'ready',
                    source_changed = 0,
                    token_count = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (token_count, now, dataset_id),
            )
        result = {
            "prepared_dataset_id": identifier,
            "dataset_id": dataset_id,
            "path": str(final_directory),
            "record_count": len(preparable_records),
            "generated_target_window_count": len(records),
            "rejected_target_window_count": len(sequence_diagnostics),
            "token_count": token_count,
            "train_record_count": len(train_sequences),
            "validation_record_count": len(validation_sequences),
            "train_sequence_count": split_manifest["train"]["sequence_count"],
            "validation_sequence_count": split_manifest["validation"][
                "sequence_count"
            ],
            "artifact_checksum": artifact_checksum,
            "verified": True,
            "status": "ready",
        }
        if context:
            context.checkpoint(
                phase="Ready",
                current_progress=len(records),
                total_progress=len(records),
                details=result,
            )
        return result
    except Exception:
        if temporary_directory.exists():
            library._remove_prepared_directory(temporary_directory)
        library.database.execute(
            """
            UPDATE datasets
            SET prepared_status = CASE
                    WHEN source_changed = 1 THEN 'stale' ELSE 'failed' END,
                updated_at = ?
            WHERE id = ? AND prepared_status = 'preparing'
            """,
            (utc_now(), dataset_id),
        )
        raise


def read_formatted_records(dataset: Mapping[str, Any]) -> ReadRecordsResult:
    path = Path(str(dataset["source_path"]))
    raw_records: list[tuple[int, Any]] = []
    for event in _record_events(path, dataset["format"], dataset["encoding"]):
        if event.error is not None:
            raise DatasetPreparationError(
                f"Record {event.index} became malformed: {event.error}"
            )
        raw_records.append((event.index, event.record))
    mapping = dataset["mapping"]
    result: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    if mapping.get("type") == "oasst2":
        branches = reconstruct_oasst2(
            [record for _index, record in raw_records],
            allowed_languages=[
                item.strip()
                for item in str(mapping.get("languages", "")).split(",")
                if item.strip()
            ]
            or None,
            include_negative_reviews=str(
                mapping.get("include_negative_reviews", "false")
            ).casefold()
            in {"1", "true", "yes"},
            branch_policy=str(
                mapping.get("branch_policy", DEFAULT_BRANCH_POLICY)
            ),
            skip_rejected=True,
            diagnostics=diagnostics,
        )
        if not branches:
            raise DatasetPreparationError(
                "NO_VALID_ASSISTANT_TARGETS: OASST2 adapter produced no usable trees"
            )
        for branch in branches:
            messages = branch.chat_messages()
            assistant_positions = [
                index
                for index, message in enumerate(messages)
                if message["role"] == "assistant"
            ]
            for target_ordinal, target_position in enumerate(
                assistant_positions, start=1
            ):
                target_messages = messages[: target_position + 1]
                target_source_messages = branch.messages[: target_position + 1]
                target_message = target_source_messages[-1]
                result.append(
                    {
                        "formatted_text": "\n\n".join(
                            f"{message['role'].capitalize()}:\n"
                            f"{message['content']}"
                            for message in target_messages
                        ),
                        "messages": target_messages,
                        "assistant_only_labels": True,
                        "terminal_assistant_only_labels": True,
                        "group_id": branch.tree_id,
                        "provenance": {
                            "source_record_index": (
                                branch.source_record_indices[0]
                                if branch.source_record_indices
                                else None
                            ),
                            "source_record_indices": list(
                                branch.source_record_indices
                            ),
                            "mapping_type": "oasst2",
                            "message_tree_id": branch.tree_id,
                            "branch_id": branch.branch_id,
                            "message_ids": [
                                message.message_id
                                for message in target_source_messages
                            ],
                            "target_message_id": target_message.message_id,
                            "target_ordinal": target_ordinal,
                            "target_count_in_branch": len(
                                assistant_positions
                            ),
                            "branch_policy": branch.branch_policy,
                            "target_policy": (
                                "one_prefix_window_per_assistant_target"
                            ),
                            "source_layout": branch.source_layout,
                        },
                    }
                )
    else:
        for index, record in raw_records:
            try:
                result.append(
                    mapped_training_record(
                        record,
                        mapping,
                        source_record_index=index,
                    )
                )
            except _RecordProblem as exc:
                raise DatasetPreparationError(
                    f"Record {index} no longer passes validation: {exc}"
                ) from exc
    for record in result:
        record["character_count"] = len(record["formatted_text"])
    return ReadRecordsResult(
        records=result,
        source_record_count=len(raw_records),
        diagnostics=diagnostics,
    )


def split_records(
    records: list[dict[str, Any]],
    settings: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    original_group_ids: list[str] = []
    for index, record in enumerate(records):
        original_group_ids.append(
            str(record.get("group_id") or f"record:{index}")
        )




    parent = {group_id: group_id for group_id in original_group_ids}

    def find(group_id: str) -> str:
        root = group_id
        while parent[root] != root:
            root = parent[root]
        while parent[group_id] != group_id:
            next_group = parent[group_id]
            parent[group_id] = root
            group_id = next_group
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        keep, merge = sorted((left_root, right_root))
        parent[merge] = keep

    digest_owner: dict[str, str] = {}
    for index, record in enumerate(records):
        semantic_digest = record.get("semantic_digest")
        if semantic_digest is None:
            continue
        digest = str(semantic_digest)
        group_id = original_group_ids[index]
        owner = digest_owner.setdefault(digest, group_id)
        union(group_id, owner)

    component_members: dict[str, set[str]] = {}
    for group_id in parent:
        component_members.setdefault(find(group_id), set()).add(group_id)
    component_ids = {
        root: "semantic-component:"
        + hashlib.sha256(
            "\n".join(sorted(members)).encode("utf-8")
        ).hexdigest()
        for root, members in component_members.items()
    }
    split_group_by_original = {
        group_id: component_ids[find(group_id)] for group_id in parent
    }
    prepared_records: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        source_group_id = original_group_ids[index]
        split_group_id = split_group_by_original[source_group_id]
        prepared_record = {
            **record,
            "split_group_id": split_group_id,
            "provenance": {
                **dict(record.get("provenance") or {}),
                "source_split_group_id": source_group_id,
                "split_group_id": split_group_id,
                "semantic_digest": record.get("semantic_digest"),
            },
        }
        prepared_records.append(prepared_record)
        groups.setdefault(split_group_id, []).append(prepared_record)

    group_ids = sorted(groups)
    random.Random(int(settings["random_seed"])).shuffle(group_ids)
    validation_count = round(
        len(group_ids) * float(settings["validation_split"])
    )
    if len(group_ids) > 1 and settings["validation_split"] > 0:
        validation_count = max(1, validation_count)
    validation_count = min(validation_count, max(0, len(group_ids) - 1))
    validation_groups = set(group_ids[:validation_count])
    train = [
        record
        for record in prepared_records
        if str(record["split_group_id"]) not in validation_groups
    ]
    validation = [
        record
        for record in prepared_records
        if str(record["split_group_id"]) in validation_groups
    ]
    return train, validation


def tokenize_records(
    library: DatasetLibrary,
    records: list[dict[str, Any]],
    settings: Mapping[str, Any],
    *,
    context: OperationContext | None,
    progress_offset: int,
    progress_total: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for offset, record in enumerate(records, start=1):
        if context and (offset == 1 or offset % 100 == 0):
            context.checkpoint(
                phase="Tokenising",
                current_progress=progress_offset + offset - 1,
                total_progress=progress_total,
            )
        messages = record.get("messages")
        if messages is not None and library.tokenizer is not None:
            if not settings["bos"] or not settings["eos"]:
                raise DatasetPreparationError(
                    "The semantic Chat template requires BOS and per-message EOS "
                    "boundaries; enable both settings"
                )
            tokens, labels = library.tokenizer.conversation_example(
                messages,
                2**31 - 1,
                assistant_only_labels=bool(record["assistant_only_labels"]),
            )
            semantic_messages = canonical_chat_messages(messages)
            inserted_system = not (
                messages and messages[0].get("role") == "system"
            )
            source_message_ids = list(
                record.get("provenance", {}).get("message_ids") or []
            )
            source_id_cursor = 0
            turn_spans: list[dict[str, Any]] = []
            cursor = 1
            for message_index, message in enumerate(semantic_messages):
                turn_length = 1 + len(
                    library.tokenizer.encode(
                        str(message["content"]), add_eos=True
                    )
                )
                turn_spans.append(
                    {
                        "role": str(message["role"]),
                        "start": cursor,
                        "end": cursor + turn_length,
                        "message_id": (
                            None
                            if inserted_system and message_index == 0
                            else source_message_ids[source_id_cursor]
                            if source_id_cursor < len(source_message_ids)
                            else None
                        ),
                    }
                )
                if not (inserted_system and message_index == 0):
                    source_id_cursor += 1
                cursor += turn_length
            if record.get("terminal_assistant_only_labels"):
                terminal_assistant = next(
                    (
                        span
                        for span in reversed(turn_spans)
                        if span["role"] == "assistant"
                    ),
                    None,
                )
                if terminal_assistant is None:
                    raise DatasetPreparationError(
                        "NO_VALID_ASSISTANT_TARGETS: target window has no "
                        "terminal assistant turn"
                    )
                labels[: int(terminal_assistant["start"])] = [
                    -100
                ] * int(terminal_assistant["start"])
        else:
            tokens = library._encode(str(record["formatted_text"]))
            if settings["bos"]:
                assert library.bos_token_id is not None
                tokens.insert(0, library.bos_token_id)
            if settings["eos"]:
                assert library.eos_token_id is not None
                tokens.append(library.eos_token_id)
            labels = list(tokens)
            if labels:
                labels[0] = -100
            turn_spans = []
        if len(tokens) < 2:
            raise DatasetPreparationError(
                "A prepared example needs at least two tokens for shifted labels"
            )
        if len(labels) != len(tokens):
            raise DatasetPreparationError(
                "Tokenizer returned different input and label lengths"
            )
        if bool(record["assistant_only_labels"]) and not any(
            label != -100 for label in labels[1:]
        ):
            raise DatasetPreparationError(
                "NO_VALID_ASSISTANT_TARGETS: record has no assistant target tokens"
            )
        semantic_digest = hashlib.sha256(
            str(record["formatted_text"]).encode("utf-8")
        ).hexdigest()
        result.append(
            {
                "tokens": tokens,
                "labels": labels,
                "semantic_digest": semantic_digest,
                "provenance": {
                    **record["provenance"],
                    "semantic_digest": semantic_digest,
                    "turn_token_spans": turn_spans,
                },
                "group_id": record["group_id"],
                "character_count": record["character_count"],
                "assistant_only_labels": bool(record["assistant_only_labels"]),
                "turn_spans": turn_spans,
            }
        )
    return result


def assistant_target_fit_diagnostic(
    record: Mapping[str, Any],
    sequence_length: int,
) -> dict[str, Any] | None:
    """Return why a semantic record cannot preserve its final target."""

    if not record.get("assistant_only_labels"):
        return None
    tokens = list(record["tokens"])
    if len(tokens) <= sequence_length:
        return None
    turn_spans = list(record.get("turn_spans") or [])
    assistant_indices = [
        index
        for index, span in enumerate(turn_spans)
        if span.get("role") == "assistant"
    ]
    if not assistant_indices:
        return {
            "code": "NO_VALID_ASSISTANT_TARGETS",
            "reason": "conversation has no assistant turn",
            **dict(record.get("provenance") or {}),
        }
    assistant_index = assistant_indices[-1]
    user_index = next(
        (
            index
            for index in range(assistant_index - 1, -1, -1)
            if turn_spans[index].get("role") == "user"
        ),
        None,
    )
    if user_index is None:
        return {
            "code": "NO_VALID_ASSISTANT_TARGETS",
            "reason": "final assistant target has no user context",
            **dict(record.get("provenance") or {}),
        }
    system_index = next(
        (
            index
            for index, span in enumerate(turn_spans)
            if span.get("role") == "system"
        ),
        None,
    )
    assistant_span = turn_spans[assistant_index]
    assistant_turn_tokens = int(assistant_span["end"]) - int(
        assistant_span["start"]
    )
    user_span = turn_spans[user_index]
    user_turn_tokens = int(user_span["end"]) - int(user_span["start"])
    minimum_user_turn_tokens = 2 + min(
        8, max(0, user_turn_tokens - 2)
    )
    system_turn_tokens = (
        int(turn_spans[system_index]["end"])
        - int(turn_spans[system_index]["start"])
        if system_index is not None
        else 0
    )
    required = (
        1
        + system_turn_tokens
        + minimum_user_turn_tokens
        + assistant_turn_tokens
    )
    if required <= sequence_length:
        return None
    return {
        "code": "ASSISTANT_TARGET_EXCEEDS_SEQUENCE",
        "policy": "reject_target_window_preserve_full_target",
        "sequence_length": int(sequence_length),
        "required_minimum_tokens": required,
        "assistant_turn_tokens": assistant_turn_tokens,
        "assistant_target_tokens": max(0, assistant_turn_tokens - 1),
        "system_turn_tokens": system_turn_tokens,
        "minimum_user_turn_tokens": minimum_user_turn_tokens,
        **dict(record.get("provenance") or {}),
    }


def make_sequences(
    records: list[dict[str, Any]],
    settings: Mapping[str, Any],
) -> list[dict[str, Any]]:
    length = int(settings["sequence_length"])
    normalised: list[dict[str, Any]] = []
    for record in records:
        tokens = list(record["tokens"])
        labels = list(record["labels"])
        truncated = False
        if len(tokens) > length:
            if settings["truncation"] == "error":
                raise DatasetPreparationError(
                    "An example exceeds sequence length and truncation is disabled"
                )
            truncated = True
            turn_spans = list(record.get("turn_spans") or [])
            if record.get("assistant_only_labels") and turn_spans:
                fit_problem = assistant_target_fit_diagnostic(record, length)
                if fit_problem is not None:
                    raise DatasetPreparationError(
                        "ASSISTANT_TARGET_EXCEEDS_SEQUENCE: rejecting this "
                        "target window is required to preserve the full target"
                    )
                assistant_indices = [
                    index
                    for index, span in enumerate(turn_spans)
                    if span["role"] == "assistant"
                ]
                if not assistant_indices:
                    raise DatasetPreparationError(
                        "NO_VALID_ASSISTANT_TARGETS: conversation has no assistant turn"
                    )
                assistant_index = assistant_indices[-1]
                user_index = next(
                    (
                        index
                        for index in range(assistant_index - 1, -1, -1)
                        if turn_spans[index]["role"] == "user"
                    ),
                    None,
                )
                if user_index is None:
                    raise DatasetPreparationError(
                        "NO_VALID_ASSISTANT_TARGETS: assistant target has no user context"
                    )
                assistant_span = turn_spans[assistant_index]
                assistant_tokens = tokens[
                    assistant_span["start"] : assistant_span["end"]
                ]
                assistant_labels = labels[
                    assistant_span["start"] : assistant_span["end"]
                ]
                system_index = next(
                    (
                        index
                        for index, span in enumerate(turn_spans)
                        if span["role"] == "system"
                    ),
                    None,
                )
                system_tokens = (
                    tokens[
                        turn_spans[system_index]["start"] : turn_spans[
                            system_index
                        ]["end"]
                    ]
                    if system_index is not None
                    else []
                )
                system_labels = [-100] * len(system_tokens)
                remaining = (
                    length - 1 - len(system_tokens) - len(assistant_tokens)
                )
                user_span = turn_spans[user_index]
                user_tokens = tokens[user_span["start"] : user_span["end"]]
                user_labels = labels[user_span["start"] : user_span["end"]]
                if len(user_tokens) > remaining:
                    user_tokens = [
                        user_tokens[0],
                        *user_tokens[-(remaining - 1) :],
                    ]
                    user_labels = [-100] * len(user_tokens)
                tokens = [
                    tokens[0],
                    *system_tokens,
                    *user_tokens,
                    *assistant_tokens,
                ]
                labels = [
                    -100,
                    *system_labels,
                    *user_labels,
                    *assistant_labels,
                ]
                original_ids = list(
                    record["provenance"].get("message_ids") or []
                )
                retained_ids = [
                    turn_spans[index].get("message_id")
                    for index in (system_index, user_index, assistant_index)
                    if index is not None
                    if turn_spans[index].get("message_id")
                ]
                updated_provenance = dict(record["provenance"])
                updated_provenance["source_message_ids"] = original_ids
                updated_provenance["message_ids"] = retained_ids
                updated_provenance["dropped_message_ids"] = [
                    identifier
                    for identifier in original_ids
                    if identifier not in retained_ids
                ]
                updated_provenance["retained_turn_token_spans"] = [
                    *(
                        [
                            {
                                "role": "system",
                                "start": 1,
                                "end": 1 + len(system_tokens),
                                "message_id": turn_spans[system_index].get(
                                    "message_id"
                                ),
                            }
                        ]
                        if system_index is not None
                        else []
                    ),
                    {
                        "role": "user",
                        "start": 1 + len(system_tokens),
                        "end": 1 + len(system_tokens) + len(user_tokens),
                        "message_id": turn_spans[user_index].get("message_id"),
                    },
                    {
                        "role": "assistant",
                        "start": 1 + len(system_tokens) + len(user_tokens),
                        "end": len(tokens),
                        "message_id": turn_spans[assistant_index].get(
                            "message_id"
                        ),
                    },
                ]
                updated_provenance["turn_token_spans"] = list(
                    updated_provenance["retained_turn_token_spans"]
                )
                record = {**record, "provenance": updated_provenance}
            elif settings["truncation"] == "right":
                tokens = tokens[:length]
                labels = labels[:length]
            else:
                tokens = tokens[-length:]
                labels = labels[-length:]
        if not any(label != -100 for label in labels[1:]):
            raise DatasetPreparationError(
                "NO_VALID_ASSISTANT_TARGETS: truncation removed every target token"
            )
        normalised.append(
            {
                **record,
                "tokens": tokens,
                "labels": labels,
                "truncated": truncated,
            }
        )
    if not settings["packing"]:
        return [
            {
                "tokens": record["tokens"],
                "labels": record["labels"],
                "provenance": {
                    "segments": [
                        {
                            **record["provenance"],
                            "sequence_start": 0,
                            "sequence_end": len(record["tokens"]),
                            "truncated": record["truncated"],
                        }
                    ]
                },
            }
            for record in normalised
        ]

    sequences: list[dict[str, Any]] = []
    current_tokens: list[int] = []
    current_labels: list[int] = []
    current_segments: list[dict[str, Any]] = []

    def flush() -> None:
        if current_tokens:
            sequences.append(
                {
                    "tokens": list(current_tokens),
                    "labels": list(current_labels),
                    "provenance": {"segments": list(current_segments)},
                }
            )
            current_tokens.clear()
            current_labels.clear()
            current_segments.clear()

    for record in normalised:
        tokens = record["tokens"]
        labels = list(record["labels"])
        if current_tokens and len(current_tokens) + len(tokens) > length:
            flush()
        start = len(current_tokens)
        if start:
            labels[0] = -100
        current_tokens.extend(tokens)
        current_labels.extend(labels)
        segment_provenance = dict(record["provenance"])
        if segment_provenance.get("turn_token_spans"):
            segment_provenance["turn_token_spans"] = [
                {
                    **span,
                    "start": int(span["start"]) + start,
                    "end": int(span["end"]) + start,
                }
                for span in segment_provenance["turn_token_spans"]
            ]
        current_segments.append(
            {
                **segment_provenance,
                "sequence_start": start,
                "sequence_end": start + len(tokens),
                "truncated": record["truncated"],
            }
        )
    flush()
    return sequences
