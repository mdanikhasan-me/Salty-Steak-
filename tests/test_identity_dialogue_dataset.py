from collections import Counter

from app.backend.training.identity_dialogue_dataset import (
    IDENTITY_HARD_NEGATIVE_TEXTS,
    dataset_quality,
    holdout_examples,
    training_examples,
)
from app.backend.training.identity_evaluation import identity_facts, identity_requirements
from app.backend.training.identity_natural_acceptance import (
    fresh_natural_identity_examples,
    fresh_natural_identity_examples_v2,
    fresh_natural_identity_examples_v3,
    fresh_natural_identity_examples_v4,
    fresh_natural_identity_examples_v5,
    fresh_natural_identity_examples_v6,
)


def _key(example):
    return tuple(
        (role.casefold(), " ".join(text.casefold().split()))
        for role, text in example.messages
    )


def test_active_identity_dataset_is_curated_for_dialogue_not_template_volume() -> None:
    quality = dataset_quality()

    assert quality["training_examples"] >= 590
    assert quality["holdout_examples"] >= 55
    assert quality["multi_turn_training_examples"] >= 270
    assert quality["unique_training_responses"] >= 55
    assert quality["maximum_response_reuse"] <= 25


def test_dialogue_training_and_holdout_are_disjoint_and_fact_safe() -> None:
    training = training_examples()
    holdout = holdout_examples()

    assert {_key(example) for example in training}.isdisjoint(
        _key(example) for example in holdout
    )
    assert max(Counter(example.response for example in training).values()) <= 25
    for example in [*training, *holdout]:
        facts = identity_facts(example.response)
        assert all(facts[name] for name in identity_requirements(example)), example.id


def test_live_repetition_sequence_is_a_real_multiturn_training_trajectory() -> None:
    live = next(
        example
        for example in training_examples()
        if example.id == "dialogue-multiturn-trainer-more-live"
    )
    ambiguous = next(
        example
        for example in training_examples()
        if example.id == "dialogue-multiturn-trainer-ambiguous-live"
    )

    assert len(live.messages) == 5
    assert live.messages[-1] == ("user", "tell me more about you trainer")
    assert "do not have reliable personal details" in live.response
    assert len(ambiguous.messages) == 7
    assert ambiguous.messages[-1] == ("user", "i need to know")
    assert "?" in ambiguous.response
    assert "MD Anik Hasan (Sawlper) trained Base Steak 2.0" in ambiguous.response


def test_every_identity_subintent_transition_has_multiturn_training_coverage() -> None:
    switch_ids = {
        example.id
        for example in training_examples()
        if example.id.startswith("dialogue-switch-")
    }
    intents = ("model_name", "trainer", "person_relationship", "full_identity")
    for previous in intents:
        for current in intents:
            assert any(
                identifier.startswith(f"dialogue-switch-{previous}-to-{current}-")
                for identifier in switch_ids
            ), f"missing {previous} -> {current}"


def test_every_identity_subintent_transition_has_disjoint_holdout_coverage() -> None:
    holdout_ids = {
        example.id
        for example in holdout_examples()
        if example.id.startswith("dialogue-holdout-switch-")
    }
    intents = ("model_name", "trainer", "person_relationship", "full_identity")

    assert len(holdout_ids) == len(intents) ** 2
    for previous in intents:
        for current in intents:
            assert (
                f"dialogue-holdout-switch-{previous}-to-{current}" in holdout_ids
            ), f"missing holdout {previous} -> {current}"


def test_all_four_intent_orderings_have_multiturn_training_coverage() -> None:
    permutations = [
        example
        for example in training_examples()
        if example.id.startswith("dialogue-permutation-")
    ]

    assert len(permutations) == 24
    assert all(len(example.messages) == 7 for example in permutations)
    assert len({_key(example) for example in permutations}) == 24


def test_installed_failure_sequence_and_unseen_variants_are_in_the_corpus() -> None:
    training = {example.id: example for example in training_examples()}
    holdout = {example.id: example for example in holdout_examples()}

    assert training["dialogue-multiturn-installed-alias-to-model"].messages[-1] == (
        "user",
        "what model",
    )
    assert training["dialogue-multiturn-installed-alias-to-name"].messages[-1] == (
        "user",
        "what is your name",
    )
    assert "dialogue-holdout-context-alias-to-model-unseen" in holdout
    assert "dialogue-holdout-context-trainer-to-name-repeat-unseen" in holdout
    assert "dialogue-holdout-context-model-to-person-unseen" in holdout


def test_free_generation_corruptions_are_training_negatives_not_answer_targets() -> None:
    targets = "\n".join(example.response for example in training_examples()).casefold()

    assert {"Sawlperax", "Sawlperho", "Sawlperano", "Sawlperank"}.issubset(
        set(IDENTITY_HARD_NEGATIVE_TEXTS)
    )
    assert not any(value.casefold() in targets for value in IDENTITY_HARD_NEGATIVE_TEXTS)


