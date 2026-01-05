"""Durable, app-owned image inputs for explicit Base Steak 2.0 analysis.

The HTTP upload and automation brokers may place bytes in temporary locations,
but the vision runtime only receives an immutable copy from this store.  Opaque
tokens are single-use.  An explicit retry mints a new token for the same
checksum-verified payload instead of trusting caller paths or resubmitted bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from ..runtime.salty_vision import ALLOWED_IMAGE_SUFFIXES, MAX_IMAGE_BYTES


VISION_INPUT_SCHEMA = "salty-steak-vision-input-v1"
VISION_INPUT_ID = re.compile(r"^[0-9a-f]{32}$")
VISION_TOKEN_SECRET = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPES = {
    ".bmp": "image/bmp",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(frozen=True, slots=True)
class VisionInputClaim:
    input_id: str
    image_path: Path
    image_sha256: str
    image_size_bytes: int
    media_type: str
    suffix: str
    source: str
    consent_evidence: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(dict(value), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _detected_media_type(path: Path) -> str:
    with path.open("rb") as stream:
        head = stream.read(16)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if head.startswith(b"BM"):
            return "image/bmp"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
    raise ValueError("The selected file is not a supported PNG, JPEG, WebP, or BMP image")


class VisionInputStore:
    """Own immutable image copies and rotate one-use opaque tokens."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._lock = threading.RLock()

    def stage(
        self,
        source_path: str | Path,
        *,
        filename: str,
        declared_media_type: str,
        source: str,
        consent_evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        if source not in {"user_attachment", "screen_capture"}:
            raise PermissionError("Vision input source is not app-approved")
        if consent_evidence.get("user_confirmed") is not True:
            raise PermissionError("Image analysis requires explicit user confirmation")
        candidate = Path(source_path).resolve(strict=True)
        if not candidate.is_file():
            raise ValueError("Vision input must be one local file")
        size = candidate.stat().st_size
        if not 1 <= size <= MAX_IMAGE_BYTES:
            raise ValueError("Image input must contain 1 byte to 25 MiB")
        suffix = Path(filename).suffix.casefold()
        if suffix not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError("Only PNG, JPEG, WebP, and BMP images are supported")
        detected = _detected_media_type(candidate)
        expected = MEDIA_TYPES[suffix]
        declared = str(declared_media_type).split(";", 1)[0].strip().casefold()
        if detected != expected or declared != detected:
            raise ValueError("Image filename, MIME type, and file signature do not match")

        identifier = secrets.token_hex(16)
        secret = secrets.token_hex(32)
        image_sha256 = _sha256_file(candidate)
        directory = self.root / identifier
        image_path = directory / f"input{suffix}"
        metadata_path = directory / "input.json"
        with self._lock:
            directory.mkdir(parents=True, exist_ok=False)
            try:
                with candidate.open("rb") as source_stream, image_path.open("xb") as target:
                    shutil.copyfileobj(source_stream, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                if _sha256_file(image_path) != image_sha256:
                    raise RuntimeError("The app-owned image copy failed its integrity check")
                now = _utc_now()
                metadata = {
                    "schema": VISION_INPUT_SCHEMA,
                    "input_id": identifier,
                    "image_filename": image_path.name,
                    "original_filename": Path(filename).name,
                    "media_type": detected,
                    "size_bytes": size,
                    "sha256": image_sha256,
                    "source": source,
                    "consent_evidence": dict(consent_evidence),
                    "token_sha256": hashlib.sha256(secret.encode("ascii")).hexdigest(),
                    "token_state": "available",
                    "analysis_state": "not_started",
                    "invocation_count": 0,
                    "operation_id": None,
                    "operation_history": [],
                    "conversation_id": None,
                    "target_model_id": None,
                    "prompt": None,
                    "created_at": now,
                    "updated_at": now,
                }
                _write_json_atomic(metadata_path, metadata)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        return {
            "vision_input_token": f"{identifier}.{secret}",
            "input_id": identifier,
            "filename": Path(filename).name,
            "media_type": detected,
            "size_bytes": size,
            "sha256": image_sha256,
            "source": source,
            "one_use_token": True,
            "app_owned_copy": True,
        }

    def claim(
        self,
        token: str,
        *,
        operation_id: str,
        conversation_id: str,
        target_model_id: str,
        prompt: str,
    ) -> VisionInputClaim:
        identifier, secret = self._parse_token(token)
        with self._lock:
            metadata_path, metadata = self._metadata(identifier)
            expected = str(metadata.get("token_sha256") or "")
            actual = hashlib.sha256(secret.encode("ascii")).hexdigest()
            if (
                metadata.get("token_state") != "available"
                or not expected
                or not hmac.compare_digest(expected, actual)
            ):
                raise PermissionError("Vision input token is invalid or has already been used")
            image_path = self._verified_image_path(metadata_path.parent, metadata)
            metadata.update(
                {
                    "token_sha256": None,
                    "token_state": "consumed",
                    "analysis_state": "running",
                    "invocation_count": int(metadata.get("invocation_count") or 0) + 1,
                    "operation_id": operation_id,
                    "operation_history": [
                        *list(metadata.get("operation_history") or []),
                        operation_id,
                    ],
                    "conversation_id": conversation_id,
                    "target_model_id": target_model_id,
                    "prompt": prompt,
                    "updated_at": _utc_now(),
                }
            )
            _write_json_atomic(metadata_path, metadata)
            return VisionInputClaim(
                input_id=identifier,
                image_path=image_path,
                image_sha256=str(metadata["sha256"]),
                image_size_bytes=int(metadata["size_bytes"]),
                media_type=str(metadata["media_type"]),
                suffix=image_path.suffix.casefold(),
                source=str(metadata["source"]),
                consent_evidence=dict(metadata.get("consent_evidence") or {}),
            )

    def finish(self, operation_id: str, state: str) -> None:
        if state not in {"completed", "failed", "interrupted"}:
            raise ValueError("Vision analysis state is invalid")
        with self._lock:
            metadata_path, metadata = self._metadata_for_operation(operation_id)
            metadata["analysis_state"] = state
            metadata["updated_at"] = _utc_now()
            _write_json_atomic(metadata_path, metadata)

    def issue_retry(self, operation_id: str) -> dict[str, Any]:
        """Mint a new one-use token for the unchanged prior image."""

        with self._lock:
            metadata_path, metadata = self._metadata_for_operation(operation_id)
            if metadata.get("analysis_state") == "running":
                raise ValueError("Vision analysis is still running")
            image_path = self._verified_image_path(metadata_path.parent, metadata)
            secret = secrets.token_hex(32)
            metadata.update(
                {
                    "token_sha256": hashlib.sha256(secret.encode("ascii")).hexdigest(),
                    "token_state": "available",
                    "analysis_state": "retry_available",
                    "operation_id": None,
                    "updated_at": _utc_now(),
                }
            )
            _write_json_atomic(metadata_path, metadata)
            return {
                "vision_input_token": f"{metadata['input_id']}.{secret}",
                "input_id": metadata["input_id"],
                "conversation_id": metadata["conversation_id"],
                "target_model_id": metadata["target_model_id"],
                "prompt": metadata["prompt"],
                "media_type": metadata["media_type"],
                "size_bytes": metadata["size_bytes"],
                "sha256": metadata["sha256"],
                "source": metadata["source"],
                "one_use_token": True,
                "immutable_retry_payload": image_path.is_file(),
            }

    def purge_conversation(self, conversation_id: str) -> int:
        """Remove every app-owned image associated with one deleted chat."""

        removed = 0
        with self._lock:
            if not self.root.is_dir():
                return 0
            for metadata_path in list(self.root.glob("*/input.json")):
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if metadata.get("conversation_id") == conversation_id:
                    shutil.rmtree(metadata_path.parent)
                    removed += 1
        return removed

    def purge_expired_unclaimed(self, *, maximum_age_seconds: int = 86_400) -> int:
        """Bound disk retention for uploads never submitted for analysis."""

        checked_age = int(maximum_age_seconds)
        if checked_age < 60:
            raise ValueError("Unclaimed image retention must be at least 60 seconds")
        cutoff = time.time() - checked_age
        removed = 0
        with self._lock:
            if not self.root.is_dir():
                return 0
            for metadata_path in list(self.root.glob("*/input.json")):
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if (
                    metadata.get("token_state") == "available"
                    and metadata.get("operation_id") is None
                    and metadata_path.stat().st_mtime < cutoff
                ):
                    shutil.rmtree(metadata_path.parent)
                    removed += 1
        return removed

    def _metadata_for_operation(self, operation_id: str) -> tuple[Path, dict[str, Any]]:
        if not self.root.is_dir():
            raise KeyError("Vision analysis input does not exist")
        matches: list[tuple[Path, dict[str, Any]]] = []
        for metadata_path in self.root.glob("*/input.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                metadata.get("operation_id") == operation_id
                or operation_id in set(metadata.get("operation_history") or [])
            ):
                matches.append((metadata_path, metadata))
        if len(matches) != 1:
            raise KeyError("Vision analysis input does not exist")
        return matches[0]

    def _metadata(self, identifier: str) -> tuple[Path, dict[str, Any]]:
        path = self.root / identifier / "input.json"
        if not path.is_file():
            raise PermissionError("Vision input token is invalid or expired")
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PermissionError("Vision input metadata is unreadable") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema") != VISION_INPUT_SCHEMA
            or metadata.get("input_id") != identifier
        ):
            raise PermissionError("Vision input metadata identity is invalid")
        return path, metadata

    @staticmethod
    def _verified_image_path(directory: Path, metadata: Mapping[str, Any]) -> Path:
        filename = str(metadata.get("image_filename") or "")
        image_path = (directory / filename).resolve()
        if directory.resolve() not in image_path.parents or not image_path.is_file():
            raise RuntimeError("The app-owned vision input is missing")
        if image_path.stat().st_size != int(metadata.get("size_bytes") or 0):
            raise RuntimeError("The app-owned vision input size changed")
        if _sha256_file(image_path) != metadata.get("sha256"):
            raise RuntimeError("The app-owned vision input checksum changed")
        return image_path

    @staticmethod
    def _parse_token(token: str) -> tuple[str, str]:
        pieces = str(token).strip().split(".")
        if (
            len(pieces) != 2
            or not VISION_INPUT_ID.fullmatch(pieces[0])
            or not VISION_TOKEN_SECRET.fullmatch(pieces[1])
        ):
            raise PermissionError("Vision input token is invalid")
        return pieces[0], pieces[1]
