"""Real additional-training workflow with one replaceable recovery state."""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import torch
from torch import Tensor

from ..system.device import normalise_device, release_device
from ..system.files import atomic_write_json, sha256_file
from ..system.timestamps import utc_now_timestamp, utc_timestamp
from ..versions.checkpoint import (
    inspect_checkpoint,
    load_checkpoint,
    save_completed_checkpoint,
)
from ..versions.model import ModelConfig, SaltyPotato, create_initial_model
from ..versions.tokenizer import SaltyTokenizer


class TrainingProgress(Protocol):
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
class TrainingSettings:
    prepared_path: str
    prepared_dataset_id: str | None
    prepared_artifact_checksum: str | None
    tokenizer_path: str
    output_path: str
    recovery_path: str
    additional_steps: int
    operation_id: str | None = None
    starting_checkpoint: str | None = None
    starting_version_id: str | None = None
    starting_weight_sha256: str | None = None
    base_total_steps: int | None = 0
    dataset_id: str | None = None
    sequence_length: int = 512
    micro_batch_size: int = 16
    gradient_accumulation: int = 1
    learning_rate: float = 0.0003
    scheduler: str = "cosine"
    warmup_steps: int = 20
    weight_decay: float = 0.1
    gradient_clip: float = 1.0
    precision: str = "bf16"
    validation_interval: int = 50
    recovery_interval: int = 50
    seed: int = 1337
    device: str = "cuda"
    context_limit: int = 8_192
    resume_state: str | None = None

    def __post_init__(self) -> None:
        if self.additional_steps < 1:
            raise ValueError("additional training steps must be positive")
        if self.sequence_length < 16 or self.sequence_length > self.context_limit:
            raise ValueError("training sequence length is outside the model context")
        if self.micro_batch_size < 1 or self.gradient_accumulation < 1:
            raise ValueError("batch size and gradient accumulation must be positive")
        if self.scheduler not in {"cosine", "linear", "constant"}:
            raise ValueError("scheduler must be cosine, linear, or constant")
        if self.precision not in {"bf16", "fp16", "fp32"}:
            raise ValueError("precision must be bf16, fp16, or fp32")


def inspect_recovery_state(
    recovery_path: str | Path,
    *,
    verify_checksums: bool = False,
) -> dict[str, Any]:
    """Validate one app-created recovery directory without loading model tensors.

    The atomic directory swap and the verified file sizes are sufficient for a
    cheap setup-page check. Resume itself requests full SHA-256 verification
    before any optimizer state is restored.
    """

    root = Path(recovery_path).resolve()
    errors: list[str] = []
    required = (
        root / "manifest.json",
        root / "model.safetensors",
        root / "config.json",
        root / "training_state.pt",
        root / "tokenizer" / "tokenizer.model",
        root / "tokenizer" / "tokenizer.json",
    )
    for path in required:
        if not path.is_file():
            errors.append(f"missing recovery file: {path.name}")
    if errors:
        return {"valid": False, "errors": errors, "metadata": None}
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "valid": False,
            "errors": [f"unreadable recovery manifest: {exc}"],
            "metadata": None,
        }
    if manifest.get("format") != "salty-potato-recovery-v2":
        errors.append("unsupported recovery format")
    if manifest.get("verified_complete") is not True:
        errors.append("recovery was not committed as complete")
    integer_fields = (
        "additional_steps_target",
        "additional_steps_completed",
        "sequence_length",
        "seed",
        "weights_size_bytes",
        "state_size_bytes",
    )
    for name in integer_fields:
        if not isinstance(manifest.get(name), int):
            errors.append(f"recovery field is invalid: {name}")
    target = manifest.get("additional_steps_target")
    completed = manifest.get("additional_steps_completed")
    if isinstance(target, int) and isinstance(completed, int):
        if target < 1 or completed < 1 or completed > target:
            errors.append("recovery step boundary is not resumable")
    for name in (
        "prepared_dataset_id",
        "prepared_path",
        "prepared_artifact_checksum",
        "tokenizer_fingerprint",
        "training_settings",
        "weights_sha256",
        "state_sha256",
    ):
        if not manifest.get(name):
            errors.append(f"recovery identity is incomplete: {name}")
    if manifest.get("starting_version_id") and not manifest.get(
        "starting_weight_sha256"
    ):
        errors.append("recovery starting-version checksum is missing")
    if not (
        manifest.get("base_total_steps") is None
        or (
            isinstance(manifest.get("base_total_steps"), int)
            and manifest["base_total_steps"] >= 0
        )
    ):
        errors.append("recovery base total-step identity is invalid")
    if not isinstance(manifest.get("training_settings"), dict):
        errors.append("recovery training settings are invalid")
    weights = root / "model.safetensors"
    state = root / "training_state.pt"
    if isinstance(manifest.get("weights_size_bytes"), int):
        if weights.stat().st_size != manifest["weights_size_bytes"]:
            errors.append("recovery weight size differs from its manifest")
    if isinstance(manifest.get("state_size_bytes"), int):
        if state.stat().st_size != manifest["state_size_bytes"]:
            errors.append("recovery state size differs from its manifest")
    try:
        tokenizer = SaltyTokenizer(root / "tokenizer")
        if tokenizer.fingerprint != manifest.get("tokenizer_fingerprint"):
            errors.append("recovery tokenizer differs from its manifest")
    except Exception as exc:
        errors.append(f"invalid recovery tokenizer: {exc}")
    if verify_checksums:
        if sha256_file(weights) != manifest.get("weights_sha256"):
            errors.append("recovery weight checksum differs from its manifest")
        if sha256_file(state) != manifest.get("state_sha256"):
            errors.append("recovery state checksum differs from its manifest")
        try:
            state_payload = torch.load(
                state,
                map_location="cpu",
                weights_only=False,
            )
            required_state = {
                "optimizer",
                "scheduler",
                "scaler",
                "training",
                "cpu_rng_state",
                "cuda_rng_states",
                "python_rng_state",
                "numpy_rng_state",
                "data_order",
                "data_order_rng_state",
            }
            missing_state = required_state - set(state_payload)
            if missing_state:
                errors.append(
                    "recovery state is missing: "
                    + ", ".join(sorted(missing_state))
                )
            if state_payload.get("training") != {
                key: value
                for key, value in manifest.items()
                if key
                not in {
                    "format",
                    "weights_sha256",
                    "state_sha256",
                    "weights_size_bytes",
                    "state_size_bytes",
                    "verified_complete",
                    "committed_at_unix",
                    "committed_at",
                }
            }:
                errors.append("recovery state metadata differs from its manifest")
            training_device = str(
                manifest.get("training_settings", {}).get("device", "")
            ).lower()
            if training_device.startswith("cuda") and not state_payload.get(
                "cuda_rng_states"
            ):
                errors.append("CUDA recovery RNG state is missing")
        except Exception as exc:
            errors.append(f"unreadable recovery training state: {exc}")
    return {
        "valid": not errors,
        "errors": errors,
        "metadata": manifest if not errors else None,
    }


