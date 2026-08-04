"""Parameter-train a Base Steak identity LoRA against its exact native GGUF.

This is deliberately not a prompt or response override. The native engine
provides final hidden states and base logits; PyTorch optimizes low-rank
parameters for the model's output projection, and the result is exported as a
standard GGUF LoRA that the same private runtime loads at inference time.
"""

from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import random
import time
from typing import Any, Iterable, Sequence

import gguf
import numpy as np
import torch
import torch.nn.functional as functional

from ..runtime.salty_native import SaltyNativeRuntime, SaltyNativeRuntimeError
from .base_steak_identity_dataset import (
    IdentityExample,
    dataset_jsonl,
    dataset_sha256,
)


IDENTITY_ADAPTER_SCHEMA = "base-steak-native-identity-adapter-v1"


@dataclass(frozen=True)
class NativeIdentitySettings:
    rank: int = 16
    epochs: int = 320
    learning_rate: float = 0.01
    weight_decay: float = 0.002
    retention_weight: float = 3.0
    adapter_l2_weight: float = 0.00002
    top_k_negatives: int = 64
    gradient_clip: float = 1.0
    seed: int = 20260819
    minimum_train_accuracy: float = 0.99



    minimum_holdout_accuracy: float = 0.82
    maximum_retention_delta: float = 0.00003
    minimum_epochs_before_early_stop: int = 300

    def __post_init__(self) -> None:
        if not 1 <= self.rank <= 128:
            raise ValueError("Identity adapter rank must be between 1 and 128")
        if not 1 <= self.epochs <= 10_000:
            raise ValueError("Identity adapter epochs must be between 1 and 10000")
        if not 1 <= self.minimum_epochs_before_early_stop <= self.epochs:
            raise ValueError(
                "Identity adapter minimum early-stop epoch must fit the epoch budget"
            )
        if not 0 < self.learning_rate <= 1:
            raise ValueError("Identity adapter learning rate is invalid")
        if not 1 <= self.top_k_negatives <= 512:
            raise ValueError("Identity adapter top-k negatives are invalid")
        if not 0 < self.maximum_retention_delta <= 0.01:
            raise ValueError("Identity adapter retention delta gate is invalid")


@dataclass(frozen=True)
class NativeTraceSample:
    example_id: str
    category: str
    hidden: np.ndarray
    candidates: np.ndarray
    base_logits: np.ndarray
    target_token: int | None


@dataclass(frozen=True)
class AdapterTrainingResult:
    lora_a: np.ndarray
    lora_b: np.ndarray
    metrics: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _token_ids(runtime: SaltyNativeRuntime, text: str) -> list[int]:
    return runtime._tokenize_bytes(
        text.encode("utf-8"),
        add_special=False,
        parse_special=True,
    )


def identity_vocabulary(
    runtime: SaltyNativeRuntime,
    examples: Iterable[IdentityExample],
) -> set[int]:
    tokens: set[int] = set()
    for example in examples:
        tokens.update(_token_ids(runtime, example.response))
    end_tokens = _token_ids(runtime, "<|im_end|>")
    if not end_tokens:
        raise SaltyNativeRuntimeError("The native tokenizer did not expose an end token")
    tokens.update(end_tokens)
    return tokens


