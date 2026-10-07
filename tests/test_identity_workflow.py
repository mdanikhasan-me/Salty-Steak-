import subprocess
import sys

import numpy as np

from app.backend.training.identity_workflow import (
    IDENTITY_TRACE_GPU_LAYERS,
    IDENTITY_TRAINING_SETTINGS,
    _expand_identity_initialization,
    _identity_training_gates,
)
from app.backend.training.native_identity import NativeIdentitySettings


def test_identity_workflow_import_keeps_native_loader_dependency_boundary_clean() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.backend.training.identity_workflow; "
                "from app.backend.training.identity_dialogue_dataset import "
                "training_examples, holdout_examples; "
                "training_examples(); holdout_examples(); "
                "assert not any(name in sys.modules for name in "
                "('numpy', 'gguf', 'torch'))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_identity_training_gate_contains_only_positive_pass_conditions() -> None:
    settings = NativeIdentitySettings(
        rank=8,
        epochs=20,
        minimum_epochs_before_early_stop=10,
    )
    expected = "a" * 64

    gates = _identity_training_gates(
        final={
            "adapted_train_accuracy": 1.0,
            "adapted_holdout_accuracy": 0.95,
            "retention_delta_max": 0.00001,
        },
        settings=settings,
        base_hash_before=expected,
        base_hash_after=expected,
        expected_hash=expected,
        base_size_before=123,
        base_size_after=123,
    )

    assert gates["hardcoded_response_absent"] is True
    assert "hardcoded_response_used" not in gates
    assert all(gates.values())


def test_identity_recipe_uses_one_full_capacity_learned_extension() -> None:
    assert IDENTITY_TRACE_GPU_LAYERS == 60
    assert IDENTITY_TRAINING_SETTINGS.rank == 512
    assert IDENTITY_TRAINING_SETTINGS.learning_rate == 0.003
    assert IDENTITY_TRAINING_SETTINGS.retention_weight == 20.0
    assert IDENTITY_TRAINING_SETTINGS.adapter_l2_weight == 0.00005
    assert IDENTITY_TRAINING_SETTINGS.top_k_negatives == 256
    assert IDENTITY_TRAINING_SETTINGS.sample_batch_size == 512
    assert IDENTITY_TRAINING_SETTINGS.epochs == 600


def test_identity_training_gate_fails_if_the_original_model_changes() -> None:
    settings = NativeIdentitySettings(
        rank=8,
        epochs=20,
        minimum_epochs_before_early_stop=10,
    )
    expected = "a" * 64

    gates = _identity_training_gates(
        final={
            "adapted_train_accuracy": 1.0,
            "adapted_holdout_accuracy": 0.95,
            "retention_delta_max": 0.00001,
        },
        settings=settings,
        base_hash_before=expected,
        base_hash_after="b" * 64,
        expected_hash=expected,
        base_size_before=123,
        base_size_after=123,
    )

    assert gates["base_model_unchanged"] is False
    assert not all(gates.values())


def test_smaller_learned_extension_expands_without_changing_its_output() -> None:
    settings = NativeIdentitySettings(
        rank=4,
        epochs=20,
        minimum_epochs_before_early_stop=10,
    )
    initial_a = np.arange(6, dtype=np.float32).reshape(2, 3)
    initial_b = np.arange(10, dtype=np.float32).reshape(5, 2)

    expanded_a, expanded_b, details = _expand_identity_initialization(
        initial_a,
        initial_b,
        settings=settings,
        hidden_size=3,
        vocabulary_size=5,
    )

    np.testing.assert_array_equal(expanded_a[:2], initial_a)
    np.testing.assert_array_equal(expanded_b[:, :2], initial_b)
    np.testing.assert_array_equal(expanded_b[:, 2:], 0.0)
    np.testing.assert_allclose(
        initial_b @ initial_a,
        expanded_b @ expanded_a,
    )
    assert details["source_rank"] == 2
    assert details["target_rank"] == 4
