import numpy as np

from app.backend.training.identity_specialists import IDENTITY_SPECIALIST_POLICIES
from app.backend.training.identity_subroute_classifier import (
    IDENTITY_SUBROUTE_FEATURE_DIMENSION,
    IdentitySubrouteLinearClassifier,
    identity_subroute_features,
)


def test_identity_subroute_features_are_deterministic_and_use_context() -> None:
    first = (("user", "Who trained you?"), ("assistant", "A trainer answer."), ("user", "I need more."))
    second = (("user", "What model is this?"), ("assistant", "A model answer."), ("user", "I need more."))

    first_indices, first_values = identity_subroute_features(first)
    repeat_indices, repeat_values = identity_subroute_features(first)
    second_indices, second_values = identity_subroute_features(second)

    assert np.array_equal(first_indices, repeat_indices)
    assert np.array_equal(first_values, repeat_values)
    assert not (
        np.array_equal(first_indices, second_indices)
        and np.array_equal(first_values, second_values)
    )
    assert np.isclose(np.linalg.norm(first_values), 1.0)


def test_identity_subroute_classifier_loads_without_pickle(tmp_path) -> None:
    weights = np.zeros(
        (len(IDENTITY_SPECIALIST_POLICIES), IDENTITY_SUBROUTE_FEATURE_DIMENSION),
        dtype=np.float32,
    )
    bias = np.zeros(len(IDENTITY_SPECIALIST_POLICIES), dtype=np.float32)
    bias[IDENTITY_SPECIALIST_POLICIES.index("boundary")] = 1.0
    path = tmp_path / "classifier.npz"
    np.savez_compressed(path, weights=weights, bias=bias)

    classifier = IdentitySubrouteLinearClassifier.load(path)

    assert classifier.predict((("user", "anything"),)) == "boundary"
