"""Transactional in-process runtime bound to one exact saved version."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import torch

from ..system.device import normalise_device, release_device
from ..versions.checkpoint import inspect_checkpoint, load_checkpoint
from ..versions.model import SaltyPotato
from ..versions.tokenizer import PromptBudget, SaltyTokenizer


class ActivationProgress(Protocol):
    def update(
        self,
        phase: str,
        *,
        current: int | float | None = None,
        total: int | float | None = None,
        result: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class RuntimeIdentity:
    runtime_id: str
    checkpoint_id: str
    saved_version_label: str
    model_name: str
    total_trained_steps: int | None
    checkpoint_path: str
    weight_sha256: str
    context_limit: int
    device: str
    precision: str
    artifact_path: str
    tokenizer_sha256: str | None = None
    model_config_sha256: str | None = None
    architecture_revision: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GenerationResult:
    text: str
    token_ids: list[int]
    omitted_turns: int
    technical_details: dict[str, Any]
    cancelled: bool = False


@dataclass
class _LoadedRuntime:
    identity: RuntimeIdentity
    model: SaltyPotato
    tokenizer: SaltyTokenizer


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


class RuntimeManager:
    def __init__(
        self,
        runtime_root: str | Path,
        *,
        device: str = "cuda",
        precision: str = "bf16",
    ) -> None:
        self.runtime_root = Path(runtime_root).resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.precision = precision
        self._lock = threading.RLock()
        self._activation_lock = threading.Lock()
        self._loaded: _LoadedRuntime | None = None
        self._generation_count = 0
        self._internal_warmup_completed = False

    @property
    def identity(self) -> RuntimeIdentity | None:
        with self._lock:
            return self._loaded.identity if self._loaded else None

    def unload(self) -> None:
        with self._lock:
            loaded = self._loaded
            self._loaded = None
            self._generation_count = 0
            self._internal_warmup_completed = False
        if loaded is not None:
            del loaded.model
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def activate(
        self,
        *,
        checkpoint_id: str,
        checkpoint_path: str | Path,
        label: str,
        total_trained_steps: int | None,
        context_limit: int,
        expected_weight_sha256: str,
        expected_tokenizer_sha256: str | None = None,
        expected_architecture_revision: int | None = None,
        expected_model_config_sha256: str | None = None,
        progress: ActivationProgress,
        commit_active: Callable[[RuntimeIdentity], None],
    ) -> RuntimeIdentity:



        with self._activation_lock:
            return self._activate_serialized(
                checkpoint_id=checkpoint_id,
                checkpoint_path=checkpoint_path,
                label=label,
                total_trained_steps=total_trained_steps,
                context_limit=context_limit,
                expected_weight_sha256=expected_weight_sha256,
                expected_tokenizer_sha256=expected_tokenizer_sha256,
                expected_architecture_revision=expected_architecture_revision,
                expected_model_config_sha256=expected_model_config_sha256,
                progress=progress,
                commit_active=commit_active,
            )

    def _activate_serialized(
        self,
        *,
        checkpoint_id: str,
        checkpoint_path: str | Path,
        label: str,
        total_trained_steps: int | None,
        context_limit: int,
        expected_weight_sha256: str,
        expected_tokenizer_sha256: str | None,
        expected_architecture_revision: int | None,
        expected_model_config_sha256: str | None,
        progress: ActivationProgress,
        commit_active: Callable[[RuntimeIdentity], None],
    ) -> RuntimeIdentity:
        checkpoint_path = Path(checkpoint_path).resolve()
        progress.update("Preparing saved version")
        report = inspect_checkpoint(checkpoint_path, calculate_checksum=True)
        if not report.valid:
            raise ValueError("; ".join(report.errors))
        if report.weight_sha256 != expected_weight_sha256:
            raise ValueError("saved version checksum differs from its database identity")
        config_path = checkpoint_path / "config.json"
        model_config_sha256 = (
            hashlib.sha256(config_path.read_bytes()).hexdigest()
            if config_path.is_file()
            else None
        )
        if (
            expected_model_config_sha256 is not None
            and model_config_sha256 != expected_model_config_sha256
        ):
            raise ValueError("saved version model configuration differs from its identity")

        runtime_id = str(uuid.uuid4())
        artifact = self.runtime_root / runtime_id
        artifact.mkdir(parents=True, exist_ok=False)
        committed = False
        resolved = None
        try:
            progress.update("Creating runtime")
            resolved = normalise_device(
                self.device,
                precision=self.precision,
                make_current=True,
            )
            target = resolved.device
            dtype = (
                torch.bfloat16
                if self.precision == "bf16"
                else torch.float16
                if self.precision == "fp16"
                else torch.float32
            )
            model, tokenizer = load_checkpoint(
                checkpoint_path,
                device=target,
                dtype=dtype,
                context_limit=context_limit,
            )
            identity = RuntimeIdentity(
                runtime_id=runtime_id,
                checkpoint_id=checkpoint_id,
                saved_version_label=label,
                model_name="Salty Steak",
                total_trained_steps=(
                    int(total_trained_steps)
                    if total_trained_steps is not None
                    else None
                ),
                checkpoint_path=str(checkpoint_path),
                weight_sha256=expected_weight_sha256,
                context_limit=int(context_limit),
                device=str(target),
                precision=self.precision,
                artifact_path=str(artifact),
                tokenizer_sha256=getattr(tokenizer, "fingerprint", None),
                model_config_sha256=model_config_sha256,
                architecture_revision=getattr(
                    model.config, "architecture_version", None
                ),
            )
            _atomic_json(
                artifact / "runtime.json",
                {
                    "format": "salty-potato-runtime-v2",
                    **identity.to_dict(),
                    "created_at_unix": time.time(),
                },
            )
            progress.update("Verifying runtime")
            if model.config.max_position_embeddings != context_limit:
                raise RuntimeError("runtime context differs from saved-version capability")
            if (
                expected_tokenizer_sha256 is not None
                and identity.tokenizer_sha256 != expected_tokenizer_sha256
            ):
                raise RuntimeError("runtime tokenizer identity differs from the saved version")
            if (
                expected_architecture_revision is not None
                and identity.architecture_revision != int(expected_architecture_revision)
            ):
                raise RuntimeError("runtime architecture identity differs from the saved version")
            progress.update("Loading Salty Steak")
            model.eval()
            progress.update("Testing real generation")
            prompt = tokenizer.build_generation_prompt(
                [{"role": "user", "content": "Reply with one word."}],
                context_limit=context_limit,
                reserved_output_tokens=1,
            )
            input_ids = torch.tensor(
                [prompt.token_ids], dtype=torch.long, device=target
            )
            warmup_timing: dict[str, Any] = {}
            output = model.generate(
                input_ids,
                max_new_tokens=8,
                eos_token_id=None,
                temperature=0,
                timing=warmup_timing,
            )
            if output.shape[1] != input_ids.shape[1] + 8:
                raise RuntimeError("runtime generation warm-up did not exercise decode")

            progress.update("Switching chat version")
            replacement = _LoadedRuntime(identity, model, tokenizer)



            with self._lock:
                previous = self._loaded
                self._loaded = replacement
                self._generation_count = 0
                self._internal_warmup_completed = True
                try:
                    commit_active(identity)
                except BaseException:
                    self._loaded = previous
                    self._generation_count = 0
                    self._internal_warmup_completed = bool(previous)
                    raise
                committed = True
            if previous:
                del previous.model
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()


            try:
                for candidate in self.runtime_root.iterdir():
                    if candidate != artifact and candidate.is_dir():
                        shutil.rmtree(candidate, ignore_errors=True)
            except OSError:


                pass
            return identity
        except Exception:
            if not committed:
                shutil.rmtree(artifact, ignore_errors=True)
                if resolved is not None:
                    release_device(resolved)
            raise

    def generate(
        self,
        *,
        active_checkpoint_id: str,
        messages: list[dict[str, str]],
        reserved_output_tokens: int,
        maximum_output_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        repetition_penalty: float,
        seed: int | None,
        should_stop: Callable[[], bool] | None = None,
        context_window_tokens: int | None = None,
        stop_sequences: list[str] | None = None,
    ) -> GenerationResult:
        with self._lock:
            loaded = self._loaded
            if loaded is None:
                raise RuntimeError("Salty Steak is not loaded")
            if loaded.identity.checkpoint_id != active_checkpoint_id:
                raise RuntimeError(
                    "active saved version and runtime-bound checkpoint differ"
                )
            identity = loaded.identity
            model = loaded.model
            tokenizer = loaded.tokenizer
            cold_warm = (
                "warm"
                if self._generation_count
                else "warm_after_internal_load"
                if self._internal_warmup_completed
                else "cold"
            )
            self._generation_count += 1
        tokenisation_started = time.perf_counter()
        effective_context_limit = min(
            identity.context_limit,
            int(context_window_tokens or identity.context_limit),
        )
        if effective_context_limit < 128:
            raise ValueError("effective context window must be at least 128 tokens")
        prompt: PromptBudget = tokenizer.build_generation_prompt(
            messages,
            context_limit=effective_context_limit,
            reserved_output_tokens=reserved_output_tokens,
        )
        tokenisation_duration = time.perf_counter() - tokenisation_started
        available_output = min(
            int(maximum_output_tokens),
            effective_context_limit - prompt.context_tokens,
        )
        if available_output < 1:
            raise ValueError("no output tokens remain in the context budget")
        device = next(model.parameters()).device
        input_ids = torch.tensor(
            [prompt.token_ids], dtype=torch.long, device=device
        )
        started = time.perf_counter()
        timing: dict[str, Any] = {}
        output = model.generate(
            input_ids,
            max_new_tokens=available_output,
            eos_token_id=tokenizer.eos_id,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            repetition_penalty=repetition_penalty,
            seed=seed,
            should_stop=should_stop,
            timing=timing,
        )
        generated = (
            output[0, input_ids.shape[1] :].detach().cpu().tolist()
        )
        eos_emitted = bool(generated and generated[-1] == tokenizer.eos_id)
        if eos_emitted:
            generated = generated[:-1]
        duration = time.perf_counter() - started
        input_context_tokens = prompt.context_tokens
        generated_output_tokens = len(generated)
        text = tokenizer.decode(generated).strip()
        matched_stop = None
        matched_offset = None
        for stop in stop_sequences or []:
            offset = text.find(stop)
            if offset >= 0 and (matched_offset is None or offset < matched_offset):
                matched_stop = stop
                matched_offset = offset
        if matched_offset is not None:
            text = text[:matched_offset].rstrip()
        cancelled = bool(
            should_stop is not None and should_stop() and not eos_emitted
        )
        return GenerationResult(
            text=text,
            token_ids=generated,
            omitted_turns=prompt.omitted_turns,
            cancelled=cancelled,
            technical_details={
                "model_name": identity.model_name,
                "saved_version_label": identity.saved_version_label,
                "checkpoint_id": identity.checkpoint_id,
                "total_trained_steps": identity.total_trained_steps,
                "runtime_id": identity.runtime_id,
                "input_context_tokens": input_context_tokens,
                "generated_output_tokens": generated_output_tokens,
                "total_processed_tokens": (
                    input_context_tokens + generated_output_tokens
                ),
                "architectural_context_limit": identity.context_limit,
                "effective_context_limit": effective_context_limit,
                "reserved_output_tokens": int(reserved_output_tokens),
                "omitted_input_tokens": prompt.omitted_tokens,
                "finish_reason": (
                    "stopped"
                    if cancelled
                    else "stop_sequence"
                    if matched_stop is not None
                    else "eos"
                    if eos_emitted
                    else "maximum_output"
                ),
                "matched_stop_sequence": matched_stop,
                "generation_duration_seconds": round(duration, 4),
                "generation_duration_ms": round(duration * 1000, 2),
                "cold_warm_classification": cold_warm,
                "queue_time_seconds": None,
                "model_readiness_wait_seconds": None,
                "tokenisation_duration_seconds": round(tokenisation_duration, 6),
                "prefill_duration_seconds": _rounded_optional(
                    timing.get("prefill_duration_seconds")
                ),
                "first_token_latency_seconds": _rounded_optional(
                    timing.get("first_token_latency_seconds")
                ),
                "decode_duration_seconds": _rounded_optional(
                    timing.get("decode_duration_seconds")
                ),
                "decode_tokens_per_second": _rounded_optional(
                    timing.get("decode_tokens_per_second")
                ),
                "attention_backend": timing.get("attention_backend"),
                "compile_mode": timing.get("compile_mode"),
                "kv_cache_state": (
                    "enabled"
                    if timing.get("kv_cache_enabled")
                    else "not_reported"
                ),
                "kv_cache_layers": timing.get("kv_cache_layers"),
                "kv_cache_device": timing.get("kv_cache_device"),
                "kv_cache_tokens": timing.get("kv_cache_tokens"),
	                "execution_device": timing.get("device", str(device)),
	                "execution_dtype": timing.get("dtype"),
	                "precision": identity.precision,
	                "device": identity.device,
                "cuda_synchronisation": timing.get("cuda_synchronisation"),
                "streaming": False,
                "frontend_update_count": None,
                "tokens_per_second": (
                    round(generated_output_tokens / duration, 2)
                    if duration > 0
                    else None
                ),
            },
        )


def _rounded_optional(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None
