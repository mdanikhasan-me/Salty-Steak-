from __future__ import annotations

import json

from app.backend.training.base_steak_identity_dataset import (
    MODEL_NAME,
    TRAINER,
    TRAINER_ALIAS,
    dataset_jsonl,
    dataset_sha256,
    holdout_examples,
    retention_examples,
    training_examples,
)


def test_identity_dataset_is_varied_deterministic_and_split_cleanly() -> None:
    training = training_examples()
    holdout = holdout_examples()
    retention = retention_examples()

    assert len(training) == 682
    assert len(holdout) == 50
    assert len(retention) >= 100
    assert sum(example.id.startswith("retention-automation-") for example in retention) == 10
    assert sum(example.id.startswith("retention-routed-ordinary-") for example in retention) == 10
    assert sum(example.id.startswith("retention-broad-") for example in retention) >= 60
    assert len({example.response for example in training}) == 18
    assert {
        example.messages[-1][1].casefold() for example in training
    }.isdisjoint(example.messages[-1][1].casefold() for example in holdout)
    assert {example.id for example in training}.isdisjoint(
        example.id for example in holdout
    )
    assert dataset_sha256(training, "train") == dataset_sha256(
        training_examples(), "train"
    )


def test_identity_responses_never_repeat_rejected_attributions() -> None:
    rejected = ("qwen", "claude", "gpt", "gemini", "llama", "alibaba", "anthropic")
    for example in [*training_examples(), *holdout_examples()]:
        response = example.response.casefold()
        assert not any(value in response for value in rejected)
        assert MODEL_NAME.casefold() in response
        assert TRAINER.casefold() in response
        assert TRAINER_ALIAS.casefold() in response


def test_jsonl_is_a_complete_conversation_training_artifact() -> None:
    examples = training_examples()
    records = [json.loads(line) for line in dataset_jsonl(examples, "train").splitlines()]

    assert len(records) == len(examples)
    assert all(record["schema"] == "base-steak-identity-example-v1" for record in records)
    assert all(record["split"] == "train" for record in records)
    assert all(record["messages"][-1]["role"] == "user" for record in records)
    assert all(record["response"] for record in records)
