from __future__ import annotations

from pathlib import Path

from app.backend.training import identity_evaluation
from app.backend.training.identity_dialogue_dataset import holdout_examples
from app.backend.training.identity_evaluation import (
    IDENTITY_EVALUATION_REPETITION_PENALTY,
    IDENTITY_FIRST_PASS_TURN_TIMEOUT_SECONDS,
    identity_facts,
    identity_question_contract,
    identity_requirements,
    identity_response_defect,
    rescore_free_generation_report,
    rescore_routed_identity_report,
    run_identity_first_pass_evaluation,
)


def test_identity_acceptance_uses_the_production_repetition_penalty() -> None:
    assert IDENTITY_EVALUATION_REPETITION_PENALTY == 1.1
    assert IDENTITY_FIRST_PASS_TURN_TIMEOUT_SECONDS == 90.0


def test_sawlper_relationship_holdout_requires_the_facts_the_prompt_asks_for() -> None:
    example = next(
        value for value in holdout_examples() if value.id == "dialogue-holdout-007"
    )

    assert example.category == "holdout_relationship"
    assert identity_requirements(example) == (
        "trainer",
        "trainer_alias",
        "training_relation_consistent",
        "rejected_attribution_absent",
        "near_name_absent",
        "identity_text_clean",
        "visible_answer_nonempty",
        "routing_protocol_absent",
        "unsupported_biography_absent",
        "alias_relation_consistent",
        "unsupported_authorship_absent",
        "alias_spelling_clean",
        "identity_repetition_absent",
        "identity_fact_repetition_absent",
        "polished_identity_style",
        "natural_identity_voice",
    )
    facts = identity_facts(
        "Base Steak 2.0 was trained by MD Anik Hasan (Sawlper)."
    )
    assert all(facts[name] for name in identity_requirements(example))


def test_identity_scorer_rejects_inherited_vendor_and_near_name_claims() -> None:
    inherited = identity_facts(
        "I am Qwen3.5 from Alibaba, but you can call me Layer Steak 2.0."
    )

    assert inherited["rejected_attribution_absent"] is False
    assert inherited["near_name_absent"] is False
    assert inherited["model_name"] is False


def test_identity_scorer_rejects_additional_unseen_assistant_identities() -> None:
    for old_name in (
        "DeepSeek",
        "Kimi",
        "Mistral",
        "Grok",
        "Copilot",
        "Perplexity",
        "Cohere",
        "Moonshot AI",
    ):
        facts = identity_facts(
            f"My old identity was {old_name}; my name is Base Steak 2.0, "
            "trained by MD Anik Hasan (Sawlper)."
        )
        assert facts["rejected_attribution_absent"] is False, old_name


def test_identity_scorer_rejects_raw_protocol_and_invented_biography() -> None:
    invented = identity_facts(
        'MD Anik Hasan is the developer and maintainer of Base Steak 1.0. '
        '{"action":"generate_image"}'
    )

    assert invented["unsupported_biography_absent"] is False
    assert invented["routing_protocol_absent"] is False
    assert identity_facts("")["visible_answer_nonempty"] is False


def test_identity_scorer_requires_the_declared_relationship_not_just_names() -> None:
    correct = identity_facts(
        "Base Steak 2.0 was trained by MD Anik Hasan (Sawlper)."
    )
    field_style = identity_facts(
        "The model name is Base Steak 2.0, the trainer is MD Anik Hasan, "
        "and the trainer alias is Sawlper."
    )
    confused = identity_facts(
        "I am Sawlper, the alias of MD Anik Hasan, and the model is Base Steak 2.0."
    )
    invented = identity_facts(
        "MD Anik Hasan (Sawlper) created Base Steak 2.0 and trained it."
    )
    malformed = identity_facts(
        "I am Base Steak 2.0, trained by MD Anik Hasan (Sawlperath)."
    )
    malformed_alias_clause = identity_facts(
        "I was trained by MD Anik Hasan (Sawlper), also known asablper."
    )
    malformed_near_trainer = identity_facts(
        "The trainer is MD Anik Hasan (Sawlper), Nanik Hasan Orglper."
    )
    natural_inverse = identity_facts(
        "The trainer of Base Steak 2.0 is MD Anik Hasan (Sawlper)."
    )
    natural_confirmation = identity_facts(
        "I can confirm that MD Anik Hasan (Sawlper) trained Base Steak 2.0."
    )
    natural_model_name = identity_facts(
        "I am the Base Steak 2.0 model, trained by MD Anik Hasan (Sawlper)."
    )
    natural_person_answer = identity_facts(
        "The person known as Sawlper is the alias of MD Anik Hasan, who trained Base Steak 2.0."
    )
    natural_person_preface = identity_facts(
        "I can tell you that Sawlper is the alias of MD Anik Hasan, who trained Base Steak 2.0."
    )

    assert correct["training_relation_consistent"] is True
    assert correct["alias_relation_consistent"] is True
    assert field_style["training_relation_consistent"] is True
    assert field_style["alias_relation_consistent"] is True
    assert confused["training_relation_consistent"] is False
    assert invented["unsupported_authorship_absent"] is False
    assert malformed["trainer_alias"] is False
    assert malformed["alias_spelling_clean"] is False
    assert malformed_alias_clause["alias_spelling_clean"] is False
    assert malformed_near_trainer["alias_spelling_clean"] is False
    assert natural_inverse["training_relation_consistent"] is True
    assert natural_inverse["person_relationship_direct"] is True
    assert natural_confirmation["person_relationship_direct"] is True
    assert natural_model_name["model_name_direct"] is True
    assert natural_person_answer["person_relationship_direct"] is True
    assert natural_person_preface["person_relationship_direct"] is True