def _trace_example(
    runtime: SaltyNativeRuntime,
    example: IdentityExample,
    *,
    core_candidates: set[int],
    top_k: int,
    retain: bool,
) -> list[NativeTraceSample]:
    if not runtime.loaded or runtime._api is None or runtime._context is None:
        raise SaltyNativeRuntimeError("Identity tracing requires a loaded native runtime")
    rendered, _ = runtime._format_chat(example.chat_messages(), "instant")
    prefix_tokens = runtime._tokenize(rendered)
    response_tokens = _token_ids(runtime, example.response)
    end_tokens = _token_ids(runtime, "<|im_end|>")
    labels = [*response_tokens, *end_tokens]
    full_tokens = [*prefix_tokens, *labels]
    if len(full_tokens) > runtime.profile.batch_size:
        raise ValueError(
            f"{example.id} uses {len(full_tokens)} tokens; increase the trace batch size"
        )

    dll = runtime._api.native
    memory = dll.llama_get_memory(runtime._context)
    dll.llama_memory_clear(memory, True)
    runtime._resident_tokens = []
    dll.llama_set_embeddings(runtime._context, True)
    token_buffer = (ctypes.c_int32 * len(full_tokens))(*full_tokens)
    batch = dll.llama_batch_get_one(token_buffer, len(full_tokens))
    status = int(dll.llama_decode(runtime._context, batch))
    if status != 0:
        raise SaltyNativeRuntimeError(
            f"Native identity trace decode failed for {example.id}: {status}"
        )

    hidden_size = int(dll.llama_model_n_embd(runtime._model))
    vocabulary_size = int(dll.llama_vocab_n_tokens(runtime._vocab))
    samples: list[NativeTraceSample] = []
    first_position = len(prefix_tokens) - 1
    for label_offset, target_token in enumerate(labels):
        position = first_position + label_offset
        hidden_pointer = dll.llama_get_embeddings_ith(runtime._context, position)
        logits_pointer = dll.llama_get_logits_ith(runtime._context, position)
        if not hidden_pointer or not logits_pointer:
            raise SaltyNativeRuntimeError(
                f"Native identity trace output {position} is unavailable for {example.id}"
            )
        hidden = np.ctypeslib.as_array(
            hidden_pointer,
            shape=(hidden_size,),
        ).astype(np.float32, copy=True)
        logits = np.ctypeslib.as_array(
            logits_pointer,
            shape=(vocabulary_size,),
        )
        negative_count = min(top_k, vocabulary_size)
        top_indices = np.argpartition(logits, -negative_count)[-negative_count:]
        candidates = np.array(
            sorted({*core_candidates, *(int(value) for value in top_indices), target_token}),
            dtype=np.int64,
        )
        base_logits = logits[candidates].astype(np.float32, copy=True)
        samples.append(
            NativeTraceSample(
                example_id=example.id,
                category=example.category,
                hidden=hidden,
                candidates=candidates,
                base_logits=base_logits,
                target_token=int(target_token),
            )
        )
    return samples


def collect_native_traces(
    runtime: SaltyNativeRuntime,
    *,
    training: Sequence[IdentityExample],
    holdout: Sequence[IdentityExample],
    retention: Sequence[IdentityExample],
    top_k: int,
    on_progress: Any = None,
) -> tuple[list[NativeTraceSample], list[NativeTraceSample], list[NativeTraceSample]]:
    all_identity = [*training, *holdout]
    core_candidates = identity_vocabulary(runtime, all_identity)
    total = len(training) + len(holdout) + len(retention)
    completed = 0

    def collect(
        examples: Sequence[IdentityExample],
        *,
        retain: bool,
    ) -> list[NativeTraceSample]:
        nonlocal completed
        output: list[NativeTraceSample] = []
        for example in examples:
            output.extend(
                _trace_example(
                    runtime,
                    example,
                    core_candidates=core_candidates,
                    top_k=top_k,
                    retain=retain,
                )
            )
            completed += 1
            if on_progress is not None:
                on_progress(completed, total, example.id)
        return output

    return (
        collect(training, retain=False),
        collect(holdout, retain=False),
        collect(retention, retain=True),
    )


def _padded_samples(
    samples: Sequence[NativeTraceSample],
    *,
    device: torch.device,
) -> dict[str, torch.Tensor]:
    if not samples:
        raise ValueError("Identity adapter training received no trace samples")
    width = max(len(sample.candidates) for sample in samples)
    candidates = np.zeros((len(samples), width), dtype=np.int64)
    logits = np.full((len(samples), width), -1e9, dtype=np.float32)
    mask = np.zeros((len(samples), width), dtype=np.bool_)
    targets = np.full((len(samples),), -1, dtype=np.int64)
    for row, sample in enumerate(samples):
        count = len(sample.candidates)
        candidates[row, :count] = sample.candidates
        logits[row, :count] = sample.base_logits
        mask[row, :count] = True
        if sample.target_token is not None:
            matches = np.flatnonzero(sample.candidates == sample.target_token)
            if len(matches) != 1:
                raise ValueError(f"{sample.example_id} target token is not unique")
            targets[row] = int(matches[0])
    return {
        "hidden": torch.as_tensor(
            np.stack([sample.hidden for sample in samples]),
            dtype=torch.float32,
            device=device,
        ),
        "candidates": torch.as_tensor(candidates, dtype=torch.long, device=device),
        "base_logits": torch.as_tensor(logits, dtype=torch.float32, device=device),
        "mask": torch.as_tensor(mask, dtype=torch.bool, device=device),
        "targets": torch.as_tensor(targets, dtype=torch.long, device=device),
    }


