"""Checkpoint integrity, isolated loading, and atomic saving."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import torch
from safetensors import safe_open
from safetensors.torch import load_file, save_file

from ..system.device import (
    cuda_supports_bf16,
    normalise_device,
    precision_for_dtype,
    release_device,
)
from ..system.files import atomic_write_json, sha256_file
from ..system.timestamps import utc_now_timestamp
from ..worker_bootstrap import package_build_id, worker_entry_path
from .model import ModelConfig, SaltyPotato
from .tokenizer import SaltyTokenizer

PRODUCTION_PARAMETER_COUNT = 95_177_472
PRODUCTION_TOKENIZER_FINGERPRINT = (
    "ab55983f4cda8c8dd207a00eff5befa16956b7616920dc12697f2d88dbff9e13"
)
PRODUCTION_TOKENIZER_MODEL_SHA256 = (
    "95452e12fab64f5d2e2f64e9c286a9af2fcc4abed5fb5e4c682aafa17aed88bc"
)
SAVED_VERSION_FORMAT = "salty-potato-saved-version-v2"
_BOOTSTRAP_IDENTITY_CACHE: dict[tuple[str, str, str, str], dict[str, Any]] = {}


def _windows_hidden_process_options() -> dict[str, Any]:
    """Keep worker consoles hidden while retaining captured stdout and stderr."""

    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _private_worker_interpreter(project: Path) -> tuple[Path, Path]:
    """Return only the project's private interpreter, never the desktop host."""

    virtual_environment = (project / ".venv").resolve()
    interpreter = (
        virtual_environment / "Scripts" / "python.exe"
        if os.name == "nt"
        else virtual_environment / "bin" / "python"
    ).resolve()
    configuration = virtual_environment / "pyvenv.cfg"
    if not configuration.is_file() or not interpreter.is_file():
        raise FileNotFoundError(
            f"private Python runtime is incomplete: {interpreter}"
        )
    if virtual_environment not in interpreter.parents:
        raise ValueError("verification interpreter is outside the private runtime")
    if interpreter.name.casefold() not in {"python", "python.exe"}:
        raise ValueError("verification interpreter is not Python")
    return virtual_environment, interpreter


