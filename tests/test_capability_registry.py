from __future__ import annotations

from app.backend.automation.broker import (
    CAPABILITIES,
    CAPABILITY_DISPLAY_NAMES,
    CAPABILITY_FIELDS,
)
from app.backend.automation.capability_registry import (
    CAPABILITY_REGISTRY,
    get_capability_descriptor,
)
from app.backend.automation.invocation import capability_contract
from app.backend.chat.agent_loop import build_system_prompt
from app.backend.chat.dispatch import build_turn_instruction


def test_every_executable_capability_has_one_complete_descriptor() -> None:
    assert tuple(CAPABILITY_REGISTRY) == CAPABILITIES
    assert set(CAPABILITY_DISPLAY_NAMES) == set(CAPABILITIES)
    assert set(CAPABILITY_FIELDS) == set(CAPABILITIES)

    for capability in CAPABILITIES:
        descriptor = get_capability_descriptor(capability)
        assert descriptor.capability_id == capability
        assert descriptor.description
        assert descriptor.affordance
        assert descriptor.side_effect_class in {"read", "mixed", "system_change"}
        assert descriptor.verification
        assert descriptor.output_types
        assert descriptor.observation_fields
        assert descriptor.supported_observations
        assert descriptor.requirements
        assert descriptor.permission == "explicit_grant_and_task_confirmation"
        assert set(descriptor.observation_fields) <= set(descriptor.output_types)
        assert descriptor.cancellation in {"between_calls", "in_flight"}
        assert CAPABILITY_DISPLAY_NAMES[capability] == descriptor.display_name
        assert CAPABILITY_FIELDS[capability] == frozenset(descriptor.argument_types)
        assert set(descriptor.required_arguments) <= set(descriptor.argument_types)


def test_argument_repair_contract_is_derived_from_the_descriptor() -> None:
    descriptor = get_capability_descriptor("files.manage")

    contract = capability_contract("files.manage")

    assert contract == descriptor.contract()
    assert contract["required"] == ["operation", "path"]
    assert contract["optional"] == [
        "content",
        "destination",
        "expected_sha256",
        "overwrite",
        "paths",
        "pattern",
        "permanent",
        "recursive",
    ]
    assert contract["argument_types"]["paths"] == "array[absolute_path]"
    assert contract["input_schema"]["required"] == ["operation", "path"]
    assert contract["output_schema"]["properties"]["matched_paths"] == (
        "array[absolute_path]"
    )
    assert "filesystem_state" in contract["supported_observations"]
    assert contract["permission"] == "explicit_grant_and_task_confirmation"
    assert "delete" in contract["operations"]
    assert contract["batch_capable"] is True


def test_router_and_agent_prompts_read_the_same_descriptor_registry() -> None:
    descriptor = get_capability_descriptor("files.manage")

    router = build_turn_instruction(
        image_available=False,
        has_previous_image=False,
        capabilities=["files.manage"],
        agent_mode=True,
    )
    agent = build_system_prompt(["files.manage"])

    assert descriptor.affordance in router
    assert descriptor.model_instructions in agent
    assert descriptor.agent_rules in agent
