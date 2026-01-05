"""Public facade for dataset inspection, validation, and preparation.

The implementation is organized by concern in sibling modules.  This facade
keeps the original ``DatasetLibrary`` API stable for the application layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from app.backend.database.control import Database, json_text
from app.backend.operations.manager import (
    Notification,
    OperationContext,
    OperationManager,
)
from app.backend.system.config import AppConfig
from app.backend.system.files import sha256_bytes, sha256_file

from . import catalog, preparation, prepared_cache, validation
from .errors import (
    DatasetError,
    DatasetFormatError,
    DatasetPreparationError,
    DatasetValidationError,
    _RecordProblem,
)
from .mapping import (
    MAPPING_FIELDS,
    MAPPING_TYPES,
    _conversation_parts,
    _extract,
    _text_value,
    format_training_example,
    mapped_training_record,
    normalize_mapping,
    preview_mapping as _preview_mapping,
)
from .prepared_cache import PREPARED_FORMAT_VERSION
from .source_inspection import (
    SUPPORTED_FORMATS,
    _detect_encoding,
    _json_records,
    _json_safe,
    _RecordEvent,
    _record_events,
    _source_format,
    source_descriptor,
    inspect_source as _inspect_source,
)
from .validation import (
    ISSUE_EXAMPLE_LIMIT,
    MINIMUM_EXAMPLE_TOKENS,
    Encoder,
    _ISSUE_DEFINITIONS,
    _IssueCollection,
)


class ConversationTokenizer(Protocol):
    fingerprint: str
    bos_id: int
    eos_id: int
    pad_id: int

    def encode(
        self, text: str, *, add_bos: bool = False, add_eos: bool = False
    ) -> list[int]: ...

    def decode(self, token_ids: list[int]) -> str: ...

    def conversation_example(
        self,
        messages: list[dict[str, str]],
        sequence_length: int,
        *,
        assistant_only_labels: bool = False,
    ) -> tuple[list[int], list[int]]: ...


class DatasetLibrary:
    """Coordinate dataset workflows backed by files and the control database."""

    def __init__(
        self,
        database: Database,
        prepared_root: str | Path,
        *,
        encoder: Encoder | None = None,
        operations: OperationManager | None = None,
        tokenizer_reference: str | None = None,
        tokenizer_fingerprint: str | None = None,
        bos_token_id: int | None = None,
        eos_token_id: int | None = None,
        pad_token_id: int | None = None,
        default_sequence_length: int = 512,
        architectural_context_tokens: int = 8192,
        default_settings: Mapping[str, Any] | None = None,
        tokenizer: ConversationTokenizer | None = None,
    ) -> None:
        self.database = database
        self.prepared_root = Path(prepared_root).resolve()
        self.prepared_root.mkdir(parents=True, exist_ok=True)
        self.encoder = encoder
        self.operations = operations
        self.tokenizer_reference = tokenizer_reference
        self.tokenizer_fingerprint = tokenizer_fingerprint
        self.bos_token_id = bos_token_id
        self.eos_token_id = eos_token_id
        self.pad_token_id = pad_token_id
        self.default_sequence_length = int(default_sequence_length)
        self.architectural_context_tokens = int(architectural_context_tokens)
        self.default_settings = dict(default_settings or {})
        self.tokenizer = tokenizer

    @classmethod
    def from_config(
        cls,
        database: Database,
        config: AppConfig,
        *,
        encoder: Encoder | None = None,
        operations: OperationManager | None = None,
        tokenizer_reference: str | None = None,
        tokenizer_fingerprint: str | None = None,
        bos_token_id: int | None = None,
        eos_token_id: int | None = None,
        pad_token_id: int | None = None,
        tokenizer: ConversationTokenizer | None = None,
    ) -> "DatasetLibrary":
        dataset = config.section("datasets")
        training = config.section("training")
        defaults = {
            "sequence_length": int(training["sequence_length"]),
            "training_split": float(dataset["training_split"]),
            "validation_split": float(dataset["validation_split"]),
            "packing": bool(dataset["packing"]),
            "truncation": str(dataset["truncation"]),
            "random_seed": int(dataset["random_seed"]),
            "bos": bool(dataset["bos"]),
            "eos": bool(dataset["eos"]),
            "padding": bool(config.section("tokenizer")["pad_to_sequence"]),
            "cache_location": str(config.paths.prepared),
        }
        return cls(
            database,
            config.paths.prepared,
            encoder=encoder,
            operations=operations,
            tokenizer_reference=tokenizer_reference
            or str(config.resolve(str(config.section("tokenizer")["model"]))),
            tokenizer_fingerprint=tokenizer_fingerprint,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            pad_token_id=pad_token_id,
            default_sequence_length=int(training["sequence_length"]),
            architectural_context_tokens=int(
                config.section("model")["architectural_context_tokens"]
            ),
            default_settings=defaults,
            tokenizer=tokenizer,
        )

    def inspect_source(
        self,
        source_path: str | Path,
        *,
        sample_limit: int = 5,
    ) -> dict[str, Any]:
        return _inspect_source(source_path, sample_limit=sample_limit)

    inspect = inspect_source

    def preview_mapping(
        self,
        source_path: str | Path,
        mapping: Mapping[str, Any],
        *,
        sample_limit: int = 3,
    ) -> dict[str, Any]:
        result = _preview_mapping(
            source_path,
            mapping,
            sample_limit=sample_limit,
        )
        settings = self._preparation_settings(
            {
                "sequence_length": self.default_sequence_length,
                "packing": False,
            }
        )
        for sample in result.get("formatted_samples", []):
            messages = sample.get("semantic_messages")
            if sample.get("error") or messages is None or self.tokenizer is None:
                continue
            canonical = preparation.canonical_chat_messages(messages)
            rendered = "\n\n".join(
                f"{str(message['role']).capitalize()}:\n{message['content']}"
                for message in canonical
            )
            record = {
                "formatted_text": rendered,
                "messages": messages,
                "assistant_only_labels": sample.get("label_mask")
                in {"assistant_only", "terminal_assistant_only"},
                "terminal_assistant_only_labels": bool(
                    sample.get("terminal_assistant_only_labels")
                ),
                "group_id": sample.get("group_id") or "preview",
                "character_count": len(rendered),
                "provenance": {
                    "source_record_index": sample.get("record"),
                    "message_tree_id": sample.get("message_tree_id"),
                    "branch_id": sample.get("branch_id"),
                    "target_ordinal": sample.get("target_ordinal"),
                },
            }
            tokenised = preparation.tokenize_records(
                self,
                [record],
                settings,
                context=None,
                progress_offset=0,
                progress_total=1,
            )[0]
            fit = preparation.assistant_target_fit_diagnostic(
                tokenised, self.default_sequence_length
            )
            sample["rendered_training_text"] = rendered
            sample["full_token_count"] = len(tokenised["tokens"])
            sample["sequence_length"] = self.default_sequence_length
            sample["fit_diagnostic"] = fit
            if fit is None:
                sequence = preparation.make_sequences(
                    [tokenised], settings
                )[0]
                tokens = sequence["tokens"]
                labels = sequence["labels"]
                spans = sequence["provenance"]["segments"][0].get(
                    "turn_token_spans", []
                )
            else:
                tokens = tokenised["tokens"][: self.default_sequence_length]
                labels = tokenised["labels"][: self.default_sequence_length]
                spans = tokenised["provenance"].get("turn_token_spans", [])
            sample["token_ids"] = tokens
            sample["decoded_token_round_trip"] = self.tokenizer.decode(tokens)
            sample["target_mask"] = [
                "target" if label != -100 else "masked" for label in labels
            ]
            sample["sequence_boundaries"] = spans
            sample["content_round_trip"] = [
                {
                    "role": message["role"],
                    "source": str(message["content"]),
                    "decoded": self.tokenizer.decode(
                        self.tokenizer.encode(str(message["content"]))
                    ),
                }
                for message in canonical
            ]
        result["preview_contract"] = {
            "template": preparation.CHAT_TEMPLATE_VERSION,
            "sequence_length": self.default_sequence_length,
            "packing": False,
            "truncation": "target_preserving",
        }
        return result

    def add_dataset(
        self,
        source_path: str | Path,
        *,
        name: str,
        language: str,
        purpose: str,
        mapping: Mapping[str, Any],
        description: str | None = None,
    ) -> dict[str, Any]:
        return catalog.add_dataset(
            self.database,
            source_path,
            name=name,
            language=language,
            purpose=purpose,
            mapping=mapping,
            description=description,
        )

    def preparation_preflight(
        self,
        dataset_id: str,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Analyze the exact materialized curriculum without writing artifacts."""

        dataset = self.get_dataset(dataset_id)
        if dataset is None:
            raise KeyError(f"Dataset does not exist: {dataset_id}")
        source_path = Path(dataset["source_path"])
        if (
            not source_path.is_file()
            or sha256_file(source_path) != dataset["source_checksum"]
        ):
            raise DatasetPreparationError(
                "The source identity changed; validate it again before preflight"
            )
        normalized = self._preparation_settings(settings)
        if dataset["mapping"].get("type") in {
            "instruction",
            "conversation",
            "oasst2",
        }:
            normalized = {**normalized, "packing": False}
        read_result = self._read_formatted_records(dataset)
        tokenised = self._tokenize_records(
            read_result.records,
            normalized,
            context=None,
            progress_offset=0,
            progress_total=len(read_result.records),
        )
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for record in tokenised:
            diagnostic = preparation.assistant_target_fit_diagnostic(
                record, int(normalized["sequence_length"])
            )
            if diagnostic is None:
                accepted.append(record)
            else:
                rejected.append(diagnostic)
        sequences = self._make_sequences(accepted, normalized)
        accepted_target_tokens = sum(
            sum(1 for label in sequence["labels"][1:] if label != -100)
            for sequence in sequences
        )
        rejected_target_tokens = sum(
            int(item.get("assistant_target_tokens", 0))
            for item in rejected
        )
        generated_target_tokens = (
            accepted_target_tokens + rejected_target_tokens
        )
        return {
            "dataset_id": dataset_id,
            "source_checksum": dataset["source_checksum"],
            "settings": normalized,
            "raw_source_record_count": read_result.source_record_count,
            "generated_record_count": len(tokenised),
            "accepted_record_count": len(accepted),
            "rejected_record_count": len(rejected),
            "sequence_count": len(sequences),
            "truncated_record_count": sum(
                1
                for sequence in sequences
                for segment in sequence.get("provenance", {}).get(
                    "segments", []
                )
                if segment.get("truncated")
            ),
            "accepted_target_tokens": accepted_target_tokens,
            "rejected_target_tokens": rejected_target_tokens,
            "generated_target_tokens": generated_target_tokens,
            "target_token_coverage": (
                accepted_target_tokens / generated_target_tokens
                if generated_target_tokens
                else 1.0
            ),
            "rejection_diagnostics": rejected[:100],
            "source_rejection_diagnostics": read_result.diagnostics[:100],
            "read_only": True,
        }

    add = add_dataset

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        return catalog.get_dataset(self.database, dataset_id)

    def list_datasets(self) -> list[dict[str, Any]]:
        return catalog.list_datasets(self.database)

    @staticmethod
    def _dataset_record(row: Mapping[str, Any]) -> dict[str, Any]:
        return catalog.dataset_record(row)

    def check_source_changed(
        self,
        dataset_id: str,
        *,
        force_checksum: bool = True,
    ) -> dict[str, Any]:
        return catalog.check_source_changed(
            self.database,
            dataset_id,
            force_checksum=force_checksum,
        )

    def _mark_source_stale(self, dataset_id: str) -> None:
        catalog.mark_source_stale(self.database, dataset_id)

    def _encode(self, text: str) -> list[int]:
        return validation.encode_text(self.encoder, text)

    def validate_dataset(
        self,
        dataset_id: str,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        return validation.validate_dataset(self, dataset_id, context=context)

    def _commit_validation(
        self,
        dataset: Mapping[str, Any],
        issues: _IssueCollection,
        *,
        record_count: int,
        usable_records: int,
        checksum: str | None,
    ) -> dict[str, Any]:
        return validation.commit_validation(
            self.database,
            dataset,
            issues,
            record_count=record_count,
            usable_records=usable_records,
            checksum=checksum,
        )

    def start_validation(self, dataset_id: str) -> dict[str, Any]:
        if self.operations is None:
            raise RuntimeError("No operation manager is configured")
        if self.get_dataset(dataset_id) is None:
            raise KeyError(f"Dataset does not exist: {dataset_id}")
        return self.operations.submit(
            "dataset_validation",
            lambda context: self.validate_dataset(dataset_id, context=context),
            target_id=dataset_id,
            dedupe_key=f"dataset-validation:{dataset_id}",
            initial_phase="Waiting",
            success_notification=Notification(
                "information",
                "Dataset validation finished",
                "Review the validation results before preparation.",
            ),
            failure_notification=Notification(
                "error",
                "Dataset validation failed",
                "Review the operation error and source file.",
                None,
            ),
        )

    validate = start_validation

    def _preparation_settings(
        self,
        settings: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return preparation.preparation_settings(self, settings)

    def start_preparation(
        self,
        dataset_id: str,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.operations is None:
            raise RuntimeError("No operation manager is configured")
        if self.get_dataset(dataset_id) is None:
            raise KeyError(f"Dataset does not exist: {dataset_id}")
        normalized = self._preparation_settings(settings)
        settings_digest = sha256_bytes(json_text(normalized).encode("utf-8"))
        return self.operations.submit(
            "dataset_preparation",
            lambda context: self.prepare_dataset(
                dataset_id,
                normalized,
                context=context,
            ),
            target_id=dataset_id,
            dedupe_key=f"dataset-preparation:{dataset_id}:{settings_digest}",
            initial_phase="Waiting",
            success_notification=Notification(
                "success",
                "Dataset prepared",
                "The verified prepared dataset is ready for training.",
            ),
            failure_notification=Notification(
                "error",
                "Dataset preparation failed",
                "Review the operation error and validation results.",
                None,
            ),
        )

    prepare = start_preparation

    def prepare_dataset(
        self,
        dataset_id: str,
        settings: Mapping[str, Any] | None = None,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        return preparation.prepare_dataset(
            self,
            dataset_id,
            settings,
            context=context,
        )

    def _read_formatted_records(
        self, dataset: Mapping[str, Any]
    ) -> preparation.ReadRecordsResult:
        return preparation.read_formatted_records(dataset)

    @staticmethod
    def _split_records(
        records: list[dict[str, Any]],
        settings: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return preparation.split_records(records, settings)

    def _tokenize_records(
        self,
        records: list[dict[str, Any]],
        settings: Mapping[str, Any],
        *,
        context: OperationContext | None,
        progress_offset: int,
        progress_total: int,
    ) -> list[dict[str, Any]]:
        return preparation.tokenize_records(
            self,
            records,
            settings,
            context=context,
            progress_offset=progress_offset,
            progress_total=progress_total,
        )

    @staticmethod
    def _make_sequences(
        records: list[dict[str, Any]],
        settings: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        return preparation.make_sequences(records, settings)

    def _write_split(
        self,
        directory: Path,
        name: str,
        sequences: list[dict[str, Any]],
        source_record_count: int,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        return prepared_cache.write_split(
            directory,
            name,
            sequences,
            source_record_count,
            settings,
            pad_token_id=self.pad_token_id,
        )

    def _verify_directory(self, directory: Path) -> dict[str, Any]:
        return prepared_cache.verify_directory(
            directory,
            tokenizer_fingerprint=self.tokenizer_fingerprint,
        )

    def verify_prepared(self, prepared_dataset_id: str) -> dict[str, Any]:
        return prepared_cache.verify_prepared(
            self.database,
            prepared_dataset_id,
            tokenizer_fingerprint=self.tokenizer_fingerprint,
        )

    def list_prepared(
        self,
        dataset_id: str | None = None,
        *,
        ready_only: bool = False,
    ) -> list[dict[str, Any]]:
        return prepared_cache.list_prepared(
            self.database,
            dataset_id,
            ready_only=ready_only,
        )

    def latest_ready_prepared(self, dataset_id: str) -> dict[str, Any] | None:
        return prepared_cache.latest_ready_prepared(self.database, dataset_id)

    def _remove_prepared_directory(self, path: Path) -> None:
        prepared_cache.remove_prepared_directory(path, self.prepared_root)
