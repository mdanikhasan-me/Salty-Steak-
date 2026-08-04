"""App-owned registry for role-specific local model bundles.

The registry intentionally keeps imported Salty Native models separate from trainable
Salty SafeTensors checkpoints.  Selection is not activation: a bundle can be
the preferred Chat base while its execution kernel remains fail-closed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


MODEL_BUNDLE_SCHEMA = "salty-steak-model-bundle-v1"
SUPPORTED_ROLES = {
    "text_generation",
    "vision_language",
    "image_generation",
    "speech_recognition",
    "speech_generation",
    "embedding",
    "reranking",
}
_COMPANION_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_COMPANION_STATE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
VERIFIED_INTEGRITY_STATES = frozenset(
    {
        "sha256_verified_at_import",
        "sha256_and_tensor_payload_verified_at_rebuild",
    }
)


class ModelBundleError(ValueError):
    """Raised when a bundle manifest is unsafe or inconsistent."""


class ModelBundleRegistry:
    """Discover immutable model artifacts under the workspace model library."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def list(self, role: str | None = None) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for manifest_path in sorted(self.root.rglob("model.json")):
            try:
                record = self._read(manifest_path)
            except (OSError, json.JSONDecodeError, ModelBundleError):
                continue
            if role is None or record["model_role"] == role:
                records.append(record)
        return sorted(
            records,
            key=lambda item: (
                not bool(item.get("selected_as_base")),
                str(item["display_name"]).casefold(),
            ),
        )

    def selected_base(self, role: str = "text_generation") -> dict[str, Any] | None:
        return next(
            (item for item in self.list(role) if item.get("selected_as_base")),
            None,
        )

    def get(self, model_id: str) -> dict[str, Any]:
        match = next((item for item in self.list() if item["id"] == model_id), None)
        if match is None:
            raise KeyError(f"Model bundle does not exist: {model_id}")
        return match

    def _read_companion_artifacts(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if "companion_artifacts" not in manifest:
            return []
        declared = manifest["companion_artifacts"]
        if not isinstance(declared, list):
            raise ModelBundleError("Model bundle companion_artifacts must be a list")

        bundle_root = manifest_path.resolve().parent
        try:
            bundle_root.relative_to(self.root)
        except ValueError as exc:
            raise ModelBundleError("Model bundle manifest escapes the model library") from exc

        records: list[dict[str, Any]] = []
        identifiers: set[str] = set()
        filenames: set[str] = set()
        for index, companion in enumerate(declared):
            if not isinstance(companion, dict):
                raise ModelBundleError(
                    f"Companion artifact {index} must be an object"
                )

            identifier = _required_companion_text(companion, "id", index)
            display_name = _required_companion_text(
                companion,
                "display_name",
                index,
            )
            role = _required_companion_text(companion, "role", index)
            state = _required_companion_text(companion, "state", index)
            if not _COMPANION_IDENTIFIER_PATTERN.fullmatch(identifier):
                raise ModelBundleError(
                    f"Companion artifact {index} id is invalid"
                )
            if not _COMPANION_STATE_PATTERN.fullmatch(role):
                raise ModelBundleError(
                    f"Companion artifact {index} role is invalid"
                )
            if not _COMPANION_STATE_PATTERN.fullmatch(state):
                raise ModelBundleError(
                    f"Companion artifact {index} state is invalid"
                )
            if identifier in identifiers:
                raise ModelBundleError(
                    f"Companion artifact id is duplicated: {identifier}"
                )
            identifiers.add(identifier)

            filename = _required_companion_text(companion, "filename", index)
            path_fragment = Path(filename)
            if (
                filename in {".", ".."}
                or path_fragment.is_absolute()
                or bool(path_fragment.drive)
                or path_fragment.name != filename
                or ":" in filename
            ):
                raise ModelBundleError(
                    f"Companion artifact {index} filename must be local to its bundle"
                )
            if filename.casefold() in filenames:
                raise ModelBundleError(
                    f"Companion artifact filename is duplicated: {filename}"
                )
            filenames.add(filename.casefold())

            companion_path = (bundle_root / filename).resolve()
            try:
                companion_path.relative_to(bundle_root)
            except ValueError as exc:
                raise ModelBundleError(
                    f"Companion artifact {index} escapes its bundle"
                ) from exc

            expected_size_value = companion.get("size_bytes")
            if (
                isinstance(expected_size_value, bool)
                or not isinstance(expected_size_value, int)
                or expected_size_value <= 0
            ):
                raise ModelBundleError(
                    f"Companion artifact {index} size_bytes must be a positive integer"
                )
            expected_size = expected_size_value

            checksum = _required_companion_text(companion, "sha256", index)
            if not _SHA256_PATTERN.fullmatch(checksum):
                raise ModelBundleError(
                    f"Companion artifact {index} sha256 must be 64 hexadecimal characters"
                )

            reason_value = companion.get("reason", "")
            if not isinstance(reason_value, str):
                raise ModelBundleError(
                    f"Companion artifact {index} reason must be text"
                )
            scale_value = companion.get("scale", 1.0)
            if (
                isinstance(scale_value, bool)
                or not isinstance(scale_value, (int, float))
                or not 0 < float(scale_value) <= 16
            ):
                raise ModelBundleError(
                    f"Companion artifact {index} scale must be between 0 and 16"
                )
            activation = str(companion.get("activation") or "always").strip().casefold()
            if activation not in {"always", "identity_intent", "routing_intent"}:
                raise ModelBundleError(
                    f"Companion artifact {index} activation must be always, identity_intent, or routing_intent"
                )
            if activation == "identity_intent" and role != "text_adapter":
                raise ModelBundleError(
                    f"Companion artifact {index} identity activation requires text_adapter role"
                )
            if activation == "routing_intent" and role != "routing_adapter":
                raise ModelBundleError(
                    f"Companion artifact {index} routing activation requires routing_adapter role"
                )
            current_size = (
                companion_path.stat().st_size if companion_path.is_file() else None
            )
            size_matches = current_size == expected_size
            file_state = (
                "present_size_matches"
                if size_matches
                else "present_size_mismatch"
                if current_size is not None
                else "missing"
            )
            records.append(
                {
                    "id": identifier,
                    "display_name": display_name,
                    "role": role,
                    "state": state,
                    "reason": reason_value.strip(),
                    "scale": float(scale_value),
                    "activation": activation,
                    "filename": filename,
                    "artifact_path": str(companion_path),
                    "format": str(companion.get("format") or "unknown"),
                    "quantization": str(
                        companion.get("quantization") or "unknown"
                    ),
                    "expected_size_bytes": expected_size,
                    "artifact_size_bytes": current_size,
                    "current_size_matches": size_matches,
                    "file_state": file_state,
                    "checksum": checksum.lower(),
                    "checksum_algorithm": "sha256",
                    "checksum_metadata_valid": True,
                    "checksum_verification": "declared_not_reverified",


                    "runtime_available": False,
                    "activation_allowed": False,
                    "auto_activation_allowed": False,
                    "runtime_loaded": False,
                }
            )
        return records

    def _read(self, manifest_path: Path) -> dict[str, Any]:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != MODEL_BUNDLE_SCHEMA:
            raise ModelBundleError("Unsupported model bundle schema")

        identifier = str(manifest.get("id") or "").strip()
        display_name = str(manifest.get("display_name") or "").strip()
        role = str(manifest.get("role") or "").strip()
        if not identifier or not display_name or role not in SUPPORTED_ROLES:
            raise ModelBundleError("Model bundle identity or role is invalid")

        artifact = manifest.get("artifact")
        if not isinstance(artifact, dict):
            raise ModelBundleError("Model bundle artifact is missing")
        filename = str(artifact.get("filename") or "").strip()
        if not filename or Path(filename).name != filename:
            raise ModelBundleError("Model artifact filename must be local to its bundle")
        model_path = (manifest_path.parent / filename).resolve()
        try:
            model_path.relative_to(self.root)
        except ValueError as exc:
            raise ModelBundleError("Model artifact escapes the model library") from exc

        expected_size = int(artifact.get("size_bytes") or 0)
        current_size = model_path.stat().st_size if model_path.is_file() else None
        size_matches = current_size == expected_size
        runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
        identity = manifest.get("identity") if isinstance(manifest.get("identity"), dict) else {}
        routing = manifest.get("routing") if isinstance(manifest.get("routing"), dict) else {}
        context = manifest.get("context") if isinstance(manifest.get("context"), dict) else {}
        chat = manifest.get("chat") if isinstance(manifest.get("chat"), dict) else {}
        integrity = manifest.get("integrity") if isinstance(manifest.get("integrity"), dict) else {}
        runtime_profile = (
            dict(runtime["profile"])
            if isinstance(runtime.get("profile"), dict)
            else {}
        )
        context_presets = [
            int(value)
            for value in context.get("presets", [])
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        activation_allowed = bool(runtime.get("activation_allowed")) and size_matches
        companion_artifacts = self._read_companion_artifacts(manifest_path, manifest)

        return {
            "id": identifier,
            "display_name": display_name,
            "friendly_name": display_name,
            "architecture": str(manifest.get("architecture") or "unknown"),
            "public_architecture_name": str(
                identity.get("public_architecture_name")
                or manifest.get("architecture")
                or "Unknown"
            ),
            "model_role": role,
            "model_role_label": "Language" if role == "text_generation" else role.replace("_", " ").title(),
            "input_modalities": list(manifest.get("input_modalities") or []),
            "output_modalities": list(manifest.get("output_modalities") or []),
            "library_kind": "model_bundle",
            "model_format": str(artifact.get("format") or "unknown"),
            "quantization": str(artifact.get("quantization") or "unknown"),
            "model_path": str(model_path),
            "checkpoint_path": str(model_path.parent),
            "artifact_size_bytes": current_size,
            "expected_size_bytes": expected_size,
            "checksum": str(artifact.get("sha256") or ""),
            "integrity": (
                "verified"
                if size_matches and integrity.get("state") in VERIFIED_INTEGRITY_STATES
                else "needs_attention"
            ),
            "integrity_scope": str(integrity.get("state") or "not_verified"),
            "current_size_matches": size_matches,
            "architectural_context_tokens": int(context.get("architectural_tokens") or 0),
            "configured_context_tokens": int(context.get("configured_tokens") or 0),
            "default_context_tokens": int(
                context.get("default_tokens")
                or context.get("configured_tokens")
                or 0
            ),
            "context_presets": context_presets,
            "context_activation_state": str(context.get("activation_state") or "not_configured"),
            "context_note": str(context.get("activation_note") or ""),
            "runtime_family": str(runtime.get("family") or "unknown"),
            "runtime_engine": str(runtime.get("engine") or "unknown"),
            "runtime_profile": runtime_profile,
            "runtime_state": str(runtime.get("state") or "not_configured"),
            "runtime_reason": str(runtime.get("reason") or ""),
            "identity_adapter_required": bool(
                identity.get("identity_adapter_required")
            ),
            "identity_trainer": str(identity.get("trainer") or "").strip(),
            "identity_adapter_scale": (
                float(identity["identity_adapter_scale"])
                if isinstance(identity.get("identity_adapter_scale"), (int, float))
                and not isinstance(identity.get("identity_adapter_scale"), bool)
                else None
            ),
            "identity_training_report_sha256": str(
                identity.get("identity_training_report_sha256") or ""
            ).casefold(),
            "identity_free_generation_report_sha256": str(
                identity.get("identity_free_generation_report_sha256") or ""
            ).casefold(),
            "identity_evaluation_metrics": (
                dict(identity["identity_evaluation_metrics"])
                if isinstance(identity.get("identity_evaluation_metrics"), dict)
                else {}
            ),
            "routing_adapter_required": bool(routing.get("adapter_required")),
            "routing_adapter_activation": str(
                routing.get("adapter_activation") or ""
            ).strip(),
            "routing_post_training_report_sha256": str(
                routing.get("post_training_report_sha256") or ""
            ).casefold(),
            "routing_training_example_count": int(
                routing.get("training_example_count") or 0
            ),
            "routing_holdout_count": int(routing.get("holdout_count") or 0),
            "routing_holdout_pass_count": int(
                routing.get("holdout_pass_count") or 0
            ),
            "routing_per_route_pass_count": (
                dict(routing["per_route_pass_count"])
                if isinstance(routing.get("per_route_pass_count"), dict)
                else {}
            ),
            "activation_allowed": activation_allowed,
            "external_service_required": bool(runtime.get("external_service_required", True)),
            "selected_as_base": bool(chat.get("base_model") and chat.get("selected")),
            "runtime_loaded": bool(chat.get("active")) and activation_allowed,
            "reasoning_default": str(
                chat.get("reasoning_default") or ""
            ).strip().casefold(),
            "companion_artifact_count": len(companion_artifacts),
            "companion_artifacts": companion_artifacts,
            "registered_at": manifest.get("registered_at"),
            "available_actions": {
                "use_in_chat": {




                    "enabled": False,
                    "reason": (
                        "This local model bundle is selected only through the verified "
                        "model-bundle cutover workflow; it cannot be switched "
                        "with the checkpoint activation command."
                    ),
                },
                "continue_training": {"enabled": False, "reason": "Imported model bundles are inference artifacts"},
                "evaluate": {"enabled": False, "reason": "Evaluation requires a verified native runtime"},
            },
            "technical_details": {
                "manifest_path": str(manifest_path),
                "model_path": str(model_path),
                "checksum": str(artifact.get("sha256") or ""),
                "format": str(artifact.get("format") or "unknown"),
                "quantization": str(artifact.get("quantization") or "unknown"),
                "architecture": str(manifest.get("architecture") or "unknown"),
                "public_architecture_name": str(
                    identity.get("public_architecture_name")
                    or manifest.get("architecture")
                    or "Unknown"
                ),
                "architectural_context_tokens": int(context.get("architectural_tokens") or 0),
                "configured_context_tokens": int(context.get("configured_tokens") or 0),
                "default_context_tokens": int(
                    context.get("default_tokens")
                    or context.get("configured_tokens")
                    or 0
                ),
                "context_presets": context_presets,
                "runtime_family": str(runtime.get("family") or "unknown"),
                "runtime_state": str(runtime.get("state") or "not_configured"),
                "identity_adapter_required": bool(
                    identity.get("identity_adapter_required")
                ),
                "identity_trainer": str(identity.get("trainer") or "").strip(),
                "identity_adapter_scale": (
                    float(identity["identity_adapter_scale"])
                    if isinstance(identity.get("identity_adapter_scale"), (int, float))
                    and not isinstance(identity.get("identity_adapter_scale"), bool)
                    else None
                ),
                "identity_training_report_sha256": str(
                    identity.get("identity_training_report_sha256") or ""
                ).casefold(),
                "identity_free_generation_report_sha256": str(
                    identity.get("identity_free_generation_report_sha256") or ""
                ).casefold(),
                "identity_evaluation_metrics": (
                    dict(identity["identity_evaluation_metrics"])
                    if isinstance(identity.get("identity_evaluation_metrics"), dict)
                    else {}
                ),
                "routing": dict(routing),
                "runtime_profile": runtime_profile,
                "external_service_required": bool(runtime.get("external_service_required", True)),
                "reasoning_default": str(
                    chat.get("reasoning_default") or ""
                ).strip().casefold(),
                "companion_artifacts": companion_artifacts,
            },
        }


def _required_companion_text(
    companion: dict[str, Any],
    field: str,
    index: int,
) -> str:
    value = companion.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ModelBundleError(
            f"Companion artifact {index} {field} must be non-empty text"
        )
    return value.strip()
