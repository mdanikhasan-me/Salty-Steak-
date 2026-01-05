"""Stable model-role contracts for routing independent local runtimes.

Training checkpoints remain language-model versions.  Other model families use
their own import, execution, and validation pipelines instead of being forced
through the language checkpoint table.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


MODEL_ROLE_SCHEMA = "salty-steak-model-roles-v1"

_ROLE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "text_generation",
        "label": "Language",
        "description": "Conversation, writing, coding, and reasoning.",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "runtime_family": "salty_native_decoder",
        "library_kind": "saved_version",
        "activation_surface": "chat",
        "implementation_state": "available",
    },
    {
        "id": "vision_language",
        "label": "Vision",
        "description": "Understand images together with text.",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "runtime_family": "vision_language",
        "library_kind": "model_bundle",
        "activation_surface": "chat",
        "implementation_state": "runtime_required",
    },
    {
        "id": "image_generation",
        "label": "Image generation",
        "description": "Create or edit images from local prompts and references.",
        "input_modalities": ["text", "image"],
        "output_modalities": ["image"],
        "runtime_family": "diffusion",
        "library_kind": "model_bundle",
        "activation_surface": "create",
        "implementation_state": "runtime_required",
    },
    {
        "id": "speech_recognition",
        "label": "Speech recognition",
        "description": "Transcribe local audio into text.",
        "input_modalities": ["audio"],
        "output_modalities": ["text"],
        "runtime_family": "speech_recognition",
        "library_kind": "model_bundle",
        "activation_surface": "transcribe",
        "implementation_state": "runtime_required",
    },
    {
        "id": "speech_generation",
        "label": "Speech generation",
        "description": "Generate local speech from text.",
        "input_modalities": ["text"],
        "output_modalities": ["audio"],
        "runtime_family": "speech_generation",
        "library_kind": "model_bundle",
        "activation_surface": "speak",
        "implementation_state": "runtime_required",
    },
    {
        "id": "embedding",
        "label": "Embeddings",
        "description": "Build private semantic search and memory indexes.",
        "input_modalities": ["text"],
        "output_modalities": ["vector"],
        "runtime_family": "embedding",
        "library_kind": "model_bundle",
        "activation_surface": "memory",
        "implementation_state": "runtime_required",
    },
    {
        "id": "reranking",
        "label": "Reranking",
        "description": "Improve local retrieval ordering before generation.",
        "input_modalities": ["text", "documents"],
        "output_modalities": ["scores"],
        "runtime_family": "reranking",
        "library_kind": "model_bundle",
        "activation_surface": "memory",
        "implementation_state": "runtime_required",
    },
)


def language_checkpoint_role() -> dict[str, Any]:
    """Return the role carried by the existing verified checkpoint format."""
    role = deepcopy(_ROLE_SPECS[0])
    role.pop("description", None)
    role.pop("implementation_state", None)
    return {
        "model_role": role["id"],
        "model_role_label": role["label"],
        "input_modalities": role["input_modalities"],
        "output_modalities": role["output_modalities"],
        "runtime_family": role["runtime_family"],
        "activation_surface": role["activation_surface"],
    }


def model_role_catalog(
    installed_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Return a deterministic catalog with measured registration counts."""
    counts = dict(installed_counts or {})
    roles: list[dict[str, Any]] = []
    for source in _ROLE_SPECS:
        role = deepcopy(source)
        count = int(counts.get(role["id"], 0))
        if count < 0:
            raise ValueError("installed model counts cannot be negative")
        role["registered_models"] = count
        role["selectable"] = bool(
            role["implementation_state"] == "available" and count > 0
        )
        roles.append(role)
    return {
        "schema": MODEL_ROLE_SCHEMA,
        "routing_policy": "one_runtime_per_role",
        "roles": roles,
    }