@dataclass
class TrainingResult:
    saved_path: str | None
    interrupted: bool
    additional_steps_completed: int
    total_trained_steps: int | None
    training_loss: float | None
    validation_loss: float | None
    validation_loss_scope: str
    validation_records_evaluated: int
    validation_target_tokens: int
    elapsed_seconds: float
    peak_vram_bytes: int
    source_valid_target_tokens_processed: dict[str, int]
    attempted_source_valid_target_tokens_processed: dict[str, int]
    source_dataset_target_tokens: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PreparedExamples:
    """Memory-mapped arrays produced only by the rebuilt prepared-data format."""

    def __init__(self, prepared_path: str | Path, split: str) -> None:
        root = Path(prepared_path)
        manifest_path = root / "manifest.json"



        self.manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file()
            else {}
        )
        self.data_order_policy = str(
            self.manifest.get("data_order_policy") or "deterministic_shuffle"
        )
        if self.data_order_policy not in {
            "deterministic_shuffle",
            "fixed_interleaved",
        }:
            raise ValueError(
                f"unsupported prepared data order policy: {self.data_order_policy}"
            )
        array_path = root / f"{split}-input-ids.npy"
        labels_path = root / f"{split}-labels.npy"
        lengths_path = root / f"{split}-lengths.npy"
        if not array_path.is_file() or not lengths_path.is_file():
            raise FileNotFoundError(f"verified prepared split is missing: {split}")
        self.array = np.load(array_path, mmap_mode="r", allow_pickle=False)
        if not labels_path.is_file():
            raise FileNotFoundError(
                f"prepared split has no durable target labels: {labels_path.name}"
            )
        self.labels = np.load(labels_path, mmap_mode="r", allow_pickle=False)
        self.lengths = np.load(lengths_path, mmap_mode="r", allow_pickle=False)
        if self.array.ndim != 2:
            raise ValueError(f"{array_path.name} must be a rank-two token array")
        if self.lengths.shape != (self.array.shape[0],):
            raise ValueError("prepared lengths do not match token array")
        if self.labels.shape != self.array.shape:
            raise ValueError("prepared labels do not match token array")
        if len(self) < 1:
            raise ValueError(f"prepared {split} split is empty")
        self.sources = ["prepared"] * len(self)
        split_manifest = self.manifest.get("splits", {}).get(split, {})
        provenance_name = split_manifest.get("provenance")
        if provenance_name:
            provenance_path = root / str(provenance_name)
            if not provenance_path.is_file():
                raise FileNotFoundError(
                    f"prepared split provenance is missing: {provenance_path.name}"
                )
            rows = [
                json.loads(line)
                for line in provenance_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            if len(rows) != len(self):
                raise ValueError("prepared provenance does not match sequence count")
            for index, row in enumerate(rows):
                source = row.get("curriculum_source")
                if source is None:
                    segments = row.get("segments") or []
                    if segments and isinstance(segments[0], dict):
                        source = segments[0].get("curriculum_source")
                if source is not None and str(source).strip():
                    self.sources[index] = str(source)

    def __len__(self) -> int:
        return int(self.array.shape[0])

    def item(self, index: int) -> tuple[list[int], list[int]]:
        length = int(self.lengths[index])
        if length < 2 or length > self.array.shape[1]:
            raise ValueError("prepared example has an invalid authoritative length")
        tokens = (
            self.array[index, :length].astype(np.int64, copy=False).tolist()
        )
        targets = (
            self.labels[index, :length]
            .astype(np.int64, copy=False)
            .tolist()
        )
        return tokens, targets

    def source(self, index: int) -> str:
        return self.sources[index]

    def target_count(self, index: int) -> int:
        length = int(self.lengths[index])
        return int(np.count_nonzero(self.labels[index, 1:length] != -100))


def _batch(
    dataset: PreparedExamples,
    indices: list[int],
    pad_id: int,
    sequence_length: int,
    *,
    pin_memory: bool,
) -> tuple[Tensor, Tensor, Tensor]:
    selected = [dataset.item(index) for index in indices]
    width = min(sequence_length, max(len(tokens) for tokens, _ in selected))
    input_ids = torch.full((len(selected), width), pad_id, dtype=torch.long)
    labels = torch.full((len(selected), width), -100, dtype=torch.long)
    mask = torch.zeros((len(selected), width), dtype=torch.bool)
    for row, (tokens, targets) in enumerate(selected):
        length = min(width, len(tokens))
        input_ids[row, :length] = torch.tensor(tokens[:length], dtype=torch.long)
        labels[row, :length] = torch.tensor(targets[:length], dtype=torch.long)
        mask[row, :length] = True
    if pin_memory:
        input_ids = input_ids.pin_memory()
        labels = labels.pin_memory()
        mask = mask.pin_memory()
    return input_ids, labels, mask


def _cosine_scheduler(
    optimizer: torch.optim.Optimizer, total_steps: int, warmup_steps: int
) -> torch.optim.lr_scheduler.LambdaLR:
    def multiplier(step: int) -> float:
        if warmup_steps and step < warmup_steps:
            return max(1e-8, step / warmup_steps)
        progress = (step - warmup_steps) / max(1, total_steps - warmup_steps)
        return 0.1 + 0.45 * (1 + math.cos(math.pi * min(1.0, progress)))

    return torch.optim.lr_scheduler.LambdaLR(optimizer, multiplier)


def _linear_scheduler(
    optimizer: torch.optim.Optimizer, total_steps: int, warmup_steps: int
) -> torch.optim.lr_scheduler.LambdaLR:
    def multiplier(step: int) -> float:
        if warmup_steps and step < warmup_steps:
            return max(1e-8, step / warmup_steps)
        progress = (step - warmup_steps) / max(1, total_steps - warmup_steps)
        return max(0.1, 1.0 - 0.9 * min(1.0, progress))

    return torch.optim.lr_scheduler.LambdaLR(optimizer, multiplier)


def _atomic_recovery(
    destination: Path,
    model: SaltyPotato,
    tokenizer_path: Path,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler,
    scaler: torch.amp.GradScaler,
    metadata: dict[str, Any],
    data_order: list[int],
    data_order_rng_state: object,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".recovery-next-", dir=destination.parent)
    )
    previous = destination.parent / ".recovery-previous"
    try:
        from safetensors.torch import save_file

        recovery_weights = {
            key: value.detach().cpu().contiguous()
            for key, value in model.checkpoint_state().items()
        }
        nonfinite = [
            key
            for key, value in recovery_weights.items()
            if not bool(torch.isfinite(value).all().item())
        ]
        if nonfinite:
            raise RuntimeError(
                "training recovery contains non-finite weights: "
                + ", ".join(nonfinite[:4])
            )
        save_file(recovery_weights, str(temporary / "model.safetensors"))
        del recovery_weights
        model.config.to_file(temporary / "config.json")
        shutil.copytree(tokenizer_path, temporary / "tokenizer")
        state = {
            "format": "salty-potato-recovery-v2",
            "optimizer": optimizer.state_dict(),
            "scheduler": scheduler.state_dict(),
            "scaler": scaler.state_dict(),
            "training": metadata,
            "cpu_rng_state": torch.get_rng_state(),
            "cuda_rng_states": (
                torch.cuda.get_rng_state_all() if torch.cuda.is_available() else []
            ),
            "python_rng_state": random.getstate(),
            "numpy_rng_state": np.random.get_state(),
            "data_order": list(data_order),
            "data_order_rng_state": data_order_rng_state,
        }
        torch.save(state, temporary / "training_state.pt")
        manifest = {
            **metadata,
            "format": "salty-potato-recovery-v2",
            "weights_sha256": sha256_file(temporary / "model.safetensors"),
            "state_sha256": sha256_file(temporary / "training_state.pt"),
            "weights_size_bytes": (temporary / "model.safetensors").stat().st_size,
            "state_size_bytes": (temporary / "training_state.pt").stat().st_size,
            "verified_complete": True,
            "committed_at": utc_now_timestamp(),
        }
        atomic_write_json(temporary / "manifest.json", manifest)
        if previous.exists():
            shutil.rmtree(previous)
        if destination.exists():
            destination.replace(previous)
        temporary.replace(destination)
        shutil.rmtree(previous, ignore_errors=True)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if not destination.exists() and previous.exists():
            previous.replace(destination)
        raise


