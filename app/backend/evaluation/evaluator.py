"""Evaluation of one exact saved version."""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import torch
import numpy as np
from torch import Tensor

from ..system.device import normalise_device, release_device
from ..system.files import sha256_file
from ..training.engine import PreparedExamples
from ..versions.checkpoint import inspect_checkpoint, load_checkpoint
from .contract import build_evaluation_contract


class EvaluationProgress(Protocol):
    def update(
        self,
        phase: str,
        *,
        current: int | float | None = None,
        total: int | float | None = None,
        result: dict[str, Any] | None = None,
    ) -> None: ...

    def stop_requested(self) -> bool: ...


@dataclass(frozen=True)
class EvaluationSettings:
    operation_id: str
    version_id: str
    checkpoint_path: str
    expected_weight_sha256: str
    expected_tokenizer_fingerprint: str
    prepared_path: str
    report_path: str
    context_limit: int
    sequence_length: int = 512
    batch_size: int = 8
    max_records: int = 0
    device: str = "cuda"
    precision: str = "bf16"
    family: str = "stage_specific"


@dataclass
class EvaluationResult:
    operation_id: str
    version_id: str
    state: str
    records_completed: int
    total_records: int
    mean_loss: float
    perplexity: float
    duration_seconds: float
    records_per_second: float
    tokens_evaluated: int
    tokens_per_second: float
    report_path: str
    report_sha256: str
    batch_size: int = 0
    timings_seconds: dict[str, Any] | None = None
    end_to_end_duration_seconds: float = 0.0
    evaluation_contract: dict[str, Any] | None = None
    evaluation_contract_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_evaluation_tokenizer_identity(
    prepared_manifest: dict[str, Any],
    tokenizer: Any,
    expected_tokenizer_fingerprint: str,
) -> None:
    """Bind prepared token IDs, the saved version, and the loaded tokenizer."""

    prepared_fingerprint = prepared_manifest.get("tokenizer_fingerprint")
    prepared_digest = prepared_manifest.get("tokenizer_digest")
    loaded_fingerprint = getattr(tokenizer, "fingerprint", None)
    loaded_digest = getattr(tokenizer, "metadata", {}).get("model_sha256")
    if (
        not prepared_fingerprint
        or not prepared_digest
        or loaded_fingerprint != prepared_fingerprint
        or loaded_fingerprint != expected_tokenizer_fingerprint
        or loaded_digest != prepared_digest
    ):
        raise ValueError(
            "prepared data tokenizer does not match the selected saved version"
        )


def perplexity_from_mean_loss(mean_loss: float) -> float:
    """Return true perplexity or fail instead of emitting a capped value."""

    if not math.isfinite(mean_loss):
        raise ValueError("evaluation mean loss is non-finite")
    try:
        return math.exp(mean_loss)
    except OverflowError as exc:
        raise ValueError(
            "evaluation perplexity exceeds the numeric reporting range"
        ) from exc


def run_evaluation(
    settings: EvaluationSettings, progress: EvaluationProgress
) -> EvaluationResult | None:
    resolved = normalise_device(
        settings.device,
        precision=settings.precision,
        make_current=True,
        reset_peak_memory=True,
    )
    try:
        return _run_evaluation(settings, progress, resolved.device)
    finally:
        release_device(resolved)


