"""App-owned contract for Steak Gen 1 ScaledFP8 image generation.

This module intentionally contains no Torch or model-framework imports.  The native
desktop host can validate a request and launch the isolated image worker
without importing either framework into the long-lived chat backend process.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


PUBLIC_MODEL_ID = "steak-gen-1-scaledfp8"
PUBLIC_MODEL_NAME = "Steak Gen 1 ScaledFP8"
PRIVATE_WORKER_PYTHON_FLAGS = ("-B",)
SUPPORT_REVISION = "f332072aa78be7aecdf3ee76d5c247082da564a6"
TRANSFORMER_SIZE_BYTES = 7_245_310_296
TRANSFORMER_SHA256 = (
    "aff8c784b9e703908b1c2e84f228c6f523504e9f9dd0307d661df8be916d5a09"
)
INTEGRITY_CACHE_SCHEMA = "salty-steak-image-bundle-integrity-cache-v1"


class SteakGenError(RuntimeError):
    """Base error for fail-closed Steak Gen execution."""


class SteakGenCancelled(SteakGenError):
    """Raised when a generation is cancelled before an image is committed."""


class SteakGenIntegrityError(SteakGenError):
    """Raised when an exact model or support artifact fails verification."""


@dataclass(frozen=True)
class PinnedFile:
    size: int
    algorithm: str
    digest: str





PINNED_SUPPORT_FILES: Mapping[str, PinnedFile] = {
    "model_index.json": PinnedFile(467, "git_blob_sha1", "570c63a7c5580f63d0a8ed622324f50015dcf914"),
    "scheduler/scheduler_config.json": PinnedFile(173, "git_blob_sha1", "2349bc43351afa730341d07dd44fce4ace1c4257"),
    "text_encoder/config.json": PinnedFile(726, "git_blob_sha1", "e49eccdc32f36da9c09cfa0e737084f9e0105e5e"),
    "text_encoder/generation_config.json": PinnedFile(239, "git_blob_sha1", "20a8a9156fc8c3f25295ca067f61fdf120d517c5"),
    "text_encoder/model-00001-of-00003.safetensors": PinnedFile(3_957_900_840, "sha256", "328a91d3122359d5547f9d79521205bc0a46e1f79a792dfe650e99fc2d651223"),
    "text_encoder/model-00002-of-00003.safetensors": PinnedFile(3_987_450_520, "sha256", "6cd087b316306a68c562436b5492edbcf6e16c6dba3a1308279caa5a58e21ca5"),
    "text_encoder/model-00003-of-00003.safetensors": PinnedFile(99_630_640, "sha256", "7ca841ee75b9c61267c0c6148fd8d096d3d21b6d3e161256a9b878154f91fc52"),
    "text_encoder/model.safetensors.index.json": PinnedFile(32_819, "git_blob_sha1", "95c0a0059df040d75dc6c396b174382cf61d2f91"),
    "tokenizer/merges.txt": PinnedFile(1_671_853, "git_blob_sha1", "31349551d90c7606f325fe0f11bbb8bd5fa0d7c7"),
    "tokenizer/tokenizer.json": PinnedFile(11_422_654, "sha256", "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"),
    "tokenizer/tokenizer_config.json": PinnedFile(9_732, "git_blob_sha1", "417d038a63fa3de29cfde265caedae14d1a58d92"),
    "tokenizer/vocab.json": PinnedFile(2_776_833, "git_blob_sha1", "4783fe10ac3adce15ac8f358ef5462739852c569"),
    "vae/config.json": PinnedFile(805, "git_blob_sha1", "894fa30ac1950cef422189d2d4cef11043c62875"),
    "vae/diffusion_pytorch_model.safetensors": PinnedFile(167_666_902, "sha256", "f5b59a26851551b67ae1fe58d32e76486e1e812def4696a4bea97f16604d40a3"),
}


@dataclass(frozen=True)
class SteakGenPaths:
    workspace_root: Path
    bundle_root: Path
    transformer_path: Path
    support_root: Path
    output_root: Path
    cache_root: Path
    control_root: Path

    @classmethod
    def from_workspace(cls, workspace_root: str | Path) -> "SteakGenPaths":
        workspace = Path(workspace_root).resolve()
        bundle = (
            workspace
            / "models"
            / "image-generation"
            / "steak-gen-1-scaledfp8"
        )
        return cls(
            workspace_root=workspace,
            bundle_root=bundle,
            transformer_path=bundle / "Steak gen 1 ScaledFP8.safetensors",
            support_root=bundle / "pipeline-support-f332072",
            output_root=workspace / "generated" / "images",
            cache_root=workspace / "cache" / "steak-gen-runtime",
            control_root=workspace / "control",
        )

    def validate_layout(self) -> None:
        if not self.workspace_root.is_dir():
            raise SteakGenIntegrityError(
                f"Workspace does not exist: {self.workspace_root}"
            )
        if not self.bundle_root.is_dir():
            raise SteakGenIntegrityError(
                f"Image model bundle does not exist: {self.bundle_root}"
            )
        if not self.transformer_path.is_file():
            raise SteakGenIntegrityError(
                f"Canonical transformer does not exist: {self.transformer_path}"
            )
        if not self.support_root.is_dir():
            raise SteakGenIntegrityError(
                f"Pinned support bundle does not exist: {self.support_root}"
            )


@dataclass(frozen=True)
class SteakGenRequest:
    prompt: str
    output_path: str
    negative_prompt: str = ""
    width: int = 512
    height: int = 512
    steps: int = 8
    guidance_scale: float = 0.0
    seed: int = 0
    max_sequence_length: int = 512
    text_runtime_unloaded: bool = False
    verify_hashes: bool = True

    def validate(self, paths: SteakGenPaths) -> Path:
        if not str(self.prompt).strip():
            raise ValueError("Image prompt cannot be empty")
        if len(self.prompt) > 16_000 or len(self.negative_prompt) > 16_000:
            raise ValueError("Image prompts cannot exceed 16000 characters")
        if not self.text_runtime_unloaded:
            raise SteakGenError(
                "Base Steak must be unloaded before Steak Gen can acquire the GPU"
            )
        for label, value in (("width", self.width), ("height", self.height)):
            if value < 256 or value > 1024 or value % 16:
                raise ValueError(f"{label} must be 256..1024 and divisible by 16")
        if not 1 <= self.steps <= 50:
            raise ValueError("steps must be between 1 and 50")
        if not 0.0 <= self.guidance_scale <= 20.0:
            raise ValueError("guidance_scale must be between 0 and 20")
        if not 0 <= self.seed <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("seed must fit an unsigned 64-bit integer")
        if not 32 <= self.max_sequence_length <= 512:
            raise ValueError("max_sequence_length must be between 32 and 512")

        destination = Path(self.output_path).resolve()
        root = paths.output_root.resolve()
        if destination.suffix.casefold() != ".png":
            raise ValueError("Steak Gen output must be a PNG file")
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"Image output must stay inside the workspace output root: {root}"
            ) from error
        return destination


@dataclass(frozen=True)
class SteakGenResult:
    output_path: str
    output_sha256: str
    width: int
    height: int
    seed: int
    steps: int
    model_id: str = PUBLIC_MODEL_ID
    model_name: str = PUBLIC_MODEL_NAME
    transformer_sha256: str = TRANSFORMER_SHA256
    support_revision: str = SUPPORT_REVISION


class CancellationSignal:
    """Filesystem cancellation signal shared with the isolated worker."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    @property
    def cancelled(self) -> bool:
        return self.path.is_file()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise SteakGenCancelled("Steak Gen image generation was cancelled")

    def cancel(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _stream_digest(path: Path, algorithm: str) -> str:
    if algorithm == "sha256":
        digest = hashlib.sha256()
        prefix = b""
    elif algorithm == "git_blob_sha1":
        digest = hashlib.sha1()
        prefix = f"blob {path.stat().st_size}\0".encode("ascii")
    else:
        raise ValueError(f"Unsupported integrity algorithm: {algorithm}")
    digest.update(prefix)
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_exact_bundle(
    paths: SteakGenPaths,
    *,
    verify_hashes: bool = True,
    support_files: Mapping[str, PinnedFile] = PINNED_SUPPORT_FILES,
    transformer_size: int = TRANSFORMER_SIZE_BYTES,
    transformer_sha256: str = TRANSFORMER_SHA256,
) -> dict[str, Any]:
    """Verify the exact transformer and support allow-list.

    Embedded SafeTensors metadata is never returned here.  It remains preserved
    in the canonical file as technical evidence, while public identity is owned
    by Salty Steak's manifest and this runtime contract.
    """

    paths.validate_layout()
    issues: list[str] = []
    transformer_size_actual = paths.transformer_path.stat().st_size
    if transformer_size_actual != transformer_size:
        issues.append(
            "transformer size mismatch: "
            f"{transformer_size_actual} != {transformer_size}"
        )
    transformer_digest: str | None = None
    if verify_hashes and transformer_size_actual == transformer_size:
        transformer_digest = _stream_digest(paths.transformer_path, "sha256")
        if transformer_digest.casefold() != transformer_sha256.casefold():
            issues.append("transformer SHA-256 mismatch")

    actual_support = {
        file.relative_to(paths.support_root).as_posix()
        for file in paths.support_root.rglob("*")
        if file.is_file()
    }
    expected_support = set(support_files)
    for relative in sorted(expected_support - actual_support):
        issues.append(f"missing support file: {relative}")
    for relative in sorted(actual_support - expected_support):
        issues.append(f"unexpected support file: {relative}")

    verified_files = 0
    for relative, pin in support_files.items():
        candidate = (paths.support_root / Path(relative)).resolve()
        try:
            candidate.relative_to(paths.support_root.resolve())
        except ValueError:
            issues.append(f"support path escapes bundle: {relative}")
            continue
        if not candidate.is_file():
            continue
        actual_size = candidate.stat().st_size
        if actual_size != pin.size:
            issues.append(
                f"support size mismatch: {relative}: {actual_size} != {pin.size}"
            )
            continue
        if verify_hashes:
            actual_digest = _stream_digest(candidate, pin.algorithm)
            if actual_digest.casefold() != pin.digest.casefold():
                issues.append(f"support hash mismatch: {relative}")
                continue
        verified_files += 1

    if issues:
        raise SteakGenIntegrityError("; ".join(issues))
    return {
        "model_id": PUBLIC_MODEL_ID,
        "model_name": PUBLIC_MODEL_NAME,
        "transformer_size": transformer_size_actual,
        "transformer_sha256": transformer_digest or transformer_sha256,
        "support_revision": SUPPORT_REVISION,
        "support_file_count": len(support_files),
        "support_files_verified": verified_files,
        "hashes_recomputed": bool(verify_hashes),
        "embedded_metadata_visibility": "technical_only",
        "upstream_transformer_included": False,
    }


def _bundle_stat_fingerprint(
    paths: SteakGenPaths,
    support_files: Mapping[str, PinnedFile],
) -> tuple[str, list[dict[str, object]]]:
    files = [paths.transformer_path] + [
        paths.support_root / relative for relative in sorted(support_files)
    ]
    records: list[dict[str, object]] = []
    for path in files:
        stat = path.stat()
        records.append(
            {
                "path": path.relative_to(paths.bundle_root).as_posix(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    encoded = json.dumps(records, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest(), records


def verify_exact_bundle_cached(
    paths: SteakGenPaths,
    *,
    support_files: Mapping[str, PinnedFile] = PINNED_SUPPORT_FILES,
    transformer_size: int = TRANSFORMER_SIZE_BYTES,
    transformer_sha256: str = TRANSFORMER_SHA256,
) -> dict[str, Any]:
    """Reuse a full local hash audit while every source file is unchanged.

    The image worker used to reread roughly 15 GiB before every picture. The
    first full audit is still mandatory. Later workers compare the complete
    allow-list plus size and nanosecond modification identity; any ordinary
    file replacement or edit invalidates the cache and recomputes every hash.
    """

    paths.validate_layout()
    paths.cache_root.mkdir(parents=True, exist_ok=True)
    cache_path = paths.cache_root / "bundle-integrity-v1.json"
    fingerprint, records = _bundle_stat_fingerprint(paths, support_files)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        cached = {}
    cache_valid = bool(
        isinstance(cached, dict)
        and cached.get("schema") == INTEGRITY_CACHE_SCHEMA
        and cached.get("fingerprint_sha256") == fingerprint
        and cached.get("transformer_sha256") == transformer_sha256
        and cached.get("support_revision") == SUPPORT_REVISION
        and cached.get("support_file_count") == len(support_files)
        and cached.get("verified") is True
    )
    if cache_valid:
        return {
            "model_id": PUBLIC_MODEL_ID,
            "model_name": PUBLIC_MODEL_NAME,
            "transformer_size": transformer_size,
            "transformer_sha256": transformer_sha256,
            "support_revision": SUPPORT_REVISION,
            "support_file_count": len(support_files),
            "support_files_verified": len(support_files),
            "hashes_recomputed": False,
            "verification_cache_hit": True,
            "integrity_fingerprint_sha256": fingerprint,
            "embedded_metadata_visibility": "technical_only",
            "upstream_transformer_included": False,
        }

    report = verify_exact_bundle(
        paths,
        verify_hashes=True,
        support_files=support_files,
        transformer_size=transformer_size,
        transformer_sha256=transformer_sha256,
    )
    payload = {
        "schema": INTEGRITY_CACHE_SCHEMA,
        "verified": True,
        "fingerprint_sha256": fingerprint,
        "files": records,
        "transformer_sha256": transformer_sha256,
        "support_revision": SUPPORT_REVISION,
        "support_file_count": len(support_files),
    }
    temporary = cache_path.with_name(
        f".{cache_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, cache_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        **report,
        "verification_cache_hit": False,
        "integrity_fingerprint_sha256": fingerprint,
    }


class _ExclusiveGpuLease:
    """Cross-process one-byte lock for one Salty Steak accelerator job."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: Any = None

    def __enter__(self) -> "_ExclusiveGpuLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            stream.close()
            raise SteakGenError(
                "Another Salty Steak accelerator job already owns the GPU lease"
            ) from error
        self._stream = stream
        return self

    def __exit__(self, *_args: Any) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class SteakGenWorkerClient:
    """Launch one isolated worker for exactly one image and then exit."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        python_executable: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.paths = SteakGenPaths.from_workspace(workspace_root)
        if python_executable is not None:
            worker_python = Path(python_executable)
        else:
            packaged = self.project_root / ".venv" / "Scripts" / "python.exe"
            private_base = self.project_root / ".python" / "python.exe"
            worker_python = (
                packaged
                if packaged.is_file()
                else private_base
                if private_base.is_file()
                else Path(sys.executable)
            )
        self.python_executable = worker_python.resolve()
        if self.python_executable.name.casefold() not in {
            "python.exe",
            "pythonw.exe",
            "python",
        }:
            raise SteakGenError(
                "Steak Gen could not find Salty Steak's private Python runtime"
            )
        self._lock = threading.Lock()

    def generate(
        self,
        request: SteakGenRequest,
        *,
        should_stop: Callable[[], bool] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        timeout_seconds: float = 60 * 60,
    ) -> SteakGenResult:
        request.validate(self.paths)
        self.paths.control_root.mkdir(parents=True, exist_ok=True)
        self.paths.cache_root.mkdir(parents=True, exist_ok=True)
        signal = CancellationSignal(
            self.paths.control_root / f"steak-gen-cancel-{uuid.uuid4().hex}.signal"
        )
        signal.clear()
        lease_path = self.paths.control_root / "salty-steak-accelerator.lock"
        with self._lock, _ExclusiveGpuLease(lease_path):
            try:
                return self._run_worker(
                    request,
                    signal=signal,
                    should_stop=should_stop,
                    on_event=on_event,
                    timeout_seconds=timeout_seconds,
                )
            finally:
                signal.clear()

    def _run_worker(
        self,
        request: SteakGenRequest,
        *,
        signal: CancellationSignal,
        should_stop: Callable[[], bool] | None,
        on_event: Callable[[dict[str, Any]], None] | None,
        timeout_seconds: float,
    ) -> SteakGenResult:
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HOME": str(self.paths.cache_root / "huggingface"),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TEMP": str(self.paths.cache_root / "temp"),
                "TMP": str(self.paths.cache_root / "temp"),
            }
        )
        (self.paths.cache_root / "temp").mkdir(parents=True, exist_ok=True)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [
                str(self.python_executable),
                *PRIVATE_WORKER_PYTHON_FLAGS,
                "-m",
                "app.backend.runtime.steak_gen_worker",
                "--serve-once",
            ],
            cwd=self.project_root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        payload = {
            "workspace_root": str(self.paths.workspace_root),
            "cancellation_path": str(signal.path),
            "request": asdict(request),
        }
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()
        process.stdin.close()

        lines: queue.Queue[str | None] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=200)

        def read_stdout() -> None:
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        def read_stderr() -> None:
            for line in process.stderr:
                stderr_tail.append(line)

        def stop_process() -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

        reader = threading.Thread(target=read_stdout, daemon=True)
        error_reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        error_reader.start()
        started = time.monotonic()
        cancellation_started: float | None = None
        response: dict[str, Any] | None = None
        try:
            while response is None:
                now = time.monotonic()
                if should_stop is not None and should_stop() and not signal.cancelled:
                    signal.cancel()
                    cancellation_started = now
                if cancellation_started is not None and now - cancellation_started > 15:
                    stop_process()
                    raise SteakGenCancelled(
                        "Steak Gen image generation was cancelled; the isolated "
                        "worker was stopped after its cancellation grace period"
                    )
                if now - started > timeout_seconds:
                    signal.cancel()
                    stop_process()
                    raise TimeoutError(
                        "Steak Gen isolated worker exceeded its timeout"
                    )
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                message = json.loads(line)
                if message.get("event"):
                    if on_event is not None:
                        on_event(dict(message))
                    continue
                response = dict(message)
        except BaseException:
            stop_process()
            raise

        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        error_reader.join(timeout=2)
        stderr = "".join(stderr_tail).strip()
        process.stdout.close()
        process.stderr.close()
        if response is None:
            raise SteakGenError(
                "Steak Gen worker exited without a result"
                + (f": {stderr[-2000:]}" if stderr else "")
            )
        if not response.get("ok"):
            error = dict(response.get("error") or {})
            error_type = str(error.get("type") or "SteakGenError")
            message = str(error.get("message") or "Steak Gen worker failed")
            if error_type == "SteakGenCancelled":
                raise SteakGenCancelled(message)
            raise SteakGenError(f"{error_type}: {message}")
        result = dict(response["result"])
        return SteakGenResult(**result)