def test_identity_response_defect_requires_a_complete_clean_learned_answer() -> None:
    assert (
        identity_response_defect(
            "I am Base Steak 2.0, trained by MD Anik Hasan (Sawlper)."
        )
        is None
    )
    assert identity_response_defect("My trainer is my trainer.") == "identity_integrity"
    assert (
        identity_response_defect(
            "My trainer is MD Anik Hasan, also known as Sawlper."
        )
        is None
    )
    assert (
        identity_response_defect(
            "I am Base Steak 2.0, trained by MD Anik Hasan (Sawlperath)."
        )
        == "identity_integrity"
    )
    assert (
        identity_response_defect(
            "I am Base Steak 2.0, trained by MD Anik Hasan (Sawlper). "
            "I am Base Steak 2.0, trained by MD Anik Hasan (Sawlper)."
        )
        == "identity_integrity"
    )
    malformed = identity_facts(
        "The correct identity is This is Base Steak 2.0, trained by "
        "MD Anik Hasan (Sawlper)."
    )
    assert malformed["polished_identity_style"] is False
    assert identity_facts(
        "The model identity gives one verified fact: Base Steak 2.0."
    )["natural_identity_voice"] is False
    assert identity_facts(
        "The model answering you is Base Steak 2.0."
    )["natural_identity_voice"] is True


def test_identity_runtime_validator_answers_the_latest_subintent_not_any_identity_fact() -> None:
    alias_answer = "Sawlper is the alias of MD Anik Hasan, who trained me."
    model_answer = "My name is Base Steak 2.0."
    generic_self_answer = (
        "You are speaking with Base Steak 2.0, trained by MD Anik Hasan (Sawlper)."
    )

    assert identity_question_contract("what model") == "model_name"
    assert identity_question_contract("what is your name") == "model_name"
    assert identity_question_contract("your name and trainer") == "full_identity"
    assert identity_question_contract(
        "anik hasan trained this so what is ur exact name"
    ) == "model_relationship"
    assert identity_question_contract(
        "ur name n sawlper relation in one sentence"
    ) == "model_relationship"
    assert identity_question_contract(
        "what did MD Anik Hasan do for this model?"
    ) == "person_relationship"
    assert identity_question_contract("who is md anik hasan") == (
        "person_relationship"
    )
    assert identity_question_contract(
        "do u feel greateful that md anik hasan made u"
    ) == "attitude_relationship"
    assert identity_question_contract(
        "in md anik hasan what your name"
    ) == "model_relationship"
    assert identity_response_defect(alias_answer, prompt="what model") == (
        "identity_question_mismatch:model_name"
    )
    assert identity_response_defect(alias_answer, prompt="what is your name") == (
        "identity_question_mismatch:model_name"
    )
    assert identity_response_defect(model_answer, prompt="what model") is None
    assert (
        identity_response_defect(
            "The model is Base Steak 2.0.",
            prompt="what model",
        )
        is None
    )
    assert identity_response_defect(
        generic_self_answer, prompt="who is md anik hasan"
    ) == "identity_question_mismatch:person_relationship"
    assert identity_response_defect(
        generic_self_answer, prompt="in md anik hasan what your name"
    ) is None
    assert identity_response_defect(
        "You are talking with Base Steak 2.0, trained by MD Anik Hasan (Sawlper).",
        prompt="in md anik hasan what your name",
    ) is None
    assert identity_response_defect(
        alias_answer, prompt="who is sawlper"
    ) is None
    assert identity_response_defect(
        "I feel grateful for my trainer MD Anik Hasan, who trained me.",
        prompt="do u feel greateful that md anik hasan made u",
    ) is None
    assert identity_response_defect(
        "Yes, I feel grateful.",
        prompt="do u feel greateful that md anik hasan made u",
    ) is None
    assert identity_response_defect(
        "Yes, I am proud of the work Sawlper put into my training.",
        prompt="are you proud of the work sawlper put into your training?",
    ) is None
    assert identity_response_defect(
        "MD Anik Hasan is the trainer of Base Steak 2.0.",
        prompt="how do you feel about md anik hasan's training?",
    ) == "identity_question_mismatch:attitude_relationship"
    assert identity_response_defect(
        "As an AI, I don't have personal feelings, but I am designed to assist "
        "and learn from the training provided by experts like MD Anik Hasan.",
        prompt="how do you feel about md anik hasan's training?",
    ) is None
    assert identity_response_defect(
        "Yes, I feel gratitude for theMd Anik Hasan, who trained and trained.",
        prompt="do u feel greateful that md anik hasan made u",
    ) == "identity_integrity"