def _validate_private_worker_interpreter(
    interpreter: Path,
    virtual_environment: Path,
    project: Path,
    environment: dict[str, str],
    *,
    timeout_seconds: float = 10.0,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Prove the selected executable is the private runtime before worker launch."""

    build_id = package_build_id(project)
    worker_entry = worker_entry_path(project).resolve()
    worker_hash = sha256_file(worker_entry)
    configuration = virtual_environment / "pyvenv.cfg"
    environment_marker = hashlib.sha256(
        configuration.read_bytes()
        + str(virtual_environment).encode("utf-8")
    ).hexdigest()
    cache_key = (
        str(interpreter.resolve()),
        build_id,
        worker_hash,
        environment_marker,
    )
    cached = _BOOTSTRAP_IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    command = [
        str(interpreter),
        "-I",
        "-B",
        str((project / "app" / "backend" / "worker_bootstrap.py").resolve()),
        "--expected-package-root",
        str(project.resolve()),
        "--expected-build-id",
        build_id,
    ]
    completed = runner(
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        **_windows_hidden_process_options(),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "private Python runtime could not run the lightweight worker bootstrap: "
            + (completed.stderr or completed.stdout or "unknown interpreter failure")
        )
    try:
        identity = json.loads(completed.stdout)
        executable = Path(identity["executable"]).resolve()
        prefix = Path(identity["prefix"]).resolve()
        package_root = Path(identity["package_root"]).resolve()
        returned_entry = Path(identity["worker_entry"]).resolve()
    except (json.JSONDecodeError, KeyError, TypeError, OSError) as error:
        raise RuntimeError("private Python runtime returned an invalid identity probe") from error
    if (
        identity.get("success") is not True
        or executable != interpreter
        or prefix != virtual_environment
        or package_root != project.resolve()
        or returned_entry != worker_entry
        or identity.get("worker_sha256") != worker_hash
        or identity.get("package_build_id") != build_id
        or not isinstance(identity.get("environment_fingerprint"), str)
    ):
        raise RuntimeError(
            "verification worker bootstrap identity did not match the private package"
        )
    if executable.name.casefold() not in {"python", "python.exe"}:
        raise RuntimeError("desktop host cannot be selected as the worker interpreter")
    _BOOTSTRAP_IDENTITY_CACHE.clear()
    _BOOTSTRAP_IDENTITY_CACHE[cache_key] = dict(identity)
    return identity


def _is_iso_utc_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.endswith("Z")
    except ValueError:
        return False


class IsolatedWorkerError(RuntimeError):
    """An isolated verification worker did not return a usable result."""

    def __init__(
        self,
        message: str,
        *,
        return_code: int,
        stdout: str,
        stderr: str,
        result_error: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.return_code = int(return_code)
        self.stdout = stdout
        self.stderr = stderr
        self.result_error = result_error
        self.diagnostics = diagnostics or {}

    def technical_details(self) -> str:
        return json.dumps(
            {
                "return_code": self.return_code,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "result_error": self.result_error,
                **self.diagnostics,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _production_config_errors(config: ModelConfig) -> list[str]:
    errors: list[str] = []
    pinned = {
        "model_type": "salty_potato",
        "vocab_size": 16_384,
        "hidden_size": 768,
        "intermediate_size": 2_304,
        "num_hidden_layers": 12,
        "num_attention_heads": 12,
        "num_key_value_heads": 4,
        "rope_theta": 10_000.0,
        "rms_norm_epsilon": 1e-5,
        "dropout": 0.0,
        "tie_word_embeddings": True,
    }
    for name, required in pinned.items():
        if getattr(config, name) != required:
            errors.append(f"production architecture mismatch for {name}")
    if config.architecture_version != 2:
        errors.append("production saved versions must use architecture revision 2")
    if config.max_position_embeddings != 8_192:
        errors.append("production saved versions must declare 8,192 tokens")
    if config.expected_parameter_count() != PRODUCTION_PARAMETER_COUNT:
        errors.append("production parameter count is incompatible")
    return errors


@dataclass
class CheckpointReport:
    path: str
    valid: bool
    errors: list[str]
    warnings: list[str]
    step: int | None
    parameter_count: int
    tensor_count: int
    weight_bytes: int
    weight_sha256: str | None
    tokenizer_fingerprint: str | None
    short_smoke_digest: str | None = None
    context_test_tokens: int | None = None
    peak_vram_bytes: int | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _expected_layout(config: ModelConfig) -> dict[str, tuple[int, ...]]:
    with torch.device("meta"):
        model = SaltyPotato(config, initialize=False)
    return {
        key: tuple(tensor.shape)
        for key, tensor in model.checkpoint_state().items()
    }


def inspect_checkpoint(
    checkpoint: str | Path,
    *,
    calculate_checksum: bool = True,
    require_production_architecture: bool = True,
) -> CheckpointReport:
    root = Path(checkpoint).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    weights = root / "model.safetensors"
    config_file = root / "config.json"
    manifest_file = root / "manifest.json"
    tokenizer_dir = root / "tokenizer"
    manifest: dict[str, Any] = {}
    step = None
    count = 0
    parameters = 0
    weight_bytes = weights.stat().st_size if weights.is_file() else 0
    weight_hash = None
    tokenizer_fingerprint = None

    for required in (weights, config_file, manifest_file):
        if not required.is_file():
            errors.append(f"missing required file: {required.name}")
    if not tokenizer_dir.is_dir():
        errors.append("missing checkpoint-local tokenizer")
    if errors:
        return CheckpointReport(
            str(root), False, errors, warnings, None, 0, 0, weight_bytes, None, None
        )

    try:
        config = ModelConfig.from_file(config_file)
        expected = _expected_layout(config)
        if require_production_architecture:
            errors.extend(_production_config_errors(config))
    except Exception as error:
        errors.append(f"invalid model configuration: {error}")
        expected = {}
        config = None
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        checkpoint_format = manifest.get("format")
        if checkpoint_format != SAVED_VERSION_FORMAT:
            errors.append("unsupported checkpoint manifest format")
        if manifest.get("model_type") != "salty_potato":
            errors.append("manifest model identity is incompatible")
        raw_step = manifest.get("total_steps")
        trustworthy = isinstance(raw_step, int) and raw_step >= 0
        if trustworthy:
            step = int(raw_step)
        else:
            warnings.append("training step is not independently trustworthy")
        manifest_parameters = manifest.get("parameter_count")
        if (
            manifest_parameters is not None
            and int(manifest_parameters) != PRODUCTION_PARAMETER_COUNT
            and require_production_architecture
        ):
            errors.append("manifest parameter count is incompatible")
    except Exception as error:
        errors.append(f"unreadable manifest: {error}")
    if not manifest.get("weights_sha256"):
        errors.append("manifest does not bind the weight checksum")
    if (
        config is not None
        and manifest.get("context_tokens") != config.max_position_embeddings
    ):
        errors.append("manifest context capability differs from model configuration")

    if config is not None:
        try:
            with safe_open(str(weights), framework="pt", device="cpu") as handle:
                keys = set(handle.keys())
                count = len(keys)
                missing = sorted(set(expected) - keys)
                unexpected = sorted(keys - set(expected))
                if missing:
                    errors.append(
                        "missing tensors: " + ", ".join(missing[:8])
                    )
                if unexpected:
                    errors.append(
                        "unexpected tensors: " + ", ".join(unexpected[:8])
                    )
                for key in sorted(keys & set(expected)):
                    tensor = handle.get_slice(key)
                    shape = tuple(tensor.get_shape())
                    if shape != expected[key]:
                        errors.append(
                            f"tensor shape mismatch for {key}: {shape} != {expected[key]}"
                        )
                        continue
                    dtype = str(tensor.get_dtype())
                    if dtype not in {"F32", "F16", "BF16"}:
                        errors.append(f"unsupported tensor dtype for {key}: {dtype}")
                    parameters += int(torch.tensor(shape).prod().item())




                    values = handle.get_tensor(key)
                    try:
                        if not bool(torch.isfinite(values).all().item()):
                            errors.append(f"non-finite values in tensor: {key}")
                    finally:
                        del values
        except Exception as error:
            errors.append(f"unreadable safetensors file: {error}")
        if parameters and parameters != config.expected_parameter_count():
            errors.append(
                "tensor parameter count does not match the Salty Steak architecture"
            )
        if require_production_architecture and parameters != PRODUCTION_PARAMETER_COUNT:
            errors.append("checkpoint is not the production Salty Steak architecture")

    try:
        tokenizer = SaltyTokenizer(tokenizer_dir)
        tokenizer_fingerprint = tokenizer.fingerprint
        if config is not None and tokenizer.vocab_size != config.vocab_size:
            errors.append("checkpoint tokenizer vocabulary is incompatible")
        expected_tokenizer = manifest.get("tokenizer_fingerprint")
        if expected_tokenizer and tokenizer_fingerprint != expected_tokenizer:
            errors.append("checkpoint tokenizer fingerprint does not match manifest")
        if not expected_tokenizer:
            errors.append("manifest does not bind the checkpoint tokenizer")
        if require_production_architecture:
            if tokenizer_fingerprint != PRODUCTION_TOKENIZER_FINGERPRINT:
                errors.append("tokenizer is not the production Salty Steak tokenizer")
            if (
                tokenizer.metadata.get("model_sha256")
                != PRODUCTION_TOKENIZER_MODEL_SHA256
            ):
                errors.append("production tokenizer model checksum is incompatible")
    except Exception as error:
        errors.append(f"invalid tokenizer: {error}")

    if calculate_checksum:
        try:
            weight_hash = sha256_file(weights)
            recorded = manifest.get("weights_sha256")
            if recorded and weight_hash != recorded:
                errors.append("weight checksum does not match manifest")
        except Exception as error:
            errors.append(f"weight checksum failed: {error}")
    return CheckpointReport(
        path=str(root),
        valid=not errors,
        errors=errors,
        warnings=warnings,
        step=step,
        parameter_count=parameters,
        tensor_count=count,
        weight_bytes=weight_bytes,
        weight_sha256=weight_hash,
        tokenizer_fingerprint=tokenizer_fingerprint,
    )


def load_checkpoint(
    checkpoint: str | Path,
    *,
    device: str | torch.device = "cpu",
    dtype: torch.dtype | None = None,
    context_limit: int | None = None,
    require_production_architecture: bool = True,
) -> tuple[SaltyPotato, SaltyTokenizer]:
    root = Path(checkpoint).resolve()
    config = ModelConfig.from_file(root / "config.json")
    if require_production_architecture:
        errors = _production_config_errors(config)
        if errors:
            raise ValueError("; ".join(errors))
    if context_limit is not None and context_limit != config.max_position_embeddings:
        raise ValueError("runtime context must match the saved-version capability")
    model = SaltyPotato(config, initialize=False)
    state = load_file(str(root / "model.safetensors"), device="cpu")
    missing, unexpected = model.load_state_dict(state, strict=False)
    allowed_missing = {"lm_head.weight"} if config.tie_word_embeddings else set()
    if set(missing) != allowed_missing or unexpected:
        raise ValueError(
            f"checkpoint keys are incompatible; missing={missing}, unexpected={unexpected}"
        )
    del state
    resolved = normalise_device(
        device,
        precision=precision_for_dtype(dtype),
        make_current=True,
    )
    target = resolved.device
    if dtype is None:
        dtype = (
            torch.bfloat16
            if resolved.cuda_index is not None
            and cuda_supports_bf16(resolved.cuda_index)
            else torch.float16
            if target.type == "cuda"
            else torch.float32
        )
    model = model.to(device=target, dtype=dtype)
    model.eval()
    tokenizer = SaltyTokenizer(root / "tokenizer")
    return model, tokenizer


def run_smoke_test(
    checkpoint: str | Path,
    *,
    context_limit: int,
    context_test_tokens: int = 0,
    device: str = "cpu",
    verification_attempt_id: str | None = None,
    operation_id: str | None = None,
    checkpoint_id: str | None = None,
    package_build_id_value: str | None = None,
    verify_integrity: bool = True,
) -> dict[str, Any]:
    worker_started_at = utc_now_timestamp()
    started = time.perf_counter()
    if verify_integrity:
        integrity = inspect_checkpoint(checkpoint, calculate_checksum=True)
        if not integrity.valid:
            raise ValueError("; ".join(integrity.errors))
        parameter_count = integrity.parameter_count
    else:
        configuration = ModelConfig.from_file(
            Path(checkpoint).resolve() / "config.json"
        )
        parameter_count = configuration.expected_parameter_count()
    resolved = normalise_device(
        device,
        make_current=True,
        reset_peak_memory=True,
    )
    target = resolved.device
    if resolved.is_cuda:
        torch.cuda.empty_cache()
    try:
        model, tokenizer = load_checkpoint(
            checkpoint, device=target, context_limit=context_limit
        )
        prompt = tokenizer.encode("Salty Steak", add_bos=True)
        if context_test_tokens:
            if context_test_tokens >= context_limit:
                raise ValueError("context smoke must leave room for an output token")
            seed_tokens = tokenizer.encode(
                "The quick brown potato rests near the quiet shore. "
            )
            repeated = (
                seed_tokens
                * ((context_test_tokens - 1 + len(seed_tokens) - 1) // len(seed_tokens))
            )[: context_test_tokens - 1]
            prompt = [tokenizer.bos_id, *repeated]
        input_ids = torch.tensor([prompt], dtype=torch.long, device=target)
        with torch.inference_mode():
            output = model.generate(
                input_ids,
                max_new_tokens=1,
                eos_token_id=tokenizer.eos_id,
                temperature=0,
                prefill_chunk_size=128,
            )
        generated = output[0, input_ids.shape[1] :].detach().cpu().tolist()
        if not generated:
            raise RuntimeError("deterministic generation returned no token")
        digest = hashlib.sha256(
            json.dumps(generated, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        peak_vram = (
            int(torch.cuda.max_memory_allocated(resolved.cuda_index))
            if resolved.cuda_index is not None
            else None
        )
        checkpoint_path = str(Path(checkpoint).resolve())
        return {
            "success": True,
            "verification_attempt_id": verification_attempt_id,
            "operation_id": operation_id,
            "checkpoint_id": checkpoint_id or Path(checkpoint_path).name,
            "checkpoint_path": checkpoint_path,
            "manifest_valid": True,
            "weights_exist": (Path(checkpoint_path) / "model.safetensors").is_file(),
            "checksums_valid": True,
            "load_valid": True,
            "parameter_count": parameter_count,
            "context_limit": context_limit,
            "worker_pid": os.getpid(),
            "package_build_id": package_build_id_value
            or package_build_id(Path(__file__).resolve().parents[3]),
            "started_at": worker_started_at,
            "finished_at": utc_now_timestamp(),
            "error": None,
            "context_test_tokens": len(prompt),
            "generated_token_ids": generated,
            "digest": digest,
            "finite": True,
            "device": str(target),
            "peak_vram_bytes": peak_vram,
            "duration_seconds": round(time.perf_counter() - started, 4),
        }
    finally:
        release_device(resolved)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a disposable worker and wait until it cannot commit later."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **_windows_hidden_process_options(),
        )
    else:
        process.kill()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)


def _run_disposable_worker(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    process_started: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    options = _windows_hidden_process_options()
    if os.name != "nt":
        options["start_new_session"] = True
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **options,
    )
    if process_started is not None:
        process_started(process.pid)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        error.stdout = stdout
        error.stderr = stderr
        setattr(error, "worker_pid", process.pid)
        raise
    return subprocess.CompletedProcess(
        command, process.returncode, stdout or "", stderr or ""
    )


def isolated_smoke_test(
    checkpoint: str | Path,
    *,
    context_limit: int,
    context_test_tokens: int = 0,
    device: str = "cpu",
    timeout_seconds: float = 900,
    bootstrap_timeout_seconds: float = 10,
    worker_start_timeout_seconds: float = 30,
    integrity_timeout_seconds: float = 180,
    model_load_timeout_seconds: float | None = None,
    verification_attempt_id: str | None = None,
    operation_id: str | None = None,
    checkpoint_id: str | None = None,
    package_build_id_value: str | None = None,
    result_path: str | Path | None = None,
    process_started: Callable[[int], None] | None = None,
    phase_changed: Callable[[str], None] | None = None,
    attempt_is_valid: Callable[[], bool] | None = None,
    bootstrap_runner: Any = subprocess.run,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    project = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTHONHOME", None)
    build_id = package_build_id_value or package_build_id(project)
    expected_checkpoint_id = checkpoint_id or Path(checkpoint).resolve().name
    try:
        virtual_environment, interpreter = _private_worker_interpreter(project)
    except (FileNotFoundError, ValueError) as error:
        raise IsolatedWorkerError(
            "The checkpoint verification worker could not launch because the private Python runtime is missing.",
            return_code=-1,
            stdout="",
            stderr="",
            result_error=str(error),
        ) from error
    environment["VIRTUAL_ENV"] = str(virtual_environment)
    if phase_changed is not None:
        phase_changed("interpreter_bootstrap")
    try:
        _validate_private_worker_interpreter(
            interpreter,
            virtual_environment,
            project,
            environment,
            timeout_seconds=bootstrap_timeout_seconds,
            runner=bootstrap_runner,
        )
    except subprocess.TimeoutExpired as error:
        raise IsolatedWorkerError(
            "The checkpoint verification interpreter bootstrap timed out.",
            return_code=-1,
            stdout=error.stdout if isinstance(error.stdout, str) else "",
            stderr=error.stderr if isinstance(error.stderr, str) else "",
            result_error=f"timeout after {bootstrap_timeout_seconds} seconds",
            diagnostics={
                "timed_out_phase": "interpreter_bootstrap",
                    "command": [
                        str(interpreter),
                        "-I",
                        str(
                            (
                                project
                                / "app"
                                / "backend"
                                / "worker_bootstrap.py"
                            ).resolve()
                        ),
                    ],
                "deadline_seconds": bootstrap_timeout_seconds,
            },
        ) from error
    except (OSError, RuntimeError) as error:
        raise IsolatedWorkerError(
            "The checkpoint verification worker could not validate its private runtime.",
            return_code=-1,
            stdout="",
            stderr="",
            result_error=f"{type(error).__name__}: {error}",
        ) from error
    with contextlib.ExitStack() as stack:
        if result_path is None:
            temporary = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="salty-smoke-result-")
            )
            committed_result = Path(temporary) / "result.json"
        else:
            committed_result = Path(result_path).resolve()
            committed_result.parent.mkdir(parents=True, exist_ok=True)
            committed_result.unlink(missing_ok=True)
        identity_arguments = [
            "--verification-attempt-id",
            verification_attempt_id or "standalone",
            "--operation-id",
            operation_id or "standalone",
            "--checkpoint-id",
            expected_checkpoint_id,
            "--package-build-id",
            build_id,
        ]
        integrity_result = committed_result.with_name(
            f"{committed_result.stem}.integrity{committed_result.suffix}"
        )
        integrity_command = [
            str(interpreter),
            "-I",
            "-B",
            str((project / "app" / "backend" / "worker_launcher.py").resolve()),
            "app.backend.versions.checkpoint",
            "verify-integrity",
            "--checkpoint",
            str(Path(checkpoint).resolve()),
            "--result-file",
            str(integrity_result),
            *identity_arguments,
        ]
        command = [
            str(interpreter),
            "-I",
            "-B",
            str((project / "app" / "backend" / "worker_launcher.py").resolve()),
            "app.backend.versions.checkpoint",
            "smoke",
            "--checkpoint",
            str(Path(checkpoint).resolve()),
            "--context-limit",
            str(context_limit),
            "--context-test-tokens",
            str(context_test_tokens),
            "--device",
            device,
            "--result-file",
            str(committed_result),
            "--skip-integrity",
            *identity_arguments,
        ]
        if phase_changed is not None:
            phase_changed("worker_startup")
        launched_at = utc_now_timestamp()
        load_deadline = (
            float(model_load_timeout_seconds)
            if model_load_timeout_seconds is not None
            else float(timeout_seconds)
        )

        def launch(
            phase_command: list[str],
            phase_timeout: float,
        ) -> subprocess.CompletedProcess[str]:
            if runner is subprocess.run:
                return _run_disposable_worker(
                    phase_command,
                    cwd=project,
                    environment=environment,
                    timeout_seconds=phase_timeout,
                    process_started=process_started,
                )
            return runner(
                phase_command,
                cwd=project,
                env=environment,
                capture_output=True,
                text=True,
                timeout=phase_timeout,
                check=False,
                **_windows_hidden_process_options(),
            )

        try:
            if phase_changed is not None:
                phase_changed("checkpoint_integrity")
            integrity_process = launch(
                integrity_command,
                min(float(integrity_timeout_seconds), float(timeout_seconds)),
            )
        except subprocess.TimeoutExpired as error:
            raise IsolatedWorkerError(
                "Checkpoint integrity verification timed out.",
                return_code=-1,
                stdout=error.stdout if isinstance(error.stdout, str) else "",
                stderr=error.stderr if isinstance(error.stderr, str) else "",
                result_error=f"timeout after {timeout_seconds} seconds",
                diagnostics={
                    "timed_out_phase": "checkpoint_integrity",
                    "command": integrity_command,
                    "worker_pid": getattr(error, "worker_pid", None),
                    "started_at": launched_at,
                    "deadline_seconds": integrity_timeout_seconds,
                    "process_tree_terminated": runner is subprocess.run,
                },
            ) from error
        integrity_stdout = integrity_process.stdout or ""
        integrity_stderr = integrity_process.stderr or ""
        try:
            integrity_payload = json.loads(
                integrity_result.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise IsolatedWorkerError(
                "Checkpoint integrity verification produced no valid result.",
                return_code=integrity_process.returncode,
                stdout=integrity_stdout,
                stderr=integrity_stderr,
                result_error=f"{type(error).__name__}: {error}",
            ) from error
        expected_integrity = {
            "success": True,
            "verification_attempt_id": verification_attempt_id or "standalone",
            "operation_id": operation_id or "standalone",
            "checkpoint_id": expected_checkpoint_id,
            "checkpoint_path": str(Path(checkpoint).resolve()),
            "package_build_id": build_id,
            "manifest_valid": True,
            "weights_exist": True,
            "checksums_valid": True,
        }
        if (
            integrity_process.returncode != 0
            or any(
                integrity_payload.get(key) != value
                for key, value in expected_integrity.items()
            )
        ):
            raise IsolatedWorkerError(
                "Checkpoint integrity verification failed its result contract.",
                return_code=integrity_process.returncode,
                stdout=integrity_stdout,
                stderr=integrity_stderr,
                result_error=json.dumps(integrity_payload, sort_keys=True),
            )
        try:
            if phase_changed is not None:
                phase_changed("isolated_model_loading")
            result = launch(command, load_deadline)
        except subprocess.TimeoutExpired as error:
            raise IsolatedWorkerError(
                "Isolated model loading timed out.",
                return_code=-1,
                stdout=error.stdout if isinstance(error.stdout, str) else "",
                stderr=error.stderr if isinstance(error.stderr, str) else "",
                result_error=f"timeout after {load_deadline} seconds",
                diagnostics={
                    "timed_out_phase": "isolated_model_loading",
                    "command": command,
                    "worker_pid": getattr(error, "worker_pid", None),
                    "started_at": launched_at,
                    "deadline_seconds": load_deadline,
                    "process_tree_terminated": runner is subprocess.run,
                },
            ) from error
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if attempt_is_valid is not None and not attempt_is_valid():
            raise IsolatedWorkerError(
                "The checkpoint verification attempt was invalidated before commit.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error="invalidated attempt",
            )
        if not committed_result.is_file():
            detail = (
                "No result file was committed. Worker stdout was not a result channel."
                if stdout.strip()
                else "No result file was committed and worker stdout was empty."
            )
            raise IsolatedWorkerError(
                "The checkpoint verification worker produced no result.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error=detail,
            )
        try:
            raw_result = committed_result.read_text(encoding="utf-8")
            if not raw_result.strip():
                raise ValueError("the committed result file was empty")
            payload = json.loads(raw_result)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise IsolatedWorkerError(
                "The checkpoint verification worker produced an invalid result.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error=f"{type(error).__name__}: {error}",
            ) from error
        success_fields = {
            "success": bool, "verification_attempt_id": str,
            "operation_id": str, "checkpoint_id": str, "checkpoint_path": str,
            "manifest_valid": bool, "weights_exist": bool,
            "checksums_valid": bool, "load_valid": bool,
            "parameter_count": int, "context_limit": int, "worker_pid": int,
            "package_build_id": str, "started_at": str, "finished_at": str,
        }
        failure_fields = {
            "success": bool, "verification_attempt_id": str,
            "operation_id": str, "checkpoint_id": str, "checkpoint_path": str,
            "failed_phase": str,
            "error_type": str, "error_message": str,
            "technical_traceback": str, "worker_pid": int,
            "package_build_id": str, "started_at": str, "finished_at": str,
        }
        if not isinstance(payload, dict):
            invalid = True
            required = success_fields
        else:
            required = success_fields if payload.get("success") is True else failure_fields
            invalid = not all(isinstance(payload.get(name), expected) for name, expected in required.items())
            invalid = invalid or not all(
                _is_iso_utc_timestamp(payload[name])
                for name in ("started_at", "finished_at")
                if isinstance(payload.get(name), str)
            )
        if invalid:
            raise IsolatedWorkerError(
                "The checkpoint verification worker returned incomplete fields.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error=f"required fields: {sorted(required)}",
            )
        expected_path = str(Path(checkpoint).resolve())
        identity_errors = []
        if payload["checkpoint_path"] != expected_path:
            identity_errors.append(
                f"checkpoint path expected {expected_path}, got {payload['checkpoint_path']}"
            )
        if payload.get("verification_attempt_id") != (verification_attempt_id or "standalone"):
            identity_errors.append("verification attempt ID mismatch")
        if payload.get("operation_id") != (operation_id or "standalone"):
            identity_errors.append("operation ID mismatch")
        if payload.get("checkpoint_id") != expected_checkpoint_id:
            identity_errors.append("checkpoint ID mismatch")
        if payload.get("package_build_id") != build_id:
            identity_errors.append("package build ID mismatch")
        if identity_errors:
            raise IsolatedWorkerError(
                "The checkpoint verification worker returned the wrong identity.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error="; ".join(identity_errors),
            )
        if bool(payload["success"]) != (result.returncode == 0):
            raise IsolatedWorkerError(
                "The checkpoint verification worker result disagreed with its exit code.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error=f"success={payload['success']}, return_code={result.returncode}",
            )
        if result.returncode:
            raise IsolatedWorkerError(
                "The checkpoint verification worker reported verification failure.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error=payload.get("error_message"),
            )
        if (
            payload.get("error") is not None
            or not all(payload[name] is True for name in ("manifest_valid", "weights_exist", "checksums_valid", "load_valid"))
        ):
            raise IsolatedWorkerError(
                "The checkpoint verification worker returned an unsuccessful verification result.",
                return_code=result.returncode,
                stdout=stdout,
                stderr=stderr,
                result_error=json.dumps(payload, sort_keys=True),
            )
        return payload


def save_completed_checkpoint(
    destination: str | Path,
    model: SaltyPotato,
    tokenizer_source: str | Path,
    *,
    total_steps: int | None,
    additional_steps: int,
    dataset_id: str | None,
    operation_id: str | None = None,
    prepared_dataset_id: str | None = None,
    starting_version_id: str | None = None,
) -> dict[str, Any]:
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        model.config.to_file(temporary / "config.json")
        shutil.copytree(Path(tokenizer_source), temporary / "tokenizer")
        weights = temporary / "model.safetensors"
        state = {
            key: tensor.detach().cpu().contiguous()
            for key, tensor in model.checkpoint_state().items()
        }
        nonfinite = [
            key
            for key, tensor in state.items()
            if not bool(torch.isfinite(tensor).all().item())
        ]
        if nonfinite:
            raise ValueError(
                "cannot save non-finite checkpoint tensors: "
                + ", ".join(nonfinite[:4])
            )
        save_file(state, str(weights))
        del state
        manifest = {
            "format": "salty-potato-saved-version-v2",
            "model_type": "salty_potato",
            "display_name": "Salty Steak",
            "total_steps": int(total_steps) if total_steps is not None else None,
            "additional_steps": int(additional_steps),
            "dataset_id": dataset_id,
            "operation_id": operation_id,
            "prepared_dataset_id": prepared_dataset_id,
            "starting_version_id": starting_version_id,
            "parameter_count": model.config.expected_parameter_count(),
            "context_tokens": model.config.max_position_embeddings,
            "weights_sha256": sha256_file(weights),
            "tokenizer_fingerprint": SaltyTokenizer(
                temporary / "tokenizer"
            ).fingerprint,
            "saved_at": utc_now_timestamp(),
        }
        for completed_file in temporary.rglob("*"):
            if completed_file.is_file():
                with completed_file.open("r+b") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
        atomic_write_json(temporary / "manifest.json", manifest)
        if destination.exists():
            raise FileExistsError(destination)
        temporary.replace(destination)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--checkpoint", required=True)
    inspect_parser.add_argument("--skip-checksum", action="store_true")
    integrity_parser = subparsers.add_parser("verify-integrity")
    integrity_parser.add_argument("--checkpoint", required=True)
    integrity_parser.add_argument("--result-file", required=True)
    integrity_parser.add_argument("--verification-attempt-id", required=True)
    integrity_parser.add_argument("--operation-id", required=True)
    integrity_parser.add_argument("--checkpoint-id", required=True)
    integrity_parser.add_argument("--package-build-id", required=True)
    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.add_argument("--checkpoint", required=True)
    smoke_parser.add_argument("--context-limit", type=int, required=True)
    smoke_parser.add_argument("--context-test-tokens", type=int, default=0)
    smoke_parser.add_argument("--device", default="cpu")
    smoke_parser.add_argument("--result-file")
    smoke_parser.add_argument("--verification-attempt-id", default="standalone")
    smoke_parser.add_argument("--operation-id", default="standalone")
    smoke_parser.add_argument("--checkpoint-id")
    smoke_parser.add_argument("--package-build-id", default="development")
    smoke_parser.add_argument("--skip-integrity", action="store_true")
    arguments = parser.parse_args()
    started_at = utc_now_timestamp()
    checkpoint_path = str(Path(getattr(arguments, "checkpoint", "")).resolve())
    try:
        if arguments.command == "inspect":
            payload = inspect_checkpoint(
                arguments.checkpoint,
                calculate_checksum=not arguments.skip_checksum,
            ).to_dict()
        elif arguments.command == "verify-integrity":
            report = inspect_checkpoint(
                arguments.checkpoint,
                calculate_checksum=True,
            )
            if not report.valid:
                raise ValueError("; ".join(report.errors))
            payload = {
                "success": True,
                "verification_attempt_id": arguments.verification_attempt_id,
                "operation_id": arguments.operation_id,
                "checkpoint_id": arguments.checkpoint_id,
                "checkpoint_path": checkpoint_path,
                "manifest_valid": True,
                "weights_exist": True,
                "checksums_valid": True,
                "parameter_count": report.parameter_count,
                "worker_pid": os.getpid(),
                "package_build_id": arguments.package_build_id,
                "started_at": started_at,
                "finished_at": utc_now_timestamp(),
                "error": None,
            }
        else:
            payload = run_smoke_test(
                arguments.checkpoint,
                context_limit=arguments.context_limit,
                context_test_tokens=arguments.context_test_tokens,
                device=arguments.device,
                verification_attempt_id=arguments.verification_attempt_id,
                operation_id=arguments.operation_id,
                checkpoint_id=arguments.checkpoint_id,
                package_build_id_value=arguments.package_build_id,
                verify_integrity=not arguments.skip_integrity,
            )
            payload["started_at"] = started_at
            payload["finished_at"] = utc_now_timestamp()
        if getattr(arguments, "result_file", None):
            atomic_write_json(arguments.result_file, payload)
        else:
            print(json.dumps(payload))
        return 0
    except Exception as error:
        failure = {
            "success": False,
            "verification_attempt_id": getattr(
                arguments, "verification_attempt_id", "standalone"
            ),
            "operation_id": getattr(arguments, "operation_id", "standalone"),
            "checkpoint_id": Path(checkpoint_path).name if checkpoint_path else None,
            "checkpoint_path": checkpoint_path,
            "failed_phase": (
                "checkpoint integrity"
                if arguments.command == "verify-integrity"
                else "isolated model loading"
                if arguments.command == "smoke"
                else "checkpoint inspection"
            ),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "technical_traceback": traceback.format_exc(),
            "worker_pid": os.getpid(),
            "package_build_id": getattr(
                arguments, "package_build_id", package_build_id(
                    Path(__file__).resolve().parents[3]
                )
            ),
            "started_at": started_at,
            "finished_at": utc_now_timestamp(),
        }
        if getattr(arguments, "result_file", None):
            atomic_write_json(arguments.result_file, failure)
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
