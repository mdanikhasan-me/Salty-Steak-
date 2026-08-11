"""Salty Steak Native Desktop AI Platform — Salty Multi-Modal Vision broker.

The vision path is deliberately separate from the persistent text worker.  It
uses an isolated, one-shot vision process with no listener and cannot read
local image material until the caller supplies a permission object bound to the
exact image path and SHA-256.  This module does not register or activate the
companion model by itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..system.files import atomic_write_json


BASE_STEAK_PUBLIC_NAME = "Base Steak 2.0"
BASE_STEAK_TEXT_SHA256 = (
    "c725cc0e496ee7e6cf1b165171507fde0ae6b8cc8509130f391df366a604f3ca"
)
BASE_STEAK_VISION_SHA256 = (
    "05f662501f8bd45607b079723a3e238a4e888fd085a10a53f4057a0e250f6934"
)
VISION_RUNTIME_ID = "salty_vision_engine_steak20_b10333"
VISION_STAGE_SCHEMA = "salty-steak-vision-runtime-v1"
VISION_VALIDATOR_VERSION = "salty-vision-validator-v2"
VISION_STAGE_MANIFEST = "vision-runtime.json"
VISION_INTEGRITY_CACHE_SCHEMA = "salty-steak-vision-integrity-cache-v1"
VISION_LICENSE_FILE = "THIRD_PARTY_LICENSE.txt"
VISION_SMOKE_IMAGE_SHA256 = (
    "14a487697c059a674563f01808991bea7652c342936c0bc46967625195deee66"
)
VISION_SMOKE_OUTPUT_SHA256 = (
    "20ae9915f06550a35c472c1d0e80b48ed3ee8895c6e3b2e3f8c59fda9fc184fa"
)
VISION_RUNTIME_FILES = (
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
    "llama.dll",
    "llama-common.dll",
    "mtmd.dll",
    "llama-mtmd-cli.exe",
)
VISION_CUDA_RUNTIME_FILES = (
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudart64_12.dll",
    "ggml-cuda.dll",
    "libomp140.x86_64.dll",
)
ALLOWED_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}
MAX_IMAGE_BYTES = 25 * 1024 * 1024











VISION_RUNTIME_SHA256 = {
    "cublas64_12.dll": "e40202fe4223c1cd2d2dce7beec59e1ed61c7801bd827309183be9b50e358f4c",
    "cublasLt64_12.dll": "2a896460bef60ed57ef32b0875812f355a6984e671d638bb632f5e8c1d7a831f",
    "cudart64_12.dll": "d28e42265da7462162a54da6b7a99ea4fa2caf8139d862bb500db875d0b32dfc",
    "ggml.dll": "c44601e1ea7e15e4bba1a51b93ded7f9c70f1e0dc6af61650f44cbe913059640",
    "ggml-base.dll": "b53433fdba89a8456d688fb93e851285808ed3701fab9bff404dbeb86f2b66f2",
    "ggml-cpu.dll": "92eae795c6375d2facc57752f0fa5f5d6c9e495700400261aeaa1e84f10a8569",
    "ggml-cuda.dll": "b1958cc67dace83d533e12de56389c10e3577ccf4ec381e2734c38aab473c02d",
    "libomp140.x86_64.dll": "4a20c1e5c115c29771a12324513eb109badac72180f79481527ad79d996ffb33",
    "llama.dll": "2c6f5e9b2d6bc59b7bef41680bacd5b278656aeea43eadd9308b1ede55071a3c",
    "llama-common.dll": "dea8c975229c107b0c701a58818913e56c5fa99e34682c63730a67fcb4c78edb",
    "llama-mtmd-cli.exe": "c96389291a8d1814f8e0c1d7028d7e2e3fb8d419b2c66f15c6b345651fa621d0",
    "mtmd.dll": "734ced986b0c86ca8feb235af81ce11327bec32b709afb89ad02a3e230655afb",
}


class SaltyVisionError(RuntimeError):
    """Raised when optional vision execution cannot pass a safety gate."""


class SaltyVisionCancelled(SaltyVisionError):
    """Raised after a requested safe stop terminates the one-shot process."""


@dataclass(frozen=True)
class VisionInputPermission:
    """App-issued approval bound to one exact local image payload."""

    granted: bool
    request_id: str
    source: str
    approved_image_sha256: str
    approved_path: str | None = None
    conversation_id: str | None = None
    target_model_id: str | None = None

    def validate(self) -> None:
        if not self.granted:
            raise PermissionError("Local image access was not explicitly granted")
        if not self.request_id.strip():
            raise PermissionError("Image permission requires a request identifier")
        if self.source not in {
            "user_attachment",
            "screen_capture",
            "app_owned_validation",
        }:
            raise PermissionError("Image permission source is not app-approved")
        checksum = self.approved_image_sha256.strip().casefold()
        if len(checksum) != 64 or any(value not in "0123456789abcdef" for value in checksum):
            raise PermissionError("Image permission requires an exact SHA-256")
        if self.source in {"user_attachment", "screen_capture"}:
            if not self.conversation_id or not self.target_model_id:
                raise PermissionError(
                    "User image permission must be bound to a conversation and model"
                )


class VisionPermissionLease:
    """Consume an app-issued image permission exactly once."""

    def __init__(self, permission: VisionInputPermission) -> None:
        permission.validate()
        self.permission = permission
        self._used = False
        self._lock = threading.Lock()

    def consume(self) -> VisionInputPermission:
        with self._lock:
            if self._used:
                raise PermissionError("Image permission has already been consumed")
            self._used = True
        return self.permission


@dataclass(frozen=True)
class SaltyVisionResult:
    text: str
    image_sha256: str
    text_model_sha256: str
    projector_sha256: str
    runtime_files_sha256: dict[str, str]
    duration_seconds: float
    command_exit_code: int
    public_model_name: str
    runtime_id: str
    technical_details: dict[str, Any]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _png_chunk(name: bytes, payload: bytes) -> bytes:
    body = name + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def deterministic_smoke_image_png() -> bytes:
    """Return an app-owned deterministic 64x64 red-square PNG fixture."""

    width = height = 64
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            inside = 12 <= x < 52 and 12 <= y < 52
            row.extend((220, 32, 32) if inside else (245, 245, 245))
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )


class SaltyVisionBroker:
    """One-shot broker around the isolated Salty Multi-Modal Vision build."""

    def __init__(
        self,
        *,
        text_model_path: str | Path,
        projector_path: str | Path,
        runtime_directory: str | Path,
        temporary_root: str | Path,
        text_model_sha256: str = BASE_STEAK_TEXT_SHA256,
        projector_sha256: str = BASE_STEAK_VISION_SHA256,
        text_adapters: Sequence[Mapping[str, Any]] = (),
        timeout_seconds: int = 900,
        device: str = "none",
        gpu_layers: int = 0,
        mmproj_offload: bool = False,
        require_staged_manifest: bool = False,
    ) -> None:
        self.text_model_path = Path(text_model_path).resolve()
        self.projector_path = Path(projector_path).resolve()
        self.runtime_directory = Path(runtime_directory).resolve()
        self.temporary_root = Path(temporary_root).resolve()
        self.text_model_sha256 = text_model_sha256.casefold()
        self.projector_sha256 = projector_sha256.casefold()
        self.text_adapters: tuple[dict[str, Any], ...] = tuple(
            {
                "id": str(value.get("id") or value.get("adapter_id") or "").strip(),
                "path": str(Path(str(value.get("path") or "")).resolve()),
                "sha256": str(value.get("sha256") or "").casefold(),
                "scale": float(value.get("scale", 1.0)),
            }
            for value in text_adapters
        )
        adapter_ids: set[str] = set()
        for adapter in self.text_adapters:
            if not adapter["id"] or adapter["id"] in adapter_ids:
                raise ValueError("Vision text adapter IDs must be non-empty and unique")
            adapter_ids.add(adapter["id"])
            digest = adapter["sha256"]
            if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
                raise ValueError("Vision text adapter SHA-256 is invalid")
            if not 0 < adapter["scale"] <= 16:
                raise ValueError("Vision text adapter scale must be between 0 and 16")
        self.timeout_seconds = int(timeout_seconds)
        self.device = str(device).strip()
        self.gpu_layers = int(gpu_layers)
        self.mmproj_offload = bool(mmproj_offload)
        self.require_staged_manifest = bool(require_staged_manifest)
        if self.device not in {"none", "CUDA0"}:
            raise ValueError("Vision candidate device must be none or CUDA0")
        if self.gpu_layers < 0 or self.gpu_layers > 99:
            raise ValueError("Vision candidate gpu_layers must be between 0 and 99")
        if self.device == "none" and (self.gpu_layers or self.mmproj_offload):
            raise ValueError("CPU vision cannot request GPU offload")
        self._verified_model_stats: dict[str, tuple[int, int]] = {}
        self._runtime_hashes: dict[str, str] = {}
        self._last_smoke: dict[str, Any] | None = None
        self._staged_manifest: dict[str, Any] | None = None
        self._stage_error: str | None = None
        self._verification_in_progress = False
        self._verification_lock = threading.Lock()
        self._integrity_cache_hit = False
        self.integrity_cache_path = (
            self.temporary_root.parent / "salty-vision-integrity-cache-v1.json"
        )
        if self.require_staged_manifest:
            self._restore_integrity_cache()

    def _adapter_manifest_records(self) -> list[dict[str, Any]]:
        return [
            {
                "id": adapter["id"],
                "filename": Path(adapter["path"]).name,
                "sha256": adapter["sha256"],
                "scale": adapter["scale"],
            }
            for adapter in self.text_adapters
        ]

    def _verified_artifact_paths(self) -> dict[str, Path]:
        paths = {
            "text": self.text_model_path,
            "projector": self.projector_path,
        }
        paths.update(
            {
                f"adapter:{adapter['id']}": Path(adapter["path"])
                for adapter in self.text_adapters
            }
        )
        return paths

    @property
    def executable(self) -> Path:
        return self.runtime_directory / "llama-mtmd-cli.exe"

    def _runtime_file_paths(self) -> dict[str, Path]:
        required = list(VISION_RUNTIME_FILES)
        if self.device == "CUDA0":
            required.extend(VISION_CUDA_RUNTIME_FILES)
        return {
            name: self.runtime_directory / name
            for name in required
        }

    @property
    def license_path(self) -> Path:
        return self.runtime_directory / VISION_LICENSE_FILE

    @property
    def stage_manifest_path(self) -> Path:
        return self.runtime_directory / VISION_STAGE_MANIFEST

    def _load_staged_manifest(self) -> dict[str, Any] | None:
        self._stage_error = None
        if not self.stage_manifest_path.is_file():
            self._stage_error = f"{VISION_STAGE_MANIFEST} is missing"
            return None
        try:
            manifest = json.loads(
                self.stage_manifest_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._stage_error = f"{VISION_STAGE_MANIFEST} is unreadable"
            return None
        if not isinstance(manifest, dict):
            self._stage_error = "Vision runtime manifest must be an object"
            return None
        expected_scalars = {
            "schema": VISION_STAGE_SCHEMA,
            "runtime_id": VISION_RUNTIME_ID,
            "validator_version": VISION_VALIDATOR_VERSION,
            "text_model_sha256": self.text_model_sha256,
            "projector_sha256": self.projector_sha256,
        }
        for field, expected in expected_scalars.items():
            if manifest.get(field) != expected:
                self._stage_error = f"Vision runtime manifest differs for {field}"
                return None
        if manifest.get("text_adapters", []) != self._adapter_manifest_records():
            self._stage_error = "Vision runtime manifest text adapters differ"
            return None
        declared_runtime = manifest.get("runtime_files_sha256")
        required_hashes = {
            name: VISION_RUNTIME_SHA256[name]
            for name in self._runtime_file_paths()
        }
        if declared_runtime != required_hashes:
            self._stage_error = "Vision runtime manifest file identities differ"
            return None
        license_record = manifest.get("third_party_license")
        if (
            not isinstance(license_record, dict)
            or license_record.get("filename") != VISION_LICENSE_FILE
            or not isinstance(license_record.get("sha256"), str)
            or len(license_record["sha256"]) != 64
            or not self.license_path.is_file()
            or _sha256_file(self.license_path) != license_record["sha256"]
        ):
            self._stage_error = "Vision third-party license attribution is missing or changed"
            return None
        profile = manifest.get("runtime_profile")
        expected_profile = {
            "device": self.device,
            "gpu_layers": self.gpu_layers,
            "mmproj_offload": self.mmproj_offload,
            "context_tokens": 4096,
            "batch_size": 512,
            "micro_batch_size": 256,
        }
        if profile != expected_profile:
            self._stage_error = "Vision runtime manifest profile differs"
            return None
        smoke = manifest.get("deterministic_smoke")
        if not isinstance(smoke, dict) or (
            smoke.get("passed") is not True
            or smoke.get("image_sha256") != VISION_SMOKE_IMAGE_SHA256
            or smoke.get("output_sha256") != VISION_SMOKE_OUTPUT_SHA256
            or smoke.get("command_exit_code") != 0
            or not isinstance(smoke.get("validated_at_utc"), str)
            or not smoke.get("validated_at_utc")
        ):
            self._stage_error = "Vision runtime manifest lacks the exact smoke evidence"
            return None
        return manifest

    @staticmethod
    def _file_identity(path: Path, checksum: str) -> dict[str, Any]:
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "size_bytes": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "sha256": str(checksum).casefold(),
        }

    def _write_integrity_cache(self, manifest: Mapping[str, Any]) -> None:
        artifacts = self._verified_artifact_paths()
        expected_artifact_hashes = {
            "text": self.text_model_sha256,
            "projector": self.projector_sha256,
            **{
                f"adapter:{adapter['id']}": adapter["sha256"]
                for adapter in self.text_adapters
            },
        }
        atomic_write_json(
            self.integrity_cache_path,
            {
                "schema": VISION_INTEGRITY_CACHE_SCHEMA,
                "stage_manifest_sha256": _sha256_file(self.stage_manifest_path),
                "text_model_sha256": self.text_model_sha256,
                "projector_sha256": self.projector_sha256,
                "text_adapters": self._adapter_manifest_records(),
                "runtime_profile": manifest.get("runtime_profile"),
                "artifacts": {
                    label: self._file_identity(
                        path, expected_artifact_hashes[label]
                    )
                    for label, path in artifacts.items()
                },
                "runtime_files": {
                    name: self._file_identity(path, self._runtime_hashes[name])
                    for name, path in self._runtime_file_paths().items()
                },
            },
        )

    @staticmethod
    def _cached_file_matches(record: object, path: Path, checksum: str) -> bool:
        if not isinstance(record, Mapping) or not path.is_file():
            return False
        stat = path.stat()
        return bool(
            str(record.get("path") or "").casefold()
            == str(path.resolve()).casefold()
            and int(record.get("size_bytes") or -1) == int(stat.st_size)
            and int(record.get("mtime_ns") or -1) == int(stat.st_mtime_ns)
            and str(record.get("sha256") or "").casefold()
            == str(checksum).casefold()
        )

    def _restore_integrity_cache(self) -> bool:
        self._integrity_cache_hit = False
        if not self.integrity_cache_path.is_file():
            return False
        manifest = self._load_staged_manifest()
        if manifest is None:
            return False
        try:
            cached = json.loads(
                self.integrity_cache_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(cached, Mapping) or any(
            (
                cached.get("schema") != VISION_INTEGRITY_CACHE_SCHEMA,
                cached.get("stage_manifest_sha256")
                != _sha256_file(self.stage_manifest_path),
                cached.get("text_model_sha256") != self.text_model_sha256,
                cached.get("projector_sha256") != self.projector_sha256,
                cached.get("text_adapters") != self._adapter_manifest_records(),
                cached.get("runtime_profile") != manifest.get("runtime_profile"),
            )
        ):
            return False
        cached_artifacts = cached.get("artifacts")
        cached_runtime = cached.get("runtime_files")
        if not isinstance(cached_artifacts, Mapping) or not isinstance(
            cached_runtime, Mapping
        ):
            return False
        expected_artifact_hashes = {
            "text": self.text_model_sha256,
            "projector": self.projector_sha256,
            **{
                f"adapter:{adapter['id']}": adapter["sha256"]
                for adapter in self.text_adapters
            },
        }
        artifacts = self._verified_artifact_paths()
        if not all(
            self._cached_file_matches(
                cached_artifacts.get(label),
                path,
                expected_artifact_hashes[label],
            )
            for label, path in artifacts.items()
        ):
            return False
        runtime_paths = self._runtime_file_paths()
        required_runtime_hashes = {
            name: VISION_RUNTIME_SHA256[name] for name in runtime_paths
        }
        if not all(
            self._cached_file_matches(
                cached_runtime.get(name), path, required_runtime_hashes[name]
            )
            for name, path in runtime_paths.items()
        ):
            return False
        self._verified_model_stats = {
            label: (path.stat().st_size, path.stat().st_mtime_ns)
            for label, path in artifacts.items()
        }
        self._runtime_hashes = required_runtime_hashes
        self._last_smoke = dict(manifest["deterministic_smoke"])
        self._staged_manifest = manifest
        self._integrity_cache_hit = True
        return True

    def verify_staged_runtime(self) -> dict[str, Any]:
        """Verify the durable release-time gate without re-running the model."""

        with self._verification_lock:
            self._verification_in_progress = True
            try:
                manifest = self._load_staged_manifest()
                if manifest is None:
                    self._staged_manifest = None
                    self._verified_model_stats = {}
                    self._runtime_hashes = {}
                else:
                    self.verify_integrity()
                    required_hashes = {
                        name: VISION_RUNTIME_SHA256[name]
                        for name in self._runtime_file_paths()
                    }
                    if self._runtime_hashes != required_hashes:
                        self._staged_manifest = None
                        self._stage_error = (
                            "Live vision runtime hashes differ from candidate 1"
                        )
                    else:
                        self._last_smoke = dict(manifest["deterministic_smoke"])
                        self._staged_manifest = manifest
                        self._write_integrity_cache(manifest)
                        self._integrity_cache_hit = False
            finally:
                self._verification_in_progress = False
        return self.status()

    def status(self) -> dict[str, Any]:
        runtime_files = self._runtime_file_paths()
        missing = sorted(
            name for name, path in runtime_files.items() if not path.is_file()
        )
        candidate_built = not missing
        integrity_verified = bool(
            self._verified_model_stats and self._runtime_hashes
        )
        smoke_passed = self._last_smoke is not None
        candidate_ready = bool(candidate_built and integrity_verified and smoke_passed)
        staged_gate_passed = bool(
            self._staged_manifest is not None and candidate_ready
        )
        application_available = bool(
            staged_gate_passed if self.require_staged_manifest else False
        )
        return {
            "public_model_name": BASE_STEAK_PUBLIC_NAME,
            "runtime_id": VISION_RUNTIME_ID,

            "available": application_available,
            "application_available": application_available,
            "activation_allowed": application_available,
            "candidate_ready": candidate_ready,
            "candidate_built": candidate_built,
            "integrity_verified": integrity_verified,
            "smoke_passed": smoke_passed,
            "staged_manifest_required": self.require_staged_manifest,
            "staged_manifest_verified": staged_gate_passed,
            "verification_in_progress": self._verification_in_progress,
            "integrity_cache_hit": self._integrity_cache_hit,
            "stage_error": self._stage_error,
            "missing_runtime_files": missing,
            "third_party_license_present": self.license_path.is_file(),
            "runtime_directory": str(self.runtime_directory),
            "text_model_sha256": self.text_model_sha256,
            "projector_sha256": self.projector_sha256,
            "text_adapters": self._adapter_manifest_records(),
            "runtime_files_sha256": dict(self._runtime_hashes),
            "input_permission_required": True,
            "network_listener_created": False,
            "external_service_required": False,
            "execution_scope": (
                "isolated_one_shot_cuda_candidate"
                if self.device == "CUDA0"
                else "isolated_one_shot_cpu_candidate"
            ),
            "runtime_profile": {
                "device": self.device,
                "gpu_layers": self.gpu_layers,
                "mmproj_offload": self.mmproj_offload,
                "context_tokens": 4096,
                "batch_size": 512,
                "micro_batch_size": 256,
            },
            "reason": (
                "The exact staged runtime and durable deterministic-smoke gate passed."
                if application_available
                else "Vision runtime integrity verification is still running."
                if self._verification_in_progress
                else "Vision remains unavailable until the exact runtime is staged and its durable deterministic-smoke evidence passes."
                if self.require_staged_manifest
                else "The isolated candidate passed, but application staging remains blocked."
                if candidate_ready
                else "Vision remains unavailable until isolated build, exact-hash, and image smoke gates pass."
            ),
            "last_smoke": dict(self._last_smoke) if self._last_smoke else None,
            "private_technical_metadata": {
                "text_architecture": "steak20",
                "projector_architecture": "steak_vision_encoder",
                "projector_type": "steak_vision_merger",
                "engine_revision": "salty-native-b10333-steak20",
            },
        }

    def verify_integrity(self) -> dict[str, Any]:
        expected = {
            "text": (self.text_model_path, self.text_model_sha256),
            "projector": (self.projector_path, self.projector_sha256),
        }
        expected.update(
            {
                f"adapter:{adapter['id']}": (
                    Path(adapter["path"]),
                    adapter["sha256"],
                )
                for adapter in self.text_adapters
            }
        )
        verified_stats: dict[str, tuple[int, int]] = {}
        for label, (path, checksum) in expected.items():
            if not path.is_file():
                raise SaltyVisionError(f"The registered {label} model artifact is missing")
            before = path.stat()
            actual = _sha256_file(path)
            after = path.stat()
            before_identity = (before.st_size, before.st_mtime_ns)
            after_identity = (after.st_size, after.st_mtime_ns)
            if before_identity != after_identity:
                raise SaltyVisionError(
                    f"The {label} model artifact changed while its hash was being verified"
                )
            if actual != checksum:
                raise SaltyVisionError(
                    f"The {label} model artifact checksum differs from its registered identity"
                )
            verified_stats[label] = after_identity

        runtime_hashes: dict[str, str] = {}
        for name, path in self._runtime_file_paths().items():
            if not path.is_file():
                raise SaltyVisionError(f"The isolated vision runtime is missing {name}")
            runtime_hashes[name] = _sha256_file(path)

        self._verified_model_stats = verified_stats
        self._runtime_hashes = runtime_hashes
        return self.status()

    def _assert_verified_files_unchanged(self) -> None:
        if not self._verified_model_stats or not self._runtime_hashes:
            raise SaltyVisionError("Exact model and runtime hashes have not been verified")
        for label, path in self._verified_artifact_paths().items():
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != self._verified_model_stats[label]:
                raise SaltyVisionError(
                    f"The verified {label} model artifact changed before vision execution"
                )
        for name, expected_hash in self._runtime_hashes.items():
            path = self.runtime_directory / name
            if not path.is_file() or _sha256_file(path) != expected_hash:
                raise SaltyVisionError(
                    f"The isolated vision runtime changed before execution: {name}"
                )

    def _materialize_approved_image(
        self,
        directory: Path,
        *,
        permission: VisionInputPermission,
        image_path: str | Path | None,
        image_bytes: bytes | None,
        image_suffix: str,
    ) -> tuple[Path, str, int]:
        permission.validate()
        if (image_path is None) == (image_bytes is None):
            raise ValueError("Provide exactly one of image_path or image_bytes")
        suffix = image_suffix.strip().casefold()
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        if suffix not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError("Unsupported local image format")

        staged = directory / f"approved-input{suffix}"
        if image_path is not None:
            source = Path(image_path)
            if not source.is_absolute():
                raise PermissionError("Approved image paths must be absolute")
            source = source.resolve()
            if permission.approved_path is None:
                raise PermissionError("Path input requires a path-bound permission")
            if source != Path(permission.approved_path).resolve():
                raise PermissionError("Image path does not match the approved attachment")
            if not source.is_file():
                raise SaltyVisionError("The approved local image is missing")
            size = source.stat().st_size
            if size < 1 or size > MAX_IMAGE_BYTES:
                raise ValueError("Local image must contain 1 byte to 25 MiB")
            actual = _sha256_file(source)
            if actual != permission.approved_image_sha256.casefold():
                raise PermissionError("Local image hash does not match its permission")
            shutil.copyfile(source, staged)
        else:
            assert image_bytes is not None
            if not isinstance(image_bytes, bytes):
                raise TypeError("image_bytes must be immutable bytes")
            size = len(image_bytes)
            if size < 1 or size > MAX_IMAGE_BYTES:
                raise ValueError("Local image must contain 1 byte to 25 MiB")
            actual = hashlib.sha256(image_bytes).hexdigest()
            if actual != permission.approved_image_sha256.casefold():
                raise PermissionError("Image bytes do not match their permission")
            staged.write_bytes(image_bytes)

        staged_hash = _sha256_file(staged)
        if staged_hash != permission.approved_image_sha256.casefold():
            raise SaltyVisionError("The staged image failed its integrity check")
        return staged, staged_hash, size

    def generate(
        self,
        *,
        prompt: str,
        permission: VisionInputPermission | VisionPermissionLease,
        image_path: str | Path | None = None,
        image_bytes: bytes | None = None,
        image_suffix: str = ".png",
        maximum_output_tokens: int = 32,
        should_stop: Callable[[], bool] | None = None,
    ) -> SaltyVisionResult:
        checked_prompt = str(prompt).strip()
        if not checked_prompt or len(checked_prompt) > 4_000:
            raise ValueError("Vision prompt must contain 1 to 4,000 characters")
        if not 1 <= int(maximum_output_tokens) <= 256:
            raise ValueError("Vision candidate output must contain 1 to 256 tokens")
        if self.require_staged_manifest and not self.status()[
            "application_available"
        ]:
            raise SaltyVisionError("The staged vision runtime gate has not passed")



        if isinstance(permission, VisionPermissionLease):
            permission = permission.consume()
        elif permission.source in {"user_attachment", "screen_capture"}:
            raise PermissionError("User image permission requires a one-use app lease")

        permission.validate()
        self._assert_verified_files_unchanged()
        self.temporary_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix="salty-vision-",
            dir=self.temporary_root,
        ) as directory_name:
            directory = Path(directory_name)
            staged_image, image_sha256, image_size = self._materialize_approved_image(
                directory,
                permission=permission,
                image_path=image_path,
                image_bytes=image_bytes,
                image_suffix=image_suffix,
            )
            command = [
                str(self.executable),
                "--model",
                str(self.text_model_path),
                "--mmproj",
                str(self.projector_path),
                "--image",
                str(staged_image),
                "--prompt",
                checked_prompt,
                "--predict",
                str(int(maximum_output_tokens)),
                "--ctx-size",
                "4096",
                "--batch-size",
                "512",
                "--ubatch-size",
                "256",
                "--threads",
                "12",
                "--threads-batch",
                "12",
                "--device",
                self.device,
                "--gpu-layers",
                str(self.gpu_layers),
                (
                    "--mmproj-offload"
                    if self.mmproj_offload
                    else "--no-mmproj-offload"
                ),
                "--fit",
                "off",
                "--cache-type-k",
                "q8_0",
                "--cache-type-v",
                "q8_0",
                "--no-warmup",
                "--offline",
                "--seed",
                "0",
                "--temp",
                "0",
                "--top-k",
                "1",
                "--top-p",
                "1",
                "--min-p",
                "0",
                "--log-colors",
                "off",
                "--verbosity",
                "2",
            ]
            if self.text_adapters:
                scaled_adapters = ",".join(
                    f"{Path(os.path.relpath(adapter['path'], self.runtime_directory)).as_posix()}:{adapter['scale']:g}"
                    for adapter in self.text_adapters
                )
                command[5:5] = ["--lora-scaled", scaled_adapters]
            environment = os.environ.copy()
            environment["PATH"] = (
                f"{self.runtime_directory}{os.pathsep}{environment.get('PATH', '')}"
            )
            environment["HF_HUB_OFFLINE"] = "1"
            environment["TRANSFORMERS_OFFLINE"] = "1"
            started = time.perf_counter()
            process: subprocess.Popen[str] | None = None
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.runtime_directory,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        if os.name == "nt"
                        else 0
                    ),
                )
                while True:
                    if should_stop is not None and should_stop():
                        _terminate_process_tree(process)
                        process.communicate()
                        raise SaltyVisionCancelled(
                            "Vision analysis stopped; no partial output was saved"
                        )
                    elapsed = time.perf_counter() - started
                    if elapsed >= self.timeout_seconds:
                        _terminate_process_tree(process)
                        process.communicate()
                        raise SaltyVisionError(
                            "Isolated vision execution exceeded "
                            f"{self.timeout_seconds} seconds"
                        )
                    try:
                        stdout, stderr = process.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                if process is not None and process.poll() is None:
                    _terminate_process_tree(process)
                raise
            duration = time.perf_counter() - started
            assert process is not None
            output = stdout.strip()
            if process.returncode != 0:
                error_tail = stderr.strip()[-2_000:]
                raise SaltyVisionError(
                    "Isolated vision execution failed"
                    + (f": {error_tail}" if error_tail else "")
                )
            if not output:
                raise SaltyVisionError("Isolated vision execution returned no output")
            for empty_think in (
                "<think>\n\n</think>\n\n",
                "<think>\r\n\r\n</think>\r\n\r\n",
            ):
                if output.startswith(empty_think):
                    output = output[len(empty_think) :].lstrip()
                    break
            if not output:
                raise SaltyVisionError(
                    "Isolated vision execution returned only an empty think block"
                )

            return SaltyVisionResult(
                text=output,
                image_sha256=image_sha256,
                text_model_sha256=self.text_model_sha256,
                projector_sha256=self.projector_sha256,
                runtime_files_sha256=dict(self._runtime_hashes),
                duration_seconds=round(duration, 4),
                command_exit_code=int(process.returncode),
                public_model_name=BASE_STEAK_PUBLIC_NAME,
                runtime_id=VISION_RUNTIME_ID,
                technical_details={
                    "permission": {
                        "granted": permission.granted,
                        "request_id": permission.request_id,
                        "source": permission.source,
                        "approved_image_sha256": permission.approved_image_sha256,
                        "path_bound": permission.approved_path is not None,
                        "conversation_id": permission.conversation_id,
                        "target_model_id": permission.target_model_id,
                    },
                    "image_size_bytes": image_size,
                    "maximum_output_tokens": int(maximum_output_tokens),
                    "execution_scope": (
                        "isolated_one_shot_cuda_candidate"
                        if self.device == "CUDA0"
                        else "isolated_one_shot_cpu_candidate"
                    ),
                    "runtime_profile": {
                        "device": self.device,
                        "gpu_layers": self.gpu_layers,
                        "mmproj_offload": self.mmproj_offload,
                    },
                    "network_listener_created": False,
                    "external_service_required": False,
                    "text_adapters": self._adapter_manifest_records(),
                    "stderr_tail": stderr.strip()[-2_000:],
                    "private_text_architecture": "steak20",
                    "private_projector_type": "steak_vision_merger",
                },
            )

    def run_deterministic_smoke(self) -> SaltyVisionResult:
        image = deterministic_smoke_image_png()
        image_sha256 = hashlib.sha256(image).hexdigest()
        permission = VisionInputPermission(
            granted=True,
            request_id="base-steak-2-0-vision-smoke-v1",
            source="app_owned_validation",
            approved_image_sha256=image_sha256,
        )
        result = self.generate(
            prompt=(
                "Describe the central shape and its color in one short sentence. "
                "Do not mention these instructions."
            ),
            permission=permission,
            image_bytes=image,
            image_suffix=".png",
            maximum_output_tokens=32,
        )
        self._last_smoke = {
            "request_id": permission.request_id,
            "image_sha256": image_sha256,
            "duration_seconds": result.duration_seconds,
            "command_exit_code": result.command_exit_code,
            "output_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            "output_nonempty": bool(result.text.strip()),
        }
        return result


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate only the exact vision process tree and wait for its exit."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