def _run_evaluation(
    settings: EvaluationSettings,
    progress: EvaluationProgress,
    device: torch.device,
) -> EvaluationResult | None:
    started = time.perf_counter()
    timings = {
        "dataset_open_seconds": 0.0,
        "checkpoint_inspection_seconds": 0.0,
        "checkpoint_load_seconds": 0.0,
        "data_read_seconds": 0.0,
        "collation_seconds": 0.0,
        "padding_seconds": 0.0,
        "pin_memory_seconds": 0.0,
        "host_to_device_enqueue_seconds": 0.0,
        "host_to_device_seconds": 0.0,
        "forward_enqueue_seconds": 0.0,
        "forward_seconds": 0.0,
        "loss_reduction_seconds": 0.0,
        "cuda_synchronisation_seconds": 0.0,
        "progress_event_seconds": 0.0,
        "report_write_seconds": 0.0,
        "batches": 0,
    }
    progress.update("Preparing evaluation")
    dataset_started = time.perf_counter()
    dataset = PreparedExamples(settings.prepared_path, "validation")
    timings["dataset_open_seconds"] = time.perf_counter() - dataset_started
    total = len(dataset)
    if settings.max_records > 0:
        total = min(total, settings.max_records)
    if total < 1:
        raise ValueError("prepared dataset has no validation records")
    prepared_manifest_path = Path(settings.prepared_path) / "manifest.json"
    try:
        prepared_manifest = json.loads(
            prepared_manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("prepared evaluation manifest is unreadable") from exc
    if int(prepared_manifest.get("format_version", 0)) < 2:
        raise ValueError(
            "legacy prepared evaluation data has no durable target contract"
        )
    selected_target_tokens = sum(
        sum(1 for target in dataset.item(index)[1][1:] if target != -100)
        for index in range(total)
    )
    progress.update("Loading saved version for evaluation", current=0, total=total)
    inspection_started = time.perf_counter()
    checkpoint_report = inspect_checkpoint(
        settings.checkpoint_path, calculate_checksum=True
    )
    timings["checkpoint_inspection_seconds"] = (
        time.perf_counter() - inspection_started
    )
    if (
        not checkpoint_report.valid
        or checkpoint_report.weight_sha256 != settings.expected_weight_sha256
    ):
        raise ValueError(
            "evaluation checkpoint differs from the selected saved-version identity"
        )
    dtype = (
        torch.bfloat16
        if settings.precision == "bf16"
        else torch.float16
        if settings.precision == "fp16"
        else torch.float32
    )
    load_started = time.perf_counter()
    model, tokenizer = load_checkpoint(
        settings.checkpoint_path,
        device=device,
        dtype=dtype,
        context_limit=settings.context_limit,
    )
    validate_evaluation_tokenizer_identity(
        prepared_manifest,
        tokenizer,
        settings.expected_tokenizer_fingerprint,
    )
    config_path = Path(settings.checkpoint_path) / "config.json"
    if not config_path.is_file():
        raise ValueError("evaluation checkpoint model configuration is missing")
    evaluation_contract = build_evaluation_contract(
        family=settings.family,
        manifest=prepared_manifest,
        prepared_manifest_digest=sha256_file(prepared_manifest_path),
        tokenizer_digest=str(tokenizer.metadata["model_sha256"]),
        model_config_digest=sha256_file(config_path),
        context_length=settings.context_limit,
        sequence_length=settings.sequence_length,
        precision=settings.precision,
        max_records=settings.max_records,
        selected_records=total,
        selected_target_tokens=selected_target_tokens,
    )
    timings["checkpoint_load_seconds"] = time.perf_counter() - load_started
    model.eval()
    losses: list[tuple[float, int]] = []
    completed = 0
    batch_size = max(1, int(settings.batch_size))
    last_progress_at = time.perf_counter()
    progress_interval_seconds = 1.0
    timings["progress_interval_seconds"] = progress_interval_seconds
    progress.update("Evaluating validation records", current=0, total=total)
    with torch.inference_mode():
        for offset in range(0, total, batch_size):
            if progress.stop_requested():
                progress.update(
                    "Finishing evaluation",
                    current=completed,
                    total=total,
                    result={"interrupted": True},
                )
                return None
            indices = list(range(offset, min(total, offset + batch_size)))
            input_ids, labels, mask, batch_timings = _evaluation_batch(
                dataset,
                indices,
                tokenizer.pad_id,
                settings.sequence_length,
                pin_memory=device.type == "cuda",
            )
            for key, value in batch_timings.items():
                timings[key] += value
            valid_tokens = int((labels[:, 1:] != -100).sum().item())
            transfer_start_event = None
            transfer_end_event = None
            forward_start_event = None
            forward_end_event = None
            if device.type == "cuda":
                transfer_start_event = torch.cuda.Event(enable_timing=True)
                transfer_end_event = torch.cuda.Event(enable_timing=True)
                forward_start_event = torch.cuda.Event(enable_timing=True)
                forward_end_event = torch.cuda.Event(enable_timing=True)
                transfer_start_event.record()
            transfer_started = time.perf_counter()
            input_ids = input_ids.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            mask = mask.to(device, non_blocking=True)
            transfer_enqueue_duration = time.perf_counter() - transfer_started
            timings["host_to_device_enqueue_seconds"] += transfer_enqueue_duration
            if transfer_end_event is not None:
                transfer_end_event.record()
                assert forward_start_event is not None
                forward_start_event.record()
            forward_started = time.perf_counter()
            with torch.autocast(
                device_type=device.type,
                dtype=torch.bfloat16
                if settings.precision == "bf16"
                else torch.float16,
                enabled=device.type == "cuda" and settings.precision != "fp32",
            ):
                loss = model(
                    input_ids, labels=labels, attention_mask=mask
                )["loss"]
            forward_enqueue_duration = time.perf_counter() - forward_started
            timings["forward_enqueue_seconds"] += forward_enqueue_duration
            if forward_end_event is not None:
                forward_end_event.record()
            synchronisation_started = time.perf_counter()
            if not isinstance(loss, Tensor):
                raise RuntimeError("evaluation did not return a tensor loss")
            value = float(loss.detach().float().cpu().item())
            if device.type == "cuda":
                timings["cuda_synchronisation_seconds"] += (
                    time.perf_counter() - synchronisation_started
                )
                assert transfer_start_event is not None
                assert transfer_end_event is not None
                assert forward_start_event is not None
                assert forward_end_event is not None
                timings["host_to_device_seconds"] += (
                    transfer_start_event.elapsed_time(transfer_end_event) / 1000
                )
                timings["forward_seconds"] += (
                    forward_start_event.elapsed_time(forward_end_event) / 1000
                )
            else:
                timings["host_to_device_seconds"] += transfer_enqueue_duration
                timings["forward_seconds"] += forward_enqueue_duration
            if not math.isfinite(value):
                raise RuntimeError("evaluation produced a non-finite loss")
            reduction_started = time.perf_counter()
            losses.append((value, valid_tokens))
            timings["loss_reduction_seconds"] += time.perf_counter() - reduction_started
            completed += len(indices)
            timings["batches"] += 1
            elapsed = max(time.perf_counter() - started, 1e-9)
            rate = completed / elapsed
            now = time.perf_counter()
            if (
                completed >= total
                or now - last_progress_at >= progress_interval_seconds
            ):
                progress_started = time.perf_counter()
                progress.update(
                    "Evaluating validation records",
                    current=completed,
                    total=total,
                    result={
                        "records_completed": completed,
                        "total_records": total,
                        "records_per_second": round(rate, 3),
                        "elapsed_seconds": round(elapsed, 2),
                        "remaining_seconds": round((total - completed) / rate, 2)
                        if completed >= batch_size * 2
                        else None,
                        "batch_size": batch_size,
                    },
                )
                timings["progress_event_seconds"] += (
                    time.perf_counter() - progress_started
                )
                last_progress_at = now

    progress.update("Finalising evaluation", current=completed, total=total)
    total_weight = sum(weight for _, weight in losses)
    mean_loss = (
        sum(loss * weight for loss, weight in losses) / total_weight
        if total_weight
        else sum(loss for loss, _ in losses) / len(losses)
    )
    duration = time.perf_counter() - started
    report_payload = {
        "format": "salty-potato-evaluation-v3",
        "operation_id": settings.operation_id,
        "saved_version_id": settings.version_id,
        "checkpoint_id": settings.version_id,
        "checkpoint_path": str(Path(settings.checkpoint_path).resolve()),
        "weight_sha256": settings.expected_weight_sha256,
        "state": "completed",
        "records_completed": completed,
        "total_records": total,
        "mean_loss": mean_loss,


        "perplexity": perplexity_from_mean_loss(mean_loss),
        "duration_seconds": duration,
        "records_per_second": completed / max(duration, 1e-9),
        "tokens_evaluated": total_weight,
        "tokens_per_second": total_weight / max(duration, 1e-9),
        "batch_size": batch_size,
        "timings_seconds": {
            **timings,
            "report_write_seconds": None,
            "report_write_measurement_scope": (
                "returned result and durable database metrics"
            ),
        },
        "loss_contract": "token_natural_log_cross_entropy",
        "loss_numerator": "sum_token_natural_log_cross_entropy",
        "loss_denominator": "valid_target_tokens",
        "evaluation_contract": evaluation_contract,
        "evaluation_contract_digest": evaluation_contract["contract_digest"],
        "completed_at_unix": time.time(),
    }
    progress.update("Saving evaluation report", current=completed, total=total)
    report_path = Path(settings.report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_started = time.perf_counter()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{report_path.name}-", suffix=".tmp", dir=report_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report_payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, report_path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    timings["report_write_seconds"] = time.perf_counter() - report_started
    end_to_end_duration = time.perf_counter() - started
    result = EvaluationResult(
        operation_id=settings.operation_id,
        version_id=settings.version_id,
        state="completed",
        records_completed=completed,
        total_records=total,
        mean_loss=mean_loss,
        perplexity=report_payload["perplexity"],
        duration_seconds=duration,
        records_per_second=report_payload["records_per_second"],
        tokens_evaluated=total_weight,
        tokens_per_second=report_payload["tokens_per_second"],
        report_path=str(report_path),
        report_sha256=sha256_file(report_path),
        batch_size=batch_size,
        timings_seconds=timings,
        end_to_end_duration_seconds=end_to_end_duration,
        evaluation_contract=evaluation_contract,
        evaluation_contract_digest=evaluation_contract["contract_digest"],
    )
    return result


def _evaluation_batch(
    dataset: PreparedExamples,
    indices: list[int],
    pad_id: int,
    sequence_length: int,
    *,
    pin_memory: bool,
) -> tuple[Tensor, Tensor, Tensor, dict[str, float]]:
    """Collate one batch while keeping mutually exclusive timing buckets."""

    read_started = time.perf_counter()
    selected = [dataset.item(index) for index in indices]
    data_read_seconds = time.perf_counter() - read_started

    width = min(sequence_length, max(len(tokens) for tokens, _ in selected))
    padding_started = time.perf_counter()
    input_ids = torch.full((len(selected), width), pad_id, dtype=torch.long)
    labels = torch.full((len(selected), width), -100, dtype=torch.long)
    mask = torch.zeros((len(selected), width), dtype=torch.bool)
    padding_seconds = time.perf_counter() - padding_started

    collation_started = time.perf_counter()
    for row, (tokens, targets) in enumerate(selected):
        length = min(width, len(tokens))
        input_ids[row, :length] = torch.from_numpy(
            np.asarray(tokens[:length], dtype=np.int64)
        )
        labels[row, :length] = torch.from_numpy(
            np.asarray(targets[:length], dtype=np.int64)
        )
        mask[row, :length] = True
    collation_seconds = time.perf_counter() - collation_started

    pin_started = time.perf_counter()
    if pin_memory:
        input_ids = input_ids.pin_memory()
        labels = labels.pin_memory()
        mask = mask.pin_memory()
    pin_memory_seconds = time.perf_counter() - pin_started
    return input_ids, labels, mask, {
        "data_read_seconds": data_read_seconds,
        "padding_seconds": padding_seconds,
        "collation_seconds": collation_seconds,
        "pin_memory_seconds": pin_memory_seconds,
    }