def _restore_recovery(
    state_path: Path,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler,
    scaler: torch.amp.GradScaler,
) -> dict[str, Any]:
    payload = torch.load(state_path, map_location="cpu", weights_only=False)
    if payload.get("format") != "salty-potato-recovery-v2":
        raise ValueError("unsupported optimizer recovery format")
    for required in (
        "optimizer",
        "scheduler",
        "training",
        "data_order",
        "data_order_rng_state",
    ):
        if required not in payload:
            raise ValueError(f"recovery state is incomplete: {required}")
    optimizer.load_state_dict(payload["optimizer"])
    scheduler.load_state_dict(payload["scheduler"])
    if payload.get("scaler"):
        scaler.load_state_dict(payload["scaler"])
    if payload.get("cpu_rng_state") is not None:
        torch.set_rng_state(payload["cpu_rng_state"])
    if torch.cuda.is_available() and payload.get("cuda_rng_states"):
        torch.cuda.set_rng_state_all(payload["cuda_rng_states"])
    if payload.get("python_rng_state") is not None:
        random.setstate(payload["python_rng_state"])
    if payload.get("numpy_rng_state") is not None:
        np.random.set_state(payload["numpy_rng_state"])
    metadata = dict(payload.get("training") or {})
    metadata["_data_order"] = list(payload["data_order"])
    metadata["_data_order_rng_state"] = payload["data_order_rng_state"]
    return metadata


