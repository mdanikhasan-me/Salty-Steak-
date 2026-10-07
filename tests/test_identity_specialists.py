from collections import Counter

from app.backend.training.identity_dialogue_dataset import (
    holdout_examples,
    training_examples,
)
from app.backend.training.base_steak_identity_dataset import IdentityExample
from app.backend.training.identity_specialists import (
    IDENTITY_SPECIALIST_POLICIES,
    IDENTITY_SPECIALIST_RANKS,
    identity_specialist_adapter_id,
    identity_specialist_policy,
)


def test_every_specialist_has_training_and_unseen_holdout_coverage() -> None:
    training = Counter(identity_specialist_policy(row) for row in training_examples())
    holdout = Counter(identity_specialist_policy(row) for row in holdout_examples())

    assert set(training) == set(IDENTITY_SPECIALIST_POLICIES)
    assert set(holdout) == set(IDENTITY_SPECIALIST_POLICIES)
    assert min(training.values()) >= 60
    assert min(holdout.values()) >= 3


def test_specialist_capacity_is_additive_and_policy_scoped() -> None:
    assert IDENTITY_SPECIALIST_RANKS == {
        "model_name": 32,
        "trainer": 64,
        "relationship": 96,
        "full": 64,
        "boundary": 32,
        "research": 32,
        "clarify": 32,
        "correction": 32,
    }
    assert sum(IDENTITY_SPECIALIST_RANKS.values()) == 384
    assert len(
        {identity_specialist_adapter_id(policy) for policy in IDENTITY_SPECIALIST_POLICIES}
    ) == len(IDENTITY_SPECIALIST_POLICIES)


def test_noisy_full_identity_categories_stay_on_the_full_specialist() -> None:
    example = IdentityExample(
        id="noisy-full",
        category="holdout_full_noisy",
        messages=(("user", "trainer named but what model are u"),),
        response="I am Base Steak 2.0, trained by MD Anik Hasan (Sawlper).",
    )

    assert identity_specialist_policy(example) == "full"