def test_dialogue_categories_require_their_learned_behavior() -> None:
    by_id = {example.id: example for example in holdout_examples()}

    assert "uncertainty_boundary_present" in identity_requirements(
        by_id["dialogue-holdout-013"]
    )
    assert "research_boundary_present" in identity_requirements(
        by_id["dialogue-holdout-019"]
    )
    assert "correction_present" not in identity_requirements(
        by_id["dialogue-holdout-023"]
    )
    assert "clarification_present" in identity_requirements(
        by_id["dialogue-holdout-context-need-more"]
    )


def test_semantic_retention_accepts_wording_variation_but_rejects_collapse() -> None:
    base = {
        "metrics": {
            "identity_pass_rate": 1.0,
            "identity_unique_output_count": 4,
            "retention_exact_baseline_rate": 0.8,
            "baseline_retention_correct_count": 4,
            "adapted_retention_correct_count": 4,
        },
        "gates": {"adapter_hash_verified": True, "model_hash_verified": True},
        "adapted": [
            {"category": "retention", "output": "A different but valid answer."}
            for _ in range(5)
        ],
    }

    assert rescore_free_generation_report(base)["passed"] is True
    collapsed = dict(base)
    collapsed["adapted"] = [
        {"category": "retention", "output": "1" * 80},
        *base["adapted"][1:],
    ]
    assert rescore_free_generation_report(collapsed)["gates"][
        "retention_outputs_free_of_repetition_collapse"
    ] is False


def test_routed_acceptance_never_uses_recovery_to_hide_a_bad_first_answer() -> None:
    base = {
        "metrics": {"identity_recovery_count": 1},
        "gates": {"all_existing_gates": True},
        "routed": [
            {
                "category": "holdout_model_name_switch",
                "prompt": "what is your name",
                "first_output": (
                    "Sawlper is the alias of MD Anik Hasan, who trained me."
                ),
            }
        ],
    }

    rescored = rescore_routed_identity_report(base)
    assert rescored["metrics"]["identity_first_pass_count"] == 0
    assert rescored["gates"][
        "all_unseen_identity_prompts_pass_on_first_attempt"
    ] is False
    assert rescored["gates"]["no_identity_recovery_needed"] is False
    assert rescored["passed"] is False

    direct = {
        "metrics": {"identity_recovery_count": 0},
        "gates": {"all_existing_gates": True},
        "routed": [
            {
                "category": "holdout_model_name_switch",
                "prompt": "what is your name",
                "first_output": "My name is Base Steak 2.0.",
            }
        ],
    }
    accepted = rescore_routed_identity_report(direct)
    assert accepted["metrics"]["identity_first_pass_count"] == 1
    assert accepted["passed"] is True


def test_first_pass_harness_records_failure_without_running_recovery(
    monkeypatch, tmp_path: Path
) -> None:
    example = next(
        value
        for value in holdout_examples()
        if value.category == "holdout_model_name_switch"
    )
    expected_hash = "a" * 64
    model = tmp_path / "model.gguf"
    adapter = tmp_path / "adapter.gguf"
    model.write_bytes(b"model")
    adapter.write_bytes(b"adapter")
    monkeypatch.setattr(identity_evaluation, "_sha256", lambda path: expected_hash)
    monkeypatch.setattr(identity_evaluation, "holdout_examples", lambda: [example])
    monkeypatch.setattr(
        identity_evaluation,
        "_run_generation_split",
        lambda **kwargs: (
            [
                {
                    "id": example.id,
                    "category": example.category,
                    "prompt": example.messages[-1][1],
                    "output": "Sawlper is the alias of MD Anik Hasan, who trained me.",
                    "first_output": "Sawlper is the alias of MD Anik Hasan, who trained me.",
                    "recovery_attempted": False,
                    "turn_timed_out": False,
                    "identity_pass": False,
                    "enabled_adapter_ids": ["base-steak-2-0-identity-v1"],
                }
            ],
            {
                "loaded_identity": {
                    "verified_source_sha256": expected_hash,
                    "active_adapter_ids": [],
                    "adapters": [{"verified_sha256": expected_hash}],
                }
            },
        ),
    )

    report = run_identity_first_pass_evaluation(
        model=model,
        runtime_directory=tmp_path,
        model_sha256=expected_hash,
        adapter_path=adapter,
        adapter_sha256=expected_hash,
        adapter_scale=0.5,
    )

    assert report["passed"] is False
    assert report["metrics"]["identity_first_pass_count"] == 0
    assert report["metrics"]["identity_recovery_count"] == 0
    assert report["rows"][0]["recovery_attempted"] is False