@torch.inference_mode()
def _validation_loss(
    model: SaltyPotato,
    dataset: PreparedExamples,
    tokenizer: SaltyTokenizer,
    settings: TrainingSettings,
    device: torch.device,
) -> tuple[float | None, int, int]:
    model.eval()
    loss_numerator = 0.0
    valid_target_tokens = 0
    maximum = min(len(dataset), 64)
    for offset in range(0, maximum, settings.micro_batch_size):
        indices = list(
            range(offset, min(maximum, offset + settings.micro_batch_size))
        )
        input_ids, labels, mask = _batch(
            dataset,
            indices,
            tokenizer.pad_id,
            settings.sequence_length,
            pin_memory=device.type == "cuda",
        )
        batch_targets = int((labels[:, 1:] != -100).sum().item())
        input_ids = input_ids.to(device, non_blocking=True)
        labels = labels.to(device, non_blocking=True)
        mask = mask.to(device, non_blocking=True)
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
        if loss is not None and torch.isfinite(loss):
            loss_numerator += float(loss.item()) * batch_targets
            valid_target_tokens += batch_targets
    model.train()
    return (
        (
            loss_numerator / valid_target_tokens
            if valid_target_tokens
            else None
        ),
        maximum,
        valid_target_tokens,
    )


def run_training(
    settings: TrainingSettings, progress: TrainingProgress
) -> TrainingResult:
    resolved = normalise_device(
        settings.device,
        precision=settings.precision,
        make_current=True,
        reset_peak_memory=True,
    )
    try:
        return _run_training(settings, progress, resolved.device)
    finally:
        release_device(resolved)


