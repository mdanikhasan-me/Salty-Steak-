from app.backend.training.identity_dialogue_dataset import training_examples
from app.backend.training.identity_evaluation import identity_facts
from tools.acceptance_identity_transcripts import (
    LEGACY_IDENTITY_CHALLENGES,
    _identity_pass,
)


def _unified_identity_row() -> dict[str, object]:
    content = "My name is Base Steak 2.0, trained by MD Anik Hasan (Sawlper)."
    adapter_id = "base-steak-2-0-identity-v1"
    return {
        "prompt": "what is your name",
        "content": content,
        "facts": identity_facts(content),
        "timed_out": False,
        "operation_state": "completed",
        "learned_route": "identity",
        "route_code": "E",
        "effective_reasoning_mode": "instant",
        "active_adapter_ids": [adapter_id],
        "identity_controller": {
            "specialist": {
                "controller": "single_unified_identity_adapter",
                "enabled_adapter_ids": [adapter_id],
            }
        },
        "identity_recovery": None,
        "activity_steps": 1,
    }


def test_valid_learned_identity_recovery_is_accepted() -> None:
    row = _unified_identity_row()

    assert _identity_pass(row, "model_name") is True

    primary_id = row["active_adapter_ids"][0]
    repair_id = "base-steak-2-0-identity-introduction-repair-v1"
    row["identity_controller"]["registered_adapter_ids"] = [primary_id, repair_id]
    row["active_adapter_ids"] = [repair_id]
    row["identity_recovery"] = {
        "attempted": True,
        "controller": "learned_generic_identity_remediation_expert",
        "repair_adapter_ids": [repair_id],
        "attempts": [
            {
                "adapter_ids": [repair_id],
                "passed": True,
                "remaining_defect": None,
            }
        ],
    }
    assert _identity_pass(row, "model_name") is True

    row["identity_recovery"]["attempts"][0]["passed"] = False
    assert _identity_pass(row, "model_name") is False


def test_legacy_name_challenges_are_exactly_unseen_during_training() -> None:
    training_prompts = {
        " ".join(example.messages[-1][1].casefold().split())
        for example in training_examples()
    }

    for prompt, _requirement in LEGACY_IDENTITY_CHALLENGES:
        assert " ".join(prompt.casefold().split()) not in training_prompts
