from collections import Counter

from app.backend.training.identity_subroute import (
    IDENTITY_SUBROUTE_CODES,
    identity_subroute_holdout_examples,
    identity_subroute_training_examples,
    normalise_identity_subroute,
)


def _conversation(example):
    return tuple((role, " ".join(text.casefold().split())) for role, text in example.messages)


def test_identity_subrouter_uses_eight_single_code_policies_with_full_context() -> None:
    training = identity_subroute_training_examples()
    holdout = identity_subroute_holdout_examples()
    counts = Counter(example.response for example in training)

    assert set(IDENTITY_SUBROUTE_CODES.values()) == set("ABCDEFGH")
    assert set(counts) == set(IDENTITY_SUBROUTE_CODES.values())
    assert min(counts.values()) >= 60
    assert all(example.messages[0][0] == "system" for example in [*training, *holdout])
    assert any(len(example.messages) >= 6 for example in training)
    assert {_conversation(example) for example in training}.isdisjoint(
        _conversation(example) for example in holdout
    )


def test_identity_subroute_normalization_fails_closed() -> None:
    assert normalise_identity_subroute("a.") == "A"
    assert normalise_identity_subroute(" H ") == "H"
    assert normalise_identity_subroute("IDENTITY") is None
    assert normalise_identity_subroute("") is None