def _adapter_delta(
    hidden: torch.Tensor,
    candidates: torch.Tensor,
    lora_a: torch.Tensor,
    lora_b: torch.Tensor,
) -> torch.Tensor:
    projected = functional.linear(hidden, lora_a)
    selected_b = lora_b[candidates]
    return torch.einsum("nr,ncr->nc", projected, selected_b)


def _accuracy(
    tensors: dict[str, torch.Tensor],
    lora_a: torch.Tensor,
    lora_b: torch.Tensor,
) -> tuple[float, float]:
    with torch.no_grad():
        delta = _adapter_delta(
            tensors["hidden"],
            tensors["candidates"],
            lora_a,
            lora_b,
        )
        scores = (tensors["base_logits"] + delta).masked_fill(
            ~tensors["mask"],
            -torch.inf,
        )
        base_scores = tensors["base_logits"].masked_fill(
            ~tensors["mask"],
            -torch.inf,
        )
        target_positions = tensors["targets"]
        adapted = (scores.argmax(dim=1) == target_positions).float().mean()
        baseline = (base_scores.argmax(dim=1) == target_positions).float().mean()
        return float(baseline.item()), float(adapted.item())


def train_output_adapter(
    *,
    train_samples: Sequence[NativeTraceSample],
    holdout_samples: Sequence[NativeTraceSample],
    retention_samples: Sequence[NativeTraceSample],
    hidden_size: int,
    vocabulary_size: int,
    settings: NativeIdentitySettings,
    device_name: str = "cuda",
    on_epoch: Any = None,
) -> AdapterTrainingResult:
    if device_name == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for native identity post-training")
    device = torch.device(device_name)
    random.seed(settings.seed)
    np.random.seed(settings.seed)
    torch.manual_seed(settings.seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(settings.seed)

    train = _padded_samples(train_samples, device=device)
    holdout = _padded_samples(holdout_samples, device=device)
    retention = _padded_samples(retention_samples, device=device)
    generator = torch.Generator(device=device)
    generator.manual_seed(settings.seed)
    lora_a = torch.empty(
        (settings.rank, hidden_size),
        dtype=torch.float32,
        device=device,
    )
    torch.nn.init.normal_(lora_a, mean=0.0, std=0.01, generator=generator)
    lora_a.requires_grad_(True)
    lora_b = torch.zeros(
        (vocabulary_size, settings.rank),
        dtype=torch.float32,
        device=device,
        requires_grad=True,
    )
    optimiser = torch.optim.AdamW(
        (lora_a, lora_b),
        lr=settings.learning_rate,
        weight_decay=settings.weight_decay,
    )
    history: list[dict[str, float | int]] = []
    retention_basis = torch.linalg.qr(
        retention["hidden"].transpose(0, 1),
        mode="reduced",
    ).Q

    def preserve_retention_subspace() -> None:
        with torch.no_grad():




            for _ in range(8):
                lora_a.sub_(
                    (lora_a @ retention_basis)
                    @ retention_basis.transpose(0, 1)
                )

    preserve_retention_subspace()
    best_any: tuple[tuple[float, float, float], dict[str, Any], torch.Tensor, torch.Tensor] | None = None
    best_eligible: tuple[tuple[float, float, float], dict[str, Any], torch.Tensor, torch.Tensor] | None = None
    started = time.perf_counter()

    for epoch in range(1, settings.epochs + 1):
        optimiser.zero_grad(set_to_none=True)
        train_delta = _adapter_delta(
            train["hidden"], train["candidates"], lora_a, lora_b
        )
        train_scores = (train["base_logits"] + train_delta).masked_fill(
            ~train["mask"],
            -torch.inf,
        )
        supervised_loss = functional.cross_entropy(train_scores, train["targets"])
        retention_delta = _adapter_delta(
            retention["hidden"],
            retention["candidates"],
            lora_a,
            lora_b,
        )
        retention_loss = (
            retention_delta.masked_select(retention["mask"]).square().mean()
        )
        used_rows = torch.unique(
            torch.cat(
                (
                    train["candidates"][train["mask"]],
                    retention["candidates"][retention["mask"]],
                )
            )
        )
        regularisation = lora_a.square().mean() + lora_b[used_rows].square().mean()
        loss = (
            supervised_loss
            + settings.retention_weight * retention_loss
            + settings.adapter_l2_weight * regularisation
        )
        loss.backward()
        torch.nn.utils.clip_grad_norm_((lora_a, lora_b), settings.gradient_clip)
        optimiser.step()
        preserve_retention_subspace()

        if epoch == 1 or epoch % 5 == 0 or epoch == settings.epochs:
            baseline_train, adapted_train = _accuracy(train, lora_a, lora_b)
            baseline_holdout, adapted_holdout = _accuracy(holdout, lora_a, lora_b)
            with torch.no_grad():
                retain_delta = _adapter_delta(
                    retention["hidden"],
                    retention["candidates"],
                    lora_a,
                    lora_b,
                ).masked_select(retention["mask"])
                retain_rms = float(retain_delta.square().mean().sqrt().item())
                retain_max = float(retain_delta.abs().max().item())
            row: dict[str, float | int] = {
                "epoch": epoch,
                "loss": float(loss.detach().item()),
                "supervised_loss": float(supervised_loss.detach().item()),
                "retention_loss": float(retention_loss.detach().item()),
                "baseline_train_accuracy": baseline_train,
                "adapted_train_accuracy": adapted_train,
                "baseline_holdout_accuracy": baseline_holdout,
                "adapted_holdout_accuracy": adapted_holdout,
                "retention_delta_rms": retain_rms,
                "retention_delta_max": retain_max,
            }
            history.append(row)
            quality = (
                float(row["adapted_holdout_accuracy"]),
                float(row["adapted_train_accuracy"]),
                -float(row["retention_delta_rms"]),
            )
            snapshot = (
                quality,
                dict(row),
                lora_a.detach().clone(),
                lora_b.detach().clone(),
            )
            if best_any is None or quality > best_any[0]:
                best_any = snapshot
            if (
                float(row["adapted_train_accuracy"])
                >= settings.minimum_train_accuracy
                and float(row["retention_delta_max"])
                <= settings.maximum_retention_delta
                and (best_eligible is None or quality > best_eligible[0])
            ):
                best_eligible = snapshot
            if on_epoch is not None:
                on_epoch(dict(row))
            if (
                adapted_train >= settings.minimum_train_accuracy
                and adapted_holdout >= settings.minimum_holdout_accuracy
                and retain_max <= settings.maximum_retention_delta
                and epoch >= settings.minimum_epochs_before_early_stop
            ):
                break

    selected = best_eligible or best_any
    if selected is None:
        raise RuntimeError("Identity adapter training did not produce a checkpoint")
    _, final, best_a, best_b = selected
    with torch.no_grad():
        lora_a.copy_(best_a)
        lora_b.copy_(best_b)
    return AdapterTrainingResult(
        lora_a=lora_a.detach().cpu().numpy().astype(np.float32, copy=False),
        lora_b=lora_b.detach().cpu().numpy().astype(np.float32, copy=False),
        metrics={
            "device": str(device),
            "epochs_completed": int(final["epoch"]),
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "train_sample_count": len(train_samples),
            "holdout_sample_count": len(holdout_samples),
            "retention_sample_count": len(retention_samples),
            "history": history,
            "last": history[-1],
            "selection": "best_accuracy_checkpoint",
            "final": final,
        },
    )


def export_identity_adapter(
    output_path: Path,
    *,
    result: AdapterTrainingResult,
    settings: NativeIdentitySettings,
    base_model_sha256: str,
    train_dataset_sha256: str,
    holdout_dataset_sha256: str,
    retention_dataset_sha256: str,
) -> dict[str, Any]:
    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite {output_path}")
    writer = gguf.GGUFWriter(output_path, "steak20")
    writer.add_type(gguf.GGUFType.ADAPTER)
    writer.add_string(gguf.Keys.Adapter.TYPE, "lora")
    writer.add_float32(gguf.Keys.Adapter.LORA_ALPHA, float(settings.rank))
    writer.add_name("Base Steak 2.0 Identity Adapter")
    writer.add_author("MD Anik Hasan (Sawlper)")
    writer.add_version("1.0")
    writer.add_description(
        "Learned Base Steak 2.0 model identity and trainer attribution."
    )
    writer.add_string("base_steak.adapter.schema", IDENTITY_ADAPTER_SCHEMA)
    writer.add_string("base_steak.adapter.base_model_sha256", base_model_sha256)
    writer.add_string("base_steak.adapter.train_dataset_sha256", train_dataset_sha256)
    writer.add_string(
        "base_steak.adapter.holdout_dataset_sha256",
        holdout_dataset_sha256,
    )
    writer.add_string(
        "base_steak.adapter.retention_dataset_sha256",
        retention_dataset_sha256,
    )
    writer.add_tensor("output.weight.lora_a", result.lora_a)
    writer.add_tensor("output.weight.lora_b", result.lora_b)
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file(progress=True)
    writer.close()
    return {
        "schema": IDENTITY_ADAPTER_SCHEMA,
        "path": str(output_path),
        "sha256": _sha256(output_path),
        "size_bytes": output_path.stat().st_size,
        "rank": settings.rank,
        "alpha": settings.rank,
        "target_tensor": "output.weight",
        "base_model_sha256": base_model_sha256,
    }


def export_output_adapter(
    output_path: Path,
    *,
    result: AdapterTrainingResult,
    settings: NativeIdentitySettings,
    base_model_sha256: str,
    train_dataset_sha256: str,
    holdout_dataset_sha256: str,
    retention_dataset_sha256: str,
    adapter_schema: str,
    name: str,
    description: str,
) -> dict[str, Any]:
    """Export another app-owned output-projection LoRA with explicit metadata."""

    output_path = output_path.resolve()
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite {output_path}")
    writer = gguf.GGUFWriter(output_path, "steak20")
    writer.add_type(gguf.GGUFType.ADAPTER)
    writer.add_string(gguf.Keys.Adapter.TYPE, "lora")
    writer.add_float32(gguf.Keys.Adapter.LORA_ALPHA, float(settings.rank))
    writer.add_name(str(name))
    writer.add_author("MD Anik Hasan (Sawlper)")
    writer.add_version("1.0")
    writer.add_description(str(description))
    writer.add_string("base_steak.adapter.schema", str(adapter_schema))
    writer.add_string("base_steak.adapter.base_model_sha256", base_model_sha256)
    writer.add_string("base_steak.adapter.train_dataset_sha256", train_dataset_sha256)
    writer.add_string(
        "base_steak.adapter.holdout_dataset_sha256", holdout_dataset_sha256
    )
    writer.add_string(
        "base_steak.adapter.retention_dataset_sha256", retention_dataset_sha256
    )
    writer.add_tensor("output.weight.lora_a", result.lora_a)
    writer.add_tensor("output.weight.lora_b", result.lora_b)
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file(progress=True)
    writer.close()
    return {
        "schema": str(adapter_schema),
        "path": str(output_path),
        "sha256": _sha256(output_path),
        "size_bytes": output_path.stat().st_size,
        "rank": settings.rank,
        "alpha": settings.rank,
        "target_tensor": "output.weight",
        "base_model_sha256": base_model_sha256,
    }


def write_dataset_artifacts(
    output_directory: Path,
    *,
    training: Sequence[IdentityExample],
    holdout: Sequence[IdentityExample],
    retention: Sequence[IdentityExample],
) -> dict[str, dict[str, Any]]:
    output_directory.mkdir(parents=True, exist_ok=False)
    result: dict[str, dict[str, Any]] = {}
    for name, split, examples in (
        ("identity-train.jsonl", "train", training),
        ("identity-holdout.jsonl", "holdout", holdout),
        ("capability-retention.jsonl", "retention", retention),
    ):
        path = output_directory / name
        content = dataset_jsonl(examples, split)
        payload = content.encode("utf-8")
        path.write_bytes(payload)
        result[split] = {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "example_count": len(examples),
        }
    return result


__all__ = [
    "AdapterTrainingResult",
    "IDENTITY_ADAPTER_SCHEMA",
    "NativeIdentitySettings",
    "NativeTraceSample",
    "collect_native_traces",
    "export_identity_adapter",
    "export_output_adapter",
    "identity_vocabulary",
    "train_output_adapter",
    "write_dataset_artifacts",
]
