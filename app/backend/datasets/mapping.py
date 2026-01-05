"""Normalize source mappings and render records as exact training examples."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import DatasetValidationError, _RecordProblem
from .oasst2 import (
    DEFAULT_BRANCH_POLICY,
    OASST2AdapterError,
    reconstruct_oasst2,
    reconstruct_oasst2_record,
)
from .source_inspection import _record_events, inspect_source


MAPPING_TYPES = frozenset({"plain_text", "instruction", "conversation", "oasst2"})
MAPPING_FIELDS = frozenset(
    {
        "type",
        "plain_text",
        "instruction",
        "user_input",
        "assistant_response",
        "conversation_messages",
        "system_content",
        "languages",
        "branch_policy",
        "include_negative_reviews",
    }
)


def _extract(record: Any, field_path: str) -> Any:
    value = record
    for part in field_path.split("."):
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                raise _RecordProblem("required_field", f"Missing field {field_path}")
        else:
            raise _RecordProblem("required_field", f"Missing field {field_path}")
    return value


def _text_value(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    raise _RecordProblem(
        "required_field",
        f"Mapped field {field_name} must contain text or a scalar value",
    )


def normalize_mapping(
    mapping: Mapping[str, Any],
    *,
    format_name: str | None = None,
) -> dict[str, str]:
    unknown = set(mapping) - MAPPING_FIELDS
    if unknown:
        raise DatasetValidationError(
            f"Unknown mapping fields: {', '.join(sorted(unknown))}"
        )
    mapping_type = str(mapping.get("type", "plain_text"))
    if mapping_type not in MAPPING_TYPES:
        raise DatasetValidationError(f"Unknown mapping type: {mapping_type}")
    normalized = {
        str(key): str(value)
        for key, value in mapping.items()
        if value is not None and str(value).strip()
    }
    normalized["type"] = mapping_type
    if mapping_type == "plain_text":
        normalized.setdefault("plain_text", "text" if format_name == "txt" else "")
        if not normalized["plain_text"]:
            raise DatasetValidationError("Plain-text mapping requires plain_text")
    elif mapping_type == "instruction":
        if not normalized.get("instruction") and not normalized.get("user_input"):
            raise DatasetValidationError(
                "Instruction mapping requires instruction or user_input"
            )
        if not normalized.get("assistant_response"):
            raise DatasetValidationError(
                "Instruction mapping requires assistant_response"
            )
    elif mapping_type == "conversation" and not normalized.get("conversation_messages"):
        raise DatasetValidationError(
            "Conversation mapping requires conversation_messages"
        )
    elif mapping_type == "oasst2":
        normalized.setdefault("branch_policy", DEFAULT_BRANCH_POLICY)
        normalized.setdefault("include_negative_reviews", "false")
        if normalized["branch_policy"] != DEFAULT_BRANCH_POLICY:
            raise DatasetValidationError(
                "OASST2 preparation supports only best_ranked_leaf so shared "
                "prefixes are not duplicated"
            )
    return normalized


def _conversation_parts(messages: Any) -> list[tuple[str, str]]:
    if not isinstance(messages, Sequence) or isinstance(
        messages, (str, bytes, bytearray)
    ):
        raise _RecordProblem(
            "invalid_conversation_roles", "Conversation messages must be a list"
        )
    result: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise _RecordProblem(
                "invalid_conversation_roles",
                "Each conversation message must be an object",
            )
        role = str(message.get("role", "")).strip().lower()
        content = _text_value(message.get("content"), "content")
        if role not in {"system", "user", "assistant"} or not content.strip():
            raise _RecordProblem(
                "invalid_conversation_roles",
                "Invalid conversation role or empty content",
            )
        result.append((role, content))
    if not result:
        raise _RecordProblem("empty_record", "Conversation contains no messages")
    start = 0
    if result[0][0] == "system":
        start = 1
    turns = result[start:]
    if not turns or turns[0][0] != "user":
        raise _RecordProblem(
            "invalid_conversation_roles", "Conversation must begin with a user turn"
        )
    for offset, (role, _content) in enumerate(turns):
        expected = "user" if offset % 2 == 0 else "assistant"
        if role != expected:
            code = (
                "missing_assistant_response"
                if expected == "assistant"
                else "invalid_conversation_roles"
            )
            raise _RecordProblem(code, f"Expected {expected}, found {role}")
    if turns[-1][0] != "assistant":
        raise _RecordProblem(
            "missing_assistant_response",
            "Conversation must end with an assistant response",
        )
    return result


def format_training_example(record: Any, mapping: Mapping[str, Any]) -> str:
    normalized = normalize_mapping(mapping)
    mapping_type = normalized["type"]
    if mapping_type == "plain_text":
        content = _text_value(
            _extract(record, normalized["plain_text"]),
            normalized["plain_text"],
        )
        if not content.strip():
            raise _RecordProblem("empty_record", "Plain-text record is empty")
        return content

    if mapping_type == "instruction":
        parts: list[str] = []
        system_field = normalized.get("system_content")
        if system_field:
            system = _text_value(_extract(record, system_field), system_field)
            if system.strip():
                parts.append(f"System:\n{system}")
        instruction_field = normalized.get("instruction")
        if instruction_field:
            instruction = _text_value(
                _extract(record, instruction_field), instruction_field
            )
            if instruction.strip():
                parts.append(f"Instruction:\n{instruction}")
        input_field = normalized.get("user_input")
        if input_field:
            user_input = _text_value(_extract(record, input_field), input_field)
            if user_input.strip():
                parts.append(f"User input:\n{user_input}")
        response_field = normalized["assistant_response"]
        response = _text_value(_extract(record, response_field), response_field)
        if not response.strip():
            raise _RecordProblem(
                "missing_assistant_response", "Assistant response is empty"
            )
        parts.append(f"Assistant:\n{response}")
        if len(parts) == 1:
            raise _RecordProblem("empty_record", "Instruction and input are empty")
        return "\n\n".join(parts)

    if mapping_type == "oasst2":
        try:
            branch = reconstruct_oasst2_record(
                record,
                allowed_languages=[
                    item.strip()
                    for item in normalized.get("languages", "").split(",")
                    if item.strip()
                ]
                or None,
                include_negative_reviews=normalized.get(
                    "include_negative_reviews", "false"
                ).casefold()
                in {"1", "true", "yes"},
                branch_policy=normalized.get(
                    "branch_policy", DEFAULT_BRANCH_POLICY
                ),
            )
        except OASST2AdapterError as exc:
            code = (
                "missing_assistant_response"
                if exc.code == "NO_VALID_ASSISTANT_TARGETS"
                else "invalid_conversation_roles"
            )
            raise _RecordProblem(code, str(exc)) from exc
        messages = [(item["role"], item["content"]) for item in branch.chat_messages()]
    else:
        messages_field = normalized["conversation_messages"]
        messages = _conversation_parts(_extract(record, messages_field))
    parts = []
    system_field = normalized.get("system_content")
    if system_field:
        system = _text_value(_extract(record, system_field), system_field)
        if system.strip():
            parts.append(f"System:\n{system}")
    labels = {"system": "System", "user": "User", "assistant": "Assistant"}
    parts.extend(f"{labels[role]}:\n{content}" for role, content in messages)
    return "\n\n".join(parts)


def mapped_training_record(
    record: Any,
    mapping: Mapping[str, Any],
    *,
    source_record_index: int | None = None,
) -> dict[str, Any]:
    """Return the semantic training record used by the v2 preparation path.

    ``format_training_example`` remains the human-readable compatibility
    preview.  This function carries the actual role structure so the tokenizer
    can apply the same control-token template used at inference time.
    """

    normalized = normalize_mapping(mapping)
    mapping_type = normalized["type"]
    provenance: dict[str, Any] = {
        "source_record_index": source_record_index,
        "mapping_type": mapping_type,
    }
    if mapping_type == "plain_text":
        text = _text_value(
            _extract(record, normalized["plain_text"]),
            normalized["plain_text"],
        )
        if not text.strip():
            raise _RecordProblem("empty_record", "Plain-text record is empty")
        return {
            "formatted_text": text,
            "messages": None,
            "assistant_only_labels": False,
            "group_id": f"record:{source_record_index}",
            "provenance": provenance,
        }

    if mapping_type == "instruction":
        messages: list[dict[str, str]] = []
        system_field = normalized.get("system_content")
        if system_field:
            system = _text_value(_extract(record, system_field), system_field)
            if system.strip():
                messages.append({"role": "system", "content": system})
        user_parts: list[str] = []
        instruction_field = normalized.get("instruction")
        if instruction_field:
            instruction = _text_value(
                _extract(record, instruction_field), instruction_field
            )
            if instruction.strip():
                user_parts.append(instruction)
        input_field = normalized.get("user_input")
        if input_field:
            user_input = _text_value(_extract(record, input_field), input_field)
            if user_input.strip():
                user_parts.append(user_input)
        if not user_parts:
            raise _RecordProblem("empty_record", "Instruction and input are empty")
        response_field = normalized["assistant_response"]
        response = _text_value(_extract(record, response_field), response_field)
        if not response.strip():
            raise _RecordProblem(
                "missing_assistant_response", "Assistant response is empty"
            )
        messages.extend(
            [
                {"role": "user", "content": "\n\n".join(user_parts)},
                {"role": "assistant", "content": response},
            ]
        )
    elif mapping_type == "conversation":
        raw_messages = _conversation_parts(
            _extract(record, normalized["conversation_messages"])
        )
        messages = [
            {"role": role, "content": content} for role, content in raw_messages
        ]
        system_field = normalized.get("system_content")
        if system_field:
            system = _text_value(_extract(record, system_field), system_field)
            if system.strip():
                messages.insert(0, {"role": "system", "content": system})
    else:
        try:
            branch = reconstruct_oasst2_record(
                record,
                allowed_languages=[
                    item.strip()
                    for item in normalized.get("languages", "").split(",")
                    if item.strip()
                ]
                or None,
                include_negative_reviews=normalized.get(
                    "include_negative_reviews", "false"
                ).casefold()
                in {"1", "true", "yes"},
                branch_policy=normalized.get(
                    "branch_policy", DEFAULT_BRANCH_POLICY
                ),
            )
        except OASST2AdapterError as exc:
            code = (
                "missing_assistant_response"
                if exc.code == "NO_VALID_ASSISTANT_TARGETS"
                else "invalid_conversation_roles"
            )
            raise _RecordProblem(code, str(exc)) from exc
        messages = branch.chat_messages()
        provenance.update(
            {
                "message_tree_id": branch.tree_id,
                "branch_id": branch.branch_id,
                "message_ids": [
                    message.message_id for message in branch.messages
                ],
                "branch_policy": branch.branch_policy,
                "source_layout": branch.source_layout,
                "source_record_indices": list(branch.source_record_indices),
            }
        )

    formatted = "\n\n".join(
        f"{message['role'].capitalize()}:\n{message['content']}"
        for message in messages
    )
    return {
        "formatted_text": formatted,
        "messages": messages,
        "assistant_only_labels": True,
        "group_id": provenance.get(
            "message_tree_id", f"record:{source_record_index}"
        ),
        "provenance": provenance,
    }


def preview_mapping(
    source_path: str | Path,
    mapping: Mapping[str, Any],
    *,
    sample_limit: int = 3,
) -> dict[str, Any]:
    """Inspect a source and format the exact samples shown to the user."""

    inspection = inspect_source(source_path, sample_limit=sample_limit)
    normalized = normalize_mapping(mapping, format_name=inspection["format"])
    formatted: list[dict[str, Any]] = []
    if normalized["type"] == "oasst2":
        events = list(
            _record_events(
                Path(source_path),
                inspection["format"],
                inspection["encoding"],
            )
        )
        malformed = [event for event in events if event.error is not None]
        if malformed:
            return {
                "mapping": normalized,
                "formatted_samples": [
                    {
                        "record": event.index,
                        "error": "malformed_record",
                        "message": str(event.error),
                    }
                    for event in malformed[:sample_limit]
                ],
                "inspection": inspection,
            }
        diagnostics: list[dict[str, Any]] = []
        branches = reconstruct_oasst2(
            [event.record for event in events],
            allowed_languages=[
                item.strip()
                for item in normalized.get("languages", "").split(",")
                if item.strip()
            ]
            or None,
            include_negative_reviews=normalized.get(
                "include_negative_reviews", "false"
            ).casefold()
            in {"1", "true", "yes"},
            branch_policy=normalized.get(
                "branch_policy", DEFAULT_BRANCH_POLICY
            ),
            skip_rejected=True,
            diagnostics=diagnostics,
        )
        for branch in branches:
            messages = branch.chat_messages()
            for target_ordinal, target_position in enumerate(
                (
                    index
                    for index, message in enumerate(messages)
                    if message["role"] == "assistant"
                ),
                start=1,
            ):
                target_messages = messages[: target_position + 1]
                formatted.append(
                    {
                        "record": (
                            branch.source_record_indices[0]
                            if branch.source_record_indices
                            else None
                        ),
                        "formatted_example": "\n\n".join(
                            f"{message['role'].capitalize()}:\n"
                            f"{message['content']}"
                            for message in target_messages
                        ),
                        "semantic_messages": target_messages,
                        "label_mask": "terminal_assistant_only",
                        "terminal_assistant_only_labels": True,
                        "group_id": branch.tree_id,
                        "message_tree_id": branch.tree_id,
                        "branch_id": branch.branch_id,
                        "target_ordinal": target_ordinal,
                    }
                )
                if len(formatted) >= sample_limit:
                    return {
                        "mapping": normalized,
                        "formatted_samples": formatted,
                        "inspection": inspection,
                        "adapter_diagnostics": diagnostics[:100],
                    }
        return {
            "mapping": normalized,
            "formatted_samples": formatted,
            "inspection": inspection,
            "adapter_diagnostics": diagnostics[:100],
        }
    for index, sample in enumerate(inspection["samples"], start=1):
        try:
            text = format_training_example(sample, normalized)
            semantic = mapped_training_record(
                sample,
                normalized,
                source_record_index=index,
            )
            formatted.append(
                {
                    "record": index,
                    "formatted_example": text,
                    "semantic_messages": semantic["messages"],
                    "label_mask": (
                        "assistant_only"
                        if semantic["assistant_only_labels"]
                        else "all_tokens"
                    ),
                    "group_id": semantic["group_id"],
                }
            )
        except _RecordProblem as exc:
            formatted.append(
                {"record": index, "error": exc.code, "message": str(exc)}
            )
    return {
        "mapping": normalized,
        "formatted_samples": formatted,
        "inspection": inspection,
    }
