from __future__ import annotations

import pytest

from app.backend.runtime.model_roles import (
    MODEL_ROLE_SCHEMA,
    language_checkpoint_role,
    model_role_catalog,
)


def test_model_roles_are_unique_and_modality_directed() -> None:
    catalog = model_role_catalog({"text_generation": 2, "image_generation": 1})
    assert catalog["schema"] == MODEL_ROLE_SCHEMA
    assert catalog["routing_policy"] == "one_runtime_per_role"
    roles = catalog["roles"]
    assert len({role["id"] for role in roles}) == len(roles)
    assert all(role["input_modalities"] for role in roles)
    assert all(role["output_modalities"] for role in roles)


def test_only_an_implemented_registered_role_is_selectable() -> None:
    roles = {
        role["id"]: role
        for role in model_role_catalog(
            {"text_generation": 1, "image_generation": 1}
        )["roles"]
    }
    assert roles["text_generation"]["selectable"] is True
    assert roles["image_generation"]["registered_models"] == 1
    assert roles["image_generation"]["selectable"] is False


def test_existing_checkpoints_have_a_language_runtime_contract() -> None:
    role = language_checkpoint_role()
    assert role == {
        "model_role": "text_generation",
        "model_role_label": "Language",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "runtime_family": "salty_native_decoder",
        "activation_surface": "chat",
    }


def test_negative_registration_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        model_role_catalog({"text_generation": -1})