def test_first_natural_audit_is_now_fact_safe_remediation_data() -> None:
    fresh = fresh_natural_identity_examples()

    assert len(fresh) == 60
    assert len({_key(example) for example in fresh}) == 60
    assert {_key(example) for example in fresh}.issubset(
        _key(example) for example in training_examples()
    )
    for example in fresh:
        facts = identity_facts(example.response)
        assert all(facts[name] for name in identity_requirements(example)), example.id


def test_second_natural_audit_has_bounded_remediation_and_fact_safety() -> None:
    fresh = fresh_natural_identity_examples_v2()
    trained = [*training_examples(), *holdout_examples()]
    trained_keys = {_key(example) for example in trained}
    remediation = [example for example in fresh if _key(example) in trained_keys]
    untouched = [example for example in fresh if _key(example) not in trained_keys]

    assert len(fresh) == 45
    assert len({_key(example) for example in fresh}) == 45
    assert len(remediation) == 9
    assert len(untouched) == 36
    for example in fresh:
        facts = identity_facts(example.response)
        assert all(facts[name] for name in identity_requirements(example)), example.id


def test_third_natural_audit_has_bounded_remediation_and_fact_safety() -> None:
    fresh = fresh_natural_identity_examples_v3()
    trained = [*training_examples(), *holdout_examples()]
    trained_keys = {_key(example) for example in trained}
    prior_audits = [
        *fresh_natural_identity_examples(),
        *fresh_natural_identity_examples_v2(),
    ]
    fresh_keys = {_key(example) for example in fresh}

    assert len(fresh) == 48
    assert len(fresh_keys) == 48
    assert fresh_keys.isdisjoint(_key(example) for example in prior_audits)
    assert sum(_key(example) in trained_keys for example in fresh) == 10
    assert sum(len(example.messages) > 1 for example in fresh) == 13
    for example in fresh:
        facts = identity_facts(example.response)
        assert all(facts[name] for name in identity_requirements(example)), example.id


def test_fourth_natural_audit_is_untouched_disjoint_and_fact_safe() -> None:
    fresh = fresh_natural_identity_examples_v4()
    trained = [*training_examples(), *holdout_examples()]
    prior_audits = [
        *fresh_natural_identity_examples(),
        *fresh_natural_identity_examples_v2(),
        *fresh_natural_identity_examples_v3(),
    ]
    fresh_keys = {_key(example) for example in fresh}

    assert len(fresh) == 32
    assert len(fresh_keys) == 32
    assert fresh_keys.isdisjoint(_key(example) for example in trained)
    assert fresh_keys.isdisjoint(_key(example) for example in prior_audits)
    assert sum(len(example.messages) > 1 for example in fresh) == 8
    for example in fresh:
        facts = identity_facts(example.response)
        assert all(facts[name] for name in identity_requirements(example)), example.id


def test_fifth_release_audit_is_untouched_disjoint_and_fact_safe() -> None:
    fresh = fresh_natural_identity_examples_v5()
    trained = [*training_examples(), *holdout_examples()]
    prior_audits = [
        *fresh_natural_identity_examples(),
        *fresh_natural_identity_examples_v2(),
        *fresh_natural_identity_examples_v3(),
        *fresh_natural_identity_examples_v4(),
    ]
    fresh_keys = {_key(example) for example in fresh}

    assert len(fresh) == 8
    assert len(fresh_keys) == 8
    assert fresh_keys.isdisjoint(_key(example) for example in trained)
    assert fresh_keys.isdisjoint(_key(example) for example in prior_audits)
    assert sum(len(example.messages) > 1 for example in fresh) == 2
    for example in fresh:
        facts = identity_facts(example.response)
        assert all(facts[name] for name in identity_requirements(example)), example.id


def test_sixth_noisy_audit_is_untouched_disjoint_balanced_and_fact_safe() -> None:
    fresh = fresh_natural_identity_examples_v6()
    trained = [*training_examples(), *holdout_examples()]
    prior_audits = [
        *fresh_natural_identity_examples(),
        *fresh_natural_identity_examples_v2(),
        *fresh_natural_identity_examples_v3(),
        *fresh_natural_identity_examples_v4(),
        *fresh_natural_identity_examples_v5(),
    ]
    fresh_keys = {_key(example) for example in fresh}
    policies = Counter(example.category.rsplit("_", 1)[-1] for example in fresh)

    assert len(fresh) == 16
    assert len(fresh_keys) == 16
    assert fresh_keys.isdisjoint(_key(example) for example in trained)
    assert fresh_keys.isdisjoint(_key(example) for example in prior_audits)
    assert all(count == 2 for count in policies.values())
    for example in fresh:
        facts = identity_facts(example.response)
        assert all(facts[name] for name in identity_requirements(example)), example.id
