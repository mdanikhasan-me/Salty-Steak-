"""Validate mapped records and persist actionable validation summaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.backend.database.control import Database, json_text, utc_now
from app.backend.operations.manager import OperationContext
from app.backend.system.files import sha256_bytes, sha256_file

from .errors import DatasetValidationError, _RecordProblem
from .mapping import format_training_example
from .oasst2 import reconstruct_oasst2
from .source_inspection import (
    _detect_encoding,
    _json_safe,
    _record_events,
    _source_format,
)

if TYPE_CHECKING:
    from .library import DatasetLibrary


MINIMUM_EXAMPLE_TOKENS = 3
ISSUE_EXAMPLE_LIMIT = 3

Encoder = Callable[[str], Sequence[int]]

_ISSUE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "unreadable_source": (
        "The source file could not be read.",
        "Confirm that the file still exists and that this application can read it.",
    ),
    "malformed_record": (
        "Some records do not match the detected file format.",
        "Correct the listed source records, then validate again.",
    ),
    "required_field": (
        "A mapped required field is missing.",
        "Correct the field mapping or add the missing source values.",
    ),
    "empty_record": (
        "Some records contain no usable training text.",
        "Remove or fill the empty records in the original source.",
    ),
    "missing_assistant_response": (
        "Some examples have no assistant response.",
        "Map a populated assistant response field or correct the source.",
    ),
    "invalid_conversation_roles": (
        "Some conversations have invalid role ordering.",
        "Use an optional first system message followed by user and assistant turns.",
    ),
    "duplicate_record": (
        "Duplicate formatted examples were found.",
        "Review duplicates in the source; repeated examples may bias training.",
    ),
    "extremely_short": (
        "Some examples are extremely short.",
        "Review whether these examples contain enough learning material.",
    ),
    "extremely_long": (
        "Some examples exceed the saved architecture context.",
        "Split these examples without changing their meaning.",
    ),
    "tokenizer_unavailable": (
        "The Salty Steak tokenizer is not available.",
        "Restore and verify the SentencePiece tokenizer, then validate again.",
    ),
    "tokenizer_failure": (
        "The tokenizer could not encode some examples.",
        "Correct the listed source text or verify the tokenizer files.",
    ),
    "likely_truncation": (
        "Some examples exceed the selected training sequence length.",
        "Review truncation or packing settings before preparation.",
    ),
    "assistant_target_exceeds_sequence": (
        "Some assistant targets cannot fit the selected sequence length intact.",
        "Increase sequence length or accept the displayed target-window rejections; "
        "assistant target text will never be partially truncated.",
    ),
    "no_usable_records": (
        "No usable training records were found.",
        "Choose a populated source file and correct its mapping.",
    ),
    "source_changed_during_validation": (
        "The source changed while validation was running.",
        "Wait for source edits to finish, then validate again.",
    ),
    "oasst2_rejected_tree": (
        "Some OASST2 trees were intentionally rejected by language, review, "
        "deleted-message, or assistant-target filters.",
        "Inspect the rejection examples and selected curriculum before preparation.",
    ),
    "oasst2_invalid_tree": (
        "Some OASST2 trees could not be reconstructed safely.",
        "Correct the listed tree structure or exclude those source records.",
    ),
}


class _IssueCollection:
    def __init__(self) -> None:
        self._blocking: dict[str, dict[str, Any]] = {}
        self._warnings: dict[str, dict[str, Any]] = {}

    def add(
        self,
        code: str,
        *,
        blocking: bool,
        record_index: int | None = None,
        example: Any = None,
    ) -> None:
        collection = self._blocking if blocking else self._warnings
        message, action = _ISSUE_DEFINITIONS[code]
        issue = collection.setdefault(
            code,
            {
                "code": code,
                "message": message,
                "affected_count": 0,
                "examples": [],
                "recommended_action": action,
            },
        )
        issue["affected_count"] += 1
        if example is not None and len(issue["examples"]) < ISSUE_EXAMPLE_LIMIT:
            item = {"record": record_index, "value": _json_safe(example)}
            issue["examples"].append(item)

    @property
    def blocking(self) -> list[dict[str, Any]]:
        return list(self._blocking.values())

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return list(self._warnings.values())

    def counts(self) -> dict[str, int]:
        return {
            issue["code"]: issue["affected_count"]
            for issue in [*self._blocking.values(), *self._warnings.values()]
        }


def encode_text(encoder: Encoder | None, text: str) -> list[int]:
    if encoder is None:
        raise DatasetValidationError("Tokenizer is unavailable")
    encoded = encoder(text)
    if isinstance(encoded, (str, bytes, bytearray)) or not isinstance(
        encoded, Sequence
    ):
        raise TypeError("Tokenizer encode callable must return a sequence of IDs")
    result = [int(token) for token in encoded]
    if any(token < 0 for token in result):
        raise ValueError("Tokenizer returned a negative token ID")
    return result


def validate_dataset(
    library: DatasetLibrary,
    dataset_id: str,
    *,
    context: OperationContext | None = None,
) -> dict[str, Any]:
    dataset = library.get_dataset(dataset_id)
    if dataset is None:
        raise KeyError(f"Dataset does not exist: {dataset_id}")
    now = utc_now()
    library.database.execute(
        "UPDATE datasets SET validation_status = 'validating', updated_at = ? WHERE id = ?",
        (now, dataset_id),
    )
    issues = _IssueCollection()
    path = Path(dataset["source_path"])
    try:
        if context:
            context.checkpoint(phase="Reading data")
        if not path.is_file():
            issues.add("unreadable_source", blocking=True, example=str(path))
            return library._commit_validation(
                dataset,
                issues,
                record_count=0,
                usable_records=0,
                checksum=None,
            )
        format_name = _source_format(path)
        encoding = None if format_name == "parquet" else _detect_encoding(path)
        start_checksum = sha256_file(path)
        events = list(_record_events(path, format_name, encoding))
        total = len(events)
        if context:
            context.checkpoint(
                phase="Validating records",
                current_progress=0,
                total_progress=total,
            )
        if library.encoder is None:
            issues.add("tokenizer_unavailable", blocking=True)
        mapping = dataset["mapping"]
        seen: set[str] = set()
        usable = 0
        if mapping.get("type") == "oasst2":
            for event in events:
                if event.error is not None:
                    issues.add(
                        "malformed_record",
                        blocking=True,
                        record_index=event.index,
                        example=event.error,
                    )
            diagnostics: list[dict[str, Any]] = []
            branches = reconstruct_oasst2(
                [event.record for event in events],
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
                branch_policy=str(mapping.get("branch_policy", "best_ranked_leaf")),
                skip_rejected=True,
                diagnostics=diagnostics,
            )
            blocking_codes = {
                "DATASET_SCHEMA_INVALID",
                "DATASET_TREE_RECONSTRUCTION_FAILED",
                "DATASET_DUPLICATE_MESSAGE_ID",
                "DATASET_MISSING_PARENT",
                "DATASET_TREE_CYCLE",
                "DATASET_ROLE_INVALID",
                "DATASET_DUPLICATE_TREE",
                "DATASET_TREE_STATE_INVALID",
            }
            for diagnostic in diagnostics:
                is_blocking = diagnostic.get("code") in blocking_codes
                issues.add(
                    "oasst2_invalid_tree"
                    if is_blocking
                    else "oasst2_rejected_tree",
                    blocking=is_blocking,
                    record_index=diagnostic.get("source_record_index"),
                    example=diagnostic,
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
                    formatted = "\n\n".join(
                        f"{message['role'].capitalize()}:\n"
                        f"{message['content']}"
                        for message in target_messages
                    )
                    digest = sha256_bytes(formatted.encode("utf-8"))
                    if digest in seen:
                        issues.add(
                            "duplicate_record",
                            blocking=False,
                            example={
                                "message_tree_id": branch.tree_id,
                                "branch_id": branch.branch_id,
                                "target_ordinal": target_ordinal,
                            },
                        )
                    else:
                        seen.add(digest)
                    try:
                        if library.tokenizer is not None:
                            token_ids, labels = (
                                library.tokenizer.conversation_example(
                                    target_messages,
                                    2**31 - 1,
                                    assistant_only_labels=True,
                                )
                            )
                            target_end = max(
                                (
                                    index
                                    for index, label in enumerate(labels)
                                    if label != -100
                                ),
                                default=-1,
                            )
                            if target_end < 1:
                                issues.add(
                                    "oasst2_rejected_tree",
                                    blocking=False,
                                    example={
                                        "code": "NO_VALID_ASSISTANT_TARGETS",
                                        "message_tree_id": branch.tree_id,
                                        "target_ordinal": target_ordinal,
                                    },
                                )
                                continue
                            target_start = target_end
                            while (
                                target_start > 0
                                and labels[target_start - 1] != -100
                            ):
                                target_start -= 1
                            prefix_tokens, _prefix_labels = (
                                library.tokenizer.conversation_example(
                                    [],
                                    2**31 - 1,
                                    assistant_only_labels=True,
                                )
                            )
                            nearest_user = next(
                                message
                                for message in reversed(
                                    target_messages[:target_position]
                                )
                                if message["role"] == "user"
                            )
                            user_content_tokens = len(
                                library.tokenizer.encode(
                                    str(nearest_user["content"]),
                                    add_eos=False,
                                )
                            )
                            minimum_user_turn = (
                                2 + min(8, user_content_tokens)
                            )
                            required_minimum = (
                                len(prefix_tokens)
                                + minimum_user_turn
                                + 1
                                + (target_end - target_start + 1)
                            )
                        else:
                            token_ids = library._encode(formatted)
                            required_minimum = len(token_ids)
                    except Exception as exc:
                        issues.add(
                            "tokenizer_failure",
                            blocking=True,
                            example={
                                "message_tree_id": branch.tree_id,
                                "target_ordinal": target_ordinal,
                                "error": str(exc),
                            },
                        )
                        continue
                    if required_minimum > library.default_sequence_length:
                        issues.add(
                            "assistant_target_exceeds_sequence",
                            blocking=False,
                            example={
                                "message_tree_id": branch.tree_id,
                                "target_ordinal": target_ordinal,
                                "required_minimum_tokens": required_minimum,
                                "sequence_length": (
                                    library.default_sequence_length
                                ),
                            },
                        )
                        continue
                    if len(token_ids) > library.default_sequence_length:
                        issues.add(
                            "likely_truncation",
                            blocking=False,
                            example={
                                "message_tree_id": branch.tree_id,
                                "target_ordinal": target_ordinal,
                                "tokens": len(token_ids),
                            },
                        )
                    if len(token_ids) > library.architectural_context_tokens:
                        issues.add(
                            "extremely_long",
                            blocking=False,
                            example={
                                "message_tree_id": branch.tree_id,
                                "target_ordinal": target_ordinal,
                                "tokens": len(token_ids),
                            },
                        )
                    usable += 1
            if usable == 0:
                issues.add("no_usable_records", blocking=True)
            if context:
                context.checkpoint(
                    phase="Finalising validation",
                    current_progress=total,
                    total_progress=total,
                )
            end_checksum = sha256_file(path)
            if end_checksum != start_checksum:
                issues.add("source_changed_during_validation", blocking=True)
                library._mark_source_stale(dataset_id)
                checksum = None
            else:
                checksum = end_checksum
            return library._commit_validation(
                dataset,
                issues,
                record_count=total,
                usable_records=usable,
                checksum=checksum,
            )
        for position, event in enumerate(events, start=1):
            if context and (position == 1 or position % 100 == 0):
                context.checkpoint(
                    phase="Validating records",
                    current_progress=position - 1,
                    total_progress=total,
                )
            if event.error is not None:
                code = (
                    "empty_record"
                    if event.error == "empty line"
                    else "malformed_record"
                )
                issues.add(
                    code,
                    blocking=code == "malformed_record",
                    record_index=event.index,
                    example=event.error,
                )
                continue
            try:
                formatted = format_training_example(event.record, mapping)
            except _RecordProblem as exc:
                issues.add(
                    exc.code,
                    blocking=exc.code
                    in {
                        "required_field",
                        "missing_assistant_response",
                        "invalid_conversation_roles",
                    },
                    record_index=event.index,
                    example=event.record,
                )
                continue
            digest = sha256_bytes(formatted.encode("utf-8"))
            if digest in seen:
                issues.add(
                    "duplicate_record",
                    blocking=False,
                    record_index=event.index,
                    example=formatted,
                )
            else:
                seen.add(digest)
            if library.encoder is not None:
                try:
                    token_ids = library._encode(formatted)
                except Exception as exc:
                    issues.add(
                        "tokenizer_failure",
                        blocking=True,
                        record_index=event.index,
                        example=str(exc),
                    )
                    continue
                if len(token_ids) < MINIMUM_EXAMPLE_TOKENS:
                    issues.add(
                        "extremely_short",
                        blocking=False,
                        record_index=event.index,
                        example=formatted,
                    )
                if len(token_ids) > library.architectural_context_tokens:
                    issues.add(
                        "extremely_long",
                        blocking=False,
                        record_index=event.index,
                        example={"tokens": len(token_ids)},
                    )
                if len(token_ids) > library.default_sequence_length:
                    issues.add(
                        "likely_truncation",
                        blocking=False,
                        record_index=event.index,
                        example={"tokens": len(token_ids)},
                    )
            usable += 1
        if usable == 0:
            issues.add("no_usable_records", blocking=True)
        if context:
            context.checkpoint(
                phase="Finalising validation",
                current_progress=total,
                total_progress=total,
            )
        end_checksum = sha256_file(path)
        if end_checksum != start_checksum:
            issues.add("source_changed_during_validation", blocking=True)
            library._mark_source_stale(dataset_id)
            checksum = None
        else:
            checksum = end_checksum
        return library._commit_validation(
            dataset,
            issues,
            record_count=total,
            usable_records=usable,
            checksum=checksum,
        )
    except Exception:
        library.database.execute(
            """
            UPDATE datasets
            SET validation_status = 'failed', updated_at = ?
            WHERE id = ? AND validation_status = 'validating'
            """,
            (utc_now(), dataset_id),
        )
        raise


def commit_validation(
    database: Database,
    dataset: Mapping[str, Any],
    issues: _IssueCollection,
    *,
    record_count: int,
    usable_records: int,
    checksum: str | None,
) -> dict[str, Any]:
    status = "invalid" if issues.blocking else "valid"
    summary = {
        "dataset_id": dataset["id"],
        "status": status,
        "record_count": record_count,
        "usable_record_count": usable_records,
        "blocking_error_count": sum(
            issue["affected_count"] for issue in issues.blocking
        ),
        "warning_count": sum(
            issue["affected_count"] for issue in issues.warnings
        ),
        "blocking_errors": issues.blocking,
        "warnings": issues.warnings,
        "affected_counts": issues.counts(),
        "source_checksum": checksum,
        "validated_at": utc_now(),
    }
    path = Path(str(dataset["source_path"]))
    stat = path.stat() if path.is_file() else None
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE datasets
            SET validation_status = ?,
                validation_summary_json = ?,
                record_count = ?,
                source_checksum = COALESCE(?, source_checksum),
                size_bytes = COALESCE(?, size_bytes),
                source_mtime_ns = COALESCE(?, source_mtime_ns),
                source_changed = CASE WHEN ? IS NULL THEN 1 ELSE 0 END,
                prepared_status = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM prepared_datasets p
                        WHERE p.dataset_id = datasets.id
                          AND p.source_checksum = ?
                          AND p.verified = 1
                          AND p.status = 'ready'
                    ) THEN 'ready'
                    WHEN prepared_status IN ('ready','stale') THEN 'stale'
                    ELSE prepared_status
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json_text(summary),
                record_count,
                checksum,
                stat.st_size if stat else None,
                stat.st_mtime_ns if stat else None,
                checksum,
                checksum,
                utc_now(),
                dataset["id"],
            ),
        )
    return summary