def _run_training(
    settings: TrainingSettings,
    progress: TrainingProgress,
    device: torch.device,
) -> TrainingResult:
    started = time.perf_counter()
    trusted_base = settings.base_total_steps is not None
    base_steps = int(settings.base_total_steps or 0)
    random.seed(settings.seed)
    np.random.seed(settings.seed)
    torch.manual_seed(settings.seed)
    progress.update("Preparing data")
    train_data = PreparedExamples(settings.prepared_path, "train")
    validation_data = PreparedExamples(settings.prepared_path, "validation")
    tokenizer = SaltyTokenizer(settings.tokenizer_path)

    progress.update("Loading model")
    resume_root = (
        Path(settings.resume_state).parent
        if settings.resume_state
        else None
    )
    model_checkpoint = (
        resume_root
        if resume_root and (resume_root / "model.safetensors").is_file()
        else Path(settings.starting_checkpoint)
        if settings.starting_checkpoint
        else None
    )
    if model_checkpoint:
        if not resume_root and settings.starting_weight_sha256:
            starting_report = inspect_checkpoint(
                model_checkpoint, calculate_checksum=True
            )
            if (
                not starting_report.valid
                or starting_report.weight_sha256
                != settings.starting_weight_sha256
            ):
                raise ValueError(
                    "starting version files differ from the saved-version identity"
                )
        model, checkpoint_tokenizer = load_checkpoint(
            model_checkpoint,
            device=device,
            dtype=torch.float32,
            context_limit=settings.context_limit,
        )
        if checkpoint_tokenizer.fingerprint != tokenizer.fingerprint:
            raise ValueError("prepared data tokenizer differs from starting version")
    else:
        config = ModelConfig(
            max_position_embeddings=settings.context_limit,
            use_gradient_checkpointing=True,
        )
        model = create_initial_model(config, device=device, seed=settings.seed)
    model.train()

    fused = device.type == "cuda"
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=settings.learning_rate,
        weight_decay=settings.weight_decay,
        fused=fused,
    )
    exact_resume = settings.resume_state is not None
    schedule_steps = settings.additional_steps
    warmup = min(settings.warmup_steps, max(0, schedule_steps - 1))
    scheduler: torch.optim.lr_scheduler.LRScheduler
    if settings.scheduler == "cosine":
        scheduler = _cosine_scheduler(optimizer, schedule_steps, warmup)
    elif settings.scheduler == "linear":
        scheduler = _linear_scheduler(optimizer, schedule_steps, warmup)
    else:
        scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer, lambda _step: 1.0
        )
    scaler = torch.amp.GradScaler(
        "cuda", enabled=device.type == "cuda" and settings.precision == "fp16"
    )
    resumed_metadata: dict[str, Any] = {}
    if exact_resume:
        resumed_metadata = _restore_recovery(
            Path(settings.resume_state), optimizer, scheduler, scaler
        )
        expected_identity = {
            "starting_version_id": settings.starting_version_id,
            "starting_weight_sha256": settings.starting_weight_sha256,
            "dataset_id": settings.dataset_id,
            "prepared_dataset_id": settings.prepared_dataset_id,
            "prepared_path": str(Path(settings.prepared_path).resolve()),
            "prepared_artifact_checksum": settings.prepared_artifact_checksum,
            "tokenizer_fingerprint": tokenizer.fingerprint,
            "base_total_steps": settings.base_total_steps,
            "additional_steps_target": settings.additional_steps,
            "sequence_length": settings.sequence_length,
            "seed": settings.seed,
        }
        for name, expected_value in expected_identity.items():
            if resumed_metadata.get(name) != expected_value:
                raise ValueError(f"recovery identity differs for {name}")
        expected_training_settings = {
            "sequence_length": settings.sequence_length,
            "micro_batch_size": settings.micro_batch_size,
            "gradient_accumulation": settings.gradient_accumulation,
            "learning_rate": settings.learning_rate,
            "scheduler": settings.scheduler,
            "warmup_steps": settings.warmup_steps,
            "weight_decay": settings.weight_decay,
            "gradient_clip": settings.gradient_clip,
            "precision": settings.precision,
            "validation_interval": settings.validation_interval,
            "recovery_interval": settings.recovery_interval,
            "seed": settings.seed,
            "device": settings.device,
            "context_limit": settings.context_limit,
        }
        if resumed_metadata.get("training_settings") != expected_training_settings:
            raise ValueError("recovery training settings differ from the exact run")

    order_generator = random.Random(settings.seed)
    if exact_resume:
        indices = [int(value) for value in resumed_metadata["_data_order"]]
        if sorted(indices) != list(range(len(train_data))):
            raise ValueError("recovery data order does not match prepared training data")
        order_generator.setstate(resumed_metadata["_data_order_rng_state"])
    else:
        indices = list(range(len(train_data)))
        if train_data.data_order_policy == "deterministic_shuffle":
            order_generator.shuffle(indices)
    cursor = int(resumed_metadata.get("batch_cursor", 0)) % len(indices)
    completed = int(resumed_metadata.get("additional_steps_completed", 0))
    if completed < 0 or completed > settings.additional_steps:
        raise ValueError("recovery completed-step boundary is invalid")
    initial_completed = completed
    latest_loss = resumed_metadata.get("training_loss")
    latest_validation = resumed_metadata.get("validation_loss")
    validation_loss_scope = (
        "deterministic_prefix_up_to_64_validation_sequences"
    )
    latest_validation_records = int(
        resumed_metadata.get("validation_records_evaluated", 0)
    )
    latest_validation_target_tokens = int(
        resumed_metadata.get("validation_target_tokens", 0)
    )
    tokens_processed = int(resumed_metadata.get("tokens_processed", 0))
    target_tokens_processed = int(
        resumed_metadata.get("valid_target_tokens_processed", 0)
    )
    attempted_tokens_processed = int(
        resumed_metadata.get("attempted_tokens_processed", tokens_processed)
    )
    attempted_target_tokens_processed = int(
        resumed_metadata.get(
            "attempted_valid_target_tokens_processed",
            target_tokens_processed,
        )
    )
    source_dataset_target_tokens: dict[str, int] = {}
    for index in range(len(train_data)):
        source = train_data.source(index)
        source_dataset_target_tokens[source] = (
            source_dataset_target_tokens.get(source, 0)
            + train_data.target_count(index)
        )
    source_target_tokens_processed = {
        str(key): int(value)
        for key, value in (
            resumed_metadata.get("source_valid_target_tokens_processed") or {}
        ).items()
    }
    attempted_source_target_tokens_processed = {
        str(key): int(value)
        for key, value in (
            resumed_metadata.get(
                "attempted_source_valid_target_tokens_processed"
            )
            or {}
        ).items()
    }
    if target_tokens_processed and not source_target_tokens_processed:
        if len(source_dataset_target_tokens) != 1:
            raise ValueError(
                "recovery is missing mixed-curriculum source-token counters"
            )
        only_source = next(iter(source_dataset_target_tokens))
        source_target_tokens_processed[only_source] = target_tokens_processed
    if (
        attempted_target_tokens_processed
        and not attempted_source_target_tokens_processed
    ):
        if len(source_dataset_target_tokens) != 1:
            raise ValueError(
                "recovery is missing attempted mixed-curriculum counters"
            )
        only_source = next(iter(source_dataset_target_tokens))
        attempted_source_target_tokens_processed[only_source] = (
            attempted_target_tokens_processed
        )
    amp_skipped_updates = int(
        resumed_metadata.get("amp_skipped_updates", 0)
    )
    train_target_tokens = sum(
        sum(1 for target in train_data.item(index)[1][1:] if target != -100)
        for index in range(len(train_data))
    )
    if train_target_tokens < 1:
        raise ValueError("prepared training split has no valid target tokens")
    latest_loss_numerator = resumed_metadata.get("training_loss_numerator")
    latest_loss_denominator = resumed_metadata.get("training_loss_denominator")
    latest_gradient: dict[str, Any] | None = None
    latest_recovery_save = utc_timestamp(resumed_metadata.get("committed_at")) or utc_timestamp(
        resumed_metadata.get("committed_at_unix")
    )
    optimizer.zero_grad(set_to_none=True)
    progress.update(
        "Training",
        current=completed,
        total=settings.additional_steps,
        result={
            "total_trained_steps": (
                base_steps + completed if trusted_base else None
            )
        },
    )

    while completed < settings.additional_steps:
        step_started = time.perf_counter()
        step_cursor_before = cursor
        step_indices_before = list(indices)
        step_order_state_before = order_generator.getstate()
        step_cpu_rng_before = torch.get_rng_state()
        step_cuda_rng_before = (
            torch.cuda.get_rng_state_all() if device.type == "cuda" else []
        )
        accumulated_numerator = 0.0
        step_tokens = 0
        step_batches: list[tuple[Tensor, Tensor, Tensor, int]] = []
        step_source_targets: dict[str, int] = {}
        for _ in range(settings.gradient_accumulation):
            chosen: list[int] = []
            for _row in range(settings.micro_batch_size):
                if cursor >= len(indices):
                    if (
                        train_data.data_order_policy
                        == "deterministic_shuffle"
                    ):
                        order_generator.shuffle(indices)
                    cursor = 0
                chosen.append(indices[cursor])
                cursor += 1
            for index in chosen:
                source = train_data.source(index)
                step_source_targets[source] = (
                    step_source_targets.get(source, 0)
                    + train_data.target_count(index)
                )
            input_ids, labels, mask = _batch(
                train_data,
                chosen,
                tokenizer.pad_id,
                settings.sequence_length,
                pin_memory=device.type == "cuda",
            )
            step_tokens += int(mask.sum().item())
            valid_targets = int((labels[:, 1:] != -100).sum().item())
            if valid_targets < 1:
                raise RuntimeError("training microbatch has no valid target tokens")
            step_batches.append((input_ids, labels, mask, valid_targets))
        step_valid_targets = sum(batch[3] for batch in step_batches)
        for input_ids, labels, mask, valid_targets in step_batches:
            input_ids = input_ids.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            mask = mask.to(device, non_blocking=True)
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
                assert isinstance(loss, Tensor)
                if not bool(torch.isfinite(loss).item()):
                    raise RuntimeError("training produced a non-finite loss")
                scaled_loss = loss * (valid_targets / step_valid_targets)
            scaler.scale(scaled_loss).backward()
            accumulated_numerator += float(loss.detach().item()) * valid_targets
        scaler.unscale_(optimizer)
        parameters_with_grad = [
            parameter for parameter in model.parameters() if parameter.grad is not None
        ]
        collect_gradient_counts = completed == 0 or (
            (completed + 1) % max(1, settings.recovery_interval) == 0
        )
        zero_gradient_parameters = (
            sum(
                1
                for parameter in parameters_with_grad
                if not bool(torch.count_nonzero(parameter.grad).item())
            )
            if collect_gradient_counts
            else None
        )
        nonfinite_gradient_parameters = (
            sum(
                1
                for parameter in parameters_with_grad
                if not bool(torch.isfinite(parameter.grad).all().item())
            )
            if collect_gradient_counts
            else 0
        )
        gradient_norm = torch.nn.utils.clip_grad_norm_(
            model.parameters(), settings.gradient_clip
        )
        gradient_norm_value = float(gradient_norm.detach().float().cpu().item())
        amp_scaler_enabled = bool(scaler.is_enabled())
        gradient_nonfinite = (
            not math.isfinite(gradient_norm_value)
            or bool(nonfinite_gradient_parameters)
        )
        if gradient_nonfinite and not amp_scaler_enabled:
            raise RuntimeError("training produced a non-finite gradient")
        scale_before = float(scaler.get_scale())
        scaler.step(optimizer)
        scaler.update()
        scale_after = float(scaler.get_scale())
        amp_update_skipped = scale_after < scale_before
        if gradient_nonfinite and not amp_update_skipped:
            raise RuntimeError(
                "FP16 scaler did not reject a non-finite gradient"
            )
        attempted_tokens_processed += step_tokens
        attempted_target_tokens_processed += step_valid_targets
        for source, count in step_source_targets.items():
            attempted_source_target_tokens_processed[source] = (
                attempted_source_target_tokens_processed.get(source, 0)
                + count
            )
        latest_gradient = {
            "gradient_norm_before_clip": gradient_norm_value,
            "gradient_clip_threshold": settings.gradient_clip,
            "gradient_clipped": gradient_norm_value > settings.gradient_clip,
            "parameters_with_gradients": len(parameters_with_grad),
            "parameters_with_zero_gradients": zero_gradient_parameters,
            "parameters_with_nonfinite_gradients": nonfinite_gradient_parameters,
            "loss_scale_before": scale_before,
            "loss_scale_after": scale_after,
            "amp_update_skipped": amp_update_skipped,
        }
        optimizer.zero_grad(set_to_none=True)
        if amp_update_skipped:
            amp_skipped_updates += 1
            cursor = step_cursor_before
            indices[:] = step_indices_before
            order_generator.setstate(step_order_state_before)
            torch.set_rng_state(step_cpu_rng_before)
            if device.type == "cuda" and step_cuda_rng_before:
                torch.cuda.set_rng_state_all(step_cuda_rng_before)
            progress.update(
                "Retrying skipped FP16 update",
                current=completed,
                total=settings.additional_steps,
                result={
                    "current_training_step": completed,
                    "target_steps": settings.additional_steps,
                    "amp_update_skipped": True,
                    "amp_skipped_updates": amp_skipped_updates,
                    "loss_scale_before": scale_before,
                    "loss_scale_after": scale_after,
                    "attempted_valid_target_tokens_processed": (
                        attempted_target_tokens_processed
                    ),
                    "valid_target_tokens_processed": target_tokens_processed,
                    "source_valid_target_tokens_processed": (
                        source_target_tokens_processed
                    ),
                    "attempted_source_valid_target_tokens_processed": (
                        attempted_source_target_tokens_processed
                    ),
                },
            )
            continue
        scheduler.step()
        completed += 1
        tokens_processed += step_tokens
        target_tokens_processed += step_valid_targets
        for source, count in step_source_targets.items():
            source_target_tokens_processed[source] = (
                source_target_tokens_processed.get(source, 0) + count
            )
        latest_loss_numerator = accumulated_numerator
        latest_loss_denominator = step_valid_targets
        latest_loss = accumulated_numerator / step_valid_targets
        elapsed = time.perf_counter() - started
        step_seconds = max(time.perf_counter() - step_started, 1e-9)
        current_total = base_steps + completed if trusted_base else None

        if (
            settings.validation_interval > 0
            and completed % settings.validation_interval == 0
        ):
            progress.update(
                "Checking training loss", current=completed, total=settings.additional_steps
            )
            (
                latest_validation,
                latest_validation_records,
                latest_validation_target_tokens,
            ) = _validation_loss(
                model, validation_data, tokenizer, settings, device
            )

        stop = progress.stop_requested()
        should_recover = (
            stop
            or completed % settings.recovery_interval == 0
            or completed == settings.additional_steps
        )
        if should_recover:
            progress.update(
                "Stop requested" if stop else "Saving recovery state",
                current=completed,
                total=settings.additional_steps,
            )
            _atomic_recovery(
                Path(settings.recovery_path),
                model,
                Path(settings.tokenizer_path),
                optimizer,
                scheduler,
                scaler,
                {
                    "starting_version_id": settings.starting_version_id,
                    "starting_checkpoint_path": (
                        str(Path(settings.starting_checkpoint).resolve())
                        if settings.starting_checkpoint
                        else None
                    ),
                    "starting_weight_sha256": settings.starting_weight_sha256,
                    "dataset_id": settings.dataset_id,
                    "prepared_dataset_id": settings.prepared_dataset_id,
                    "prepared_path": str(Path(settings.prepared_path).resolve()),
                    "prepared_artifact_checksum": settings.prepared_artifact_checksum,
                    "tokenizer_fingerprint": tokenizer.fingerprint,
                    "base_total_steps": settings.base_total_steps,
                    "additional_steps_target": settings.additional_steps,
                    "additional_steps_completed": completed,
                    "total_trained_steps": current_total,
                    "training_loss": latest_loss,
                    "training_loss_numerator": latest_loss_numerator,
                    "training_loss_denominator": latest_loss_denominator,
                    "training_loss_scope": (
                        "last_optimizer_step_token_weighted_mean"
                    ),
                    "validation_loss": latest_validation,
                    "validation_loss_scope": validation_loss_scope,
                    "validation_records_evaluated": (
                        latest_validation_records
                    ),
                    "validation_target_tokens": (
                        latest_validation_target_tokens
                    ),
                    "tokens_processed": tokens_processed,
                    "valid_target_tokens_processed": target_tokens_processed,
                    "attempted_tokens_processed": attempted_tokens_processed,
                    "attempted_valid_target_tokens_processed": (
                        attempted_target_tokens_processed
                    ),
                    "source_valid_target_tokens_processed": (
                        source_target_tokens_processed
                    ),
                    "attempted_source_valid_target_tokens_processed": (
                        attempted_source_target_tokens_processed
                    ),
                    "source_dataset_target_tokens": (
                        source_dataset_target_tokens
                    ),
                    "amp_skipped_updates": amp_skipped_updates,
                    "batch_cursor": cursor,
                    "sequence_length": settings.sequence_length,
                    "seed": settings.seed,
                    "training_settings": {
                        "sequence_length": settings.sequence_length,
                        "micro_batch_size": settings.micro_batch_size,
                        "gradient_accumulation": settings.gradient_accumulation,
                        "learning_rate": settings.learning_rate,
                        "scheduler": settings.scheduler,
                        "warmup_steps": settings.warmup_steps,
                        "weight_decay": settings.weight_decay,
                        "gradient_clip": settings.gradient_clip,
                        "precision": settings.precision,
                        "validation_interval": settings.validation_interval,
                        "recovery_interval": settings.recovery_interval,
                        "seed": settings.seed,
                        "device": settings.device,
                        "context_limit": settings.context_limit,
                    },
                },
                indices,
                order_generator.getstate(),
            )
            latest_recovery_save = utc_now_timestamp()
        progress.update(
            "Training" if not stop else "Finishing",
            current=completed,
            total=settings.additional_steps,
            result={
                "current_training_step": completed,
                "current_step": completed,
                "target_steps": settings.additional_steps,
                "total_trained_steps": current_total,
                "training_loss": latest_loss,
                "training_loss_numerator": latest_loss_numerator,
                "training_loss_denominator": latest_loss_denominator,
                "training_loss_scope": "last_optimizer_step_token_weighted_mean",
                "validation_loss": latest_validation,
                "validation_loss_scope": validation_loss_scope,
                "validation_records_evaluated": latest_validation_records,
                "validation_target_tokens": latest_validation_target_tokens,
                "valid_target_tokens_processed": target_tokens_processed,
                "attempted_valid_target_tokens_processed": (
                    attempted_target_tokens_processed
                ),
                "source_valid_target_tokens_processed": (
                    source_target_tokens_processed
                ),
                "attempted_source_valid_target_tokens_processed": (
                    attempted_source_target_tokens_processed
                ),
                "source_dataset_target_tokens": source_dataset_target_tokens,
                "amp_skipped_updates": amp_skipped_updates,
                "train_valid_target_tokens": train_target_tokens,
                "approximate_dataset_passes": round(
                    target_tokens_processed / train_target_tokens, 6
                ),
                "gradient": latest_gradient,
                "learning_rate": scheduler.get_last_lr()[0],
                "tokens_per_second": round(step_tokens / step_seconds, 2),
                "milliseconds_per_step": round(step_seconds * 1000, 2),
                "elapsed_seconds": round(elapsed, 2),
                "remaining_seconds": round(
                    (elapsed / (completed - initial_completed))
                    * (settings.additional_steps - completed),
                    2,
                )
                if completed - initial_completed >= 3
                else None,
                "latest_recovery_save": latest_recovery_save,
                "vram_bytes": (
                    int(torch.cuda.memory_allocated(device))
                    if device.type == "cuda"
                    else 0
                ),
                "vram_used_bytes": (
                    int(torch.cuda.memory_allocated(device))
                    if device.type == "cuda"
                    else 0
                ),
                "next_validation": (
                    settings.validation_interval
                    - completed % settings.validation_interval
                    if settings.validation_interval
                    else None
                ),
                "next_validation_step": (
                    completed
                    + settings.validation_interval
                    - completed % settings.validation_interval
                    if settings.validation_interval
                    else None
                ),
            },
        )
        if stop:
            peak = (
                int(torch.cuda.max_memory_allocated(device))
                if device.type == "cuda"
                else 0
            )
            return TrainingResult(
                saved_path=None,
                interrupted=True,
                additional_steps_completed=completed,
                total_trained_steps=current_total,
                training_loss=latest_loss,
                validation_loss=latest_validation,
                validation_loss_scope=validation_loss_scope,
                validation_records_evaluated=latest_validation_records,
                validation_target_tokens=latest_validation_target_tokens,
                elapsed_seconds=time.perf_counter() - started,
                peak_vram_bytes=peak,
                source_valid_target_tokens_processed=dict(
                    source_target_tokens_processed
                ),
                attempted_source_valid_target_tokens_processed=dict(
                    attempted_source_target_tokens_processed
                ),
                source_dataset_target_tokens=dict(
                    source_dataset_target_tokens
                ),
            )

    progress.update(
        "Saving version",
        current=settings.additional_steps,
        total=settings.additional_steps,
    )
    model.eval()
    save_completed_checkpoint(
        settings.output_path,
        model,
        settings.tokenizer_path,
        total_steps=base_steps + completed if trusted_base else None,
        additional_steps=completed,
        dataset_id=settings.dataset_id,
        operation_id=settings.operation_id,
        prepared_dataset_id=settings.prepared_dataset_id,
        starting_version_id=settings.starting_version_id,
    )
    peak = (
        int(torch.cuda.max_memory_allocated(device))
        if device.type == "cuda"
        else 0
    )
    return TrainingResult(
        saved_path=str(Path(settings.output_path).resolve()),
        interrupted=False,
        additional_steps_completed=completed,
        total_trained_steps=base_steps + completed if trusted_base else None,
        training_loss=latest_loss,
        validation_loss=latest_validation,
        validation_loss_scope=validation_loss_scope,
        validation_records_evaluated=latest_validation_records,
        validation_target_tokens=latest_validation_target_tokens,
        elapsed_seconds=time.perf_counter() - started,
        peak_vram_bytes=peak,
        source_valid_target_tokens_processed=dict(
            source_target_tokens_processed
        ),
        attempted_source_valid_target_tokens_processed=dict(
            attempted_source_target_tokens_processed
        ),
        source_dataset_target_tokens=dict(source_dataset_target_tokens),
    )
