"""Fail-closed translation between model text and host-owned action proposals.

The language model is never an authority to execute a host action.  This
module gives the model a narrow planning grammar and converts only validated
whole-response JSON into a durable proposal for a separate review surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


HOST_ACTION_SCHEMA = "salty-steak-host-action-proposal-v1"
IMAGE_ACTION = "image.generate"
TERMINAL_ACTION = "terminal.execute"
FILE_TRASH_ACTION = "filesystem.trash_file"
TEMP_CLEANUP_ACTION = "system.clean_temp"
TEMP_CLEANUP_CONFIRMATION = "CLEAN_WINDOWS_TEMPORARY_FILES"

_IMAGE_INTENT = re.compile(
    r"\b(?:generate|create|make|draw|render|produce)\b[\s\S]{0,80}"
    r"\b(?:image|picture|photo|illustration|artwork)\b",
    re.IGNORECASE,
)
_TERMINAL_INTENT = re.compile(
    r"\b(?:terminal|powershell|command prompt|cmd\.exe|on my (?:pc|computer))\b",
    re.IGNORECASE,
)
_HOST_ACTION_VERB = re.compile(
    r"\b(?:run|execute|delete|remove|clean|move|copy|rename|create|open|close)\b",
    re.IGNORECASE,
)
_FILE_DELETE_VERB = re.compile(r"\b(?:delete|remove|trash)\b", re.IGNORECASE)
_TEMP_CLEANUP_INTENT = re.compile(
    r"\b(?:clean|clear|delete|remove|purge)\b[\s\S]{0,120}"
    r"(?:%temp%|\bprefetch\b|"
    r"\b(?:windows\s+)?temp(?:orary)?\s+(?:data|files|folders|cache)\b|"
    r"\ball\s+(?:the\s+)?(?:temp|temporary)(?:\s+(?:data|files|folders|cache))?\b)",
    re.IGNORECASE,
)
_QUOTED_WINDOWS_PATH = re.compile(
    r'''(?P<quote>["'])(?P<path>[A-Za-z]:\\[^\r\n"']+)(?P=quote)'''
)
_UNIX_EXECUTABLES = {"bash", "sh", "zsh", "rm", "sudo"}


def detect_host_action_intent(user_text: str) -> str | None:
    """Return a narrow action kind only for an explicit host-action request."""

    text = str(user_text or "").strip()
    if _IMAGE_INTENT.search(text):
        return IMAGE_ACTION
    if extract_file_trash_target(text):
        return FILE_TRASH_ACTION
    if is_temp_cleanup_request(text):
        return TEMP_CLEANUP_ACTION
    if _TERMINAL_INTENT.search(text) and _HOST_ACTION_VERB.search(text):
        return TERMINAL_ACTION
    return None


def host_action_planning_prompt(intent: str) -> str:
    """Build an ephemeral, non-authoritative planner instruction."""

    if intent == IMAGE_ACTION:
        return (
            "Plan one image-generation request for the local Salty Steak host. "
            "Return exactly one JSON object and no markdown or prose: "
            '{"schema":"salty-steak-host-action-proposal-v1",'
            '"action":"image.generate","arguments":{"prompt":"..."}}. '
            "Do not claim that an image was generated or that any tool ran."
        )
    if intent == TERMINAL_ACTION:
        return (
            "Plan one Windows terminal action for the local Salty Steak host. "
            "Return exactly one JSON object and no markdown or prose. Use schema "
            '"salty-steak-host-action-proposal-v1" and action '
            '"terminal.execute". arguments must contain argv as a JSON string '
            "array, optional working_directory, and timeout_seconds. Never use "
            "bash, sh, sudo, rm, /tmp, or another Unix-only command. If an exact "
            "target or necessary argument is missing, return "
            '{"schema":"salty-steak-host-action-proposal-v1",'
            '"action":"terminal.execute","requires_clarification":true,'
            '"question":"..."}. Do not claim execution.'
        )
    if intent in {FILE_TRASH_ACTION, TEMP_CLEANUP_ACTION}:
        return (
            "The Salty Steak host already mapped this request to a bounded "
            "Windows action. Do not run a model planner for this action."
        )
    raise ValueError("Unsupported host action intent")


def normalise_host_action_response(
    *,
    intent: str | None,
    user_text: str,
    model_text: str,
    proposal_id: str,
    image_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return host-owned display text and a non-executable proposal.

    The raw model output is represented only by a digest.  Canonical arguments
    are kept in the proposal for review, but this function has no execution
    dependency and cannot invoke a tool.
    """

    if intent not in {IMAGE_ACTION, TERMINAL_ACTION}:
        return None
    raw = str(model_text or "").strip()
    parsed = _whole_json_object(raw)
    evidence = {
        "planner_output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "planner_output_bytes": len(raw.encode("utf-8")),
    }
    if intent == IMAGE_ACTION:



        prompt = str(user_text or "").strip()





        runtime_ready = bool(
            image_runtime
            and image_runtime.get("activation_allowed") is True
            and image_runtime.get("external_service_required") is False
        )
        reason = _image_runtime_reason(image_runtime)
        proposal = {
            "schema": HOST_ACTION_SCHEMA,
            "id": proposal_id,
            "kind": IMAGE_ACTION,
            "title": "Create an image",
            "summary": prompt[:4_000],
            "arguments": {"prompt": prompt[:4_000]},
            "state": "pending_review" if runtime_ready else "blocked_runtime_unavailable",
            "requires_confirmation": True,
            "execution_allowed": False,
            "runtime_reason": None if runtime_ready else reason,
            **evidence,
        }
        return {
            "content": (
                "Image request prepared for review."
                if runtime_ready
                else "The image request is ready, but the local image-generation runtime is not ready yet."
            ),
            "proposal": proposal,
        }

    clarification = _clarification_question(parsed)
    arguments = _terminal_arguments(parsed)
    if clarification or arguments is None:
        question = clarification or (
            "Give me the exact Windows file, folder, or command arguments you want reviewed. "
            "I will not guess a destructive target."
        )
        return {
            "content": question,
            "proposal": {
                "schema": HOST_ACTION_SCHEMA,
                "id": proposal_id,
                "kind": TERMINAL_ACTION,
                "title": "Terminal action needs details",
                "summary": question,
                "arguments": None,
                "state": "needs_clarification",
                "requires_confirmation": True,
                "execution_allowed": False,
                **evidence,
            },
        }
    unix_reason = _unix_command_reason(arguments["argv"])
    if unix_reason:
        return {
            "content": (
                "That plan used a Unix command, so it was blocked. "
                "Give me the exact Windows target and I will prepare a Windows-native action for review."
            ),
            "proposal": {
                "schema": HOST_ACTION_SCHEMA,
                "id": proposal_id,
                "kind": TERMINAL_ACTION,
                "title": "Terminal action blocked",
                "summary": unix_reason,
                "arguments": None,
                "state": "blocked_platform_mismatch",
                "requires_confirmation": True,
                "execution_allowed": False,
                **evidence,
            },
        }
    destructive = _looks_destructive(arguments["argv"])
    proposal = {
        "schema": HOST_ACTION_SCHEMA,
        "id": proposal_id,
        "kind": TERMINAL_ACTION,
        "title": "Review terminal command",
        "summary": _argv_summary(arguments["argv"]),
        "arguments": arguments,
        "state": "pending_review",
        "risk": "destructive" if destructive else "standard",
        "requires_confirmation": True,
        "execution_allowed": False,
        **evidence,
    }
    return {
        "content": "A Windows terminal action is prepared. Review the exact arguments before anything runs.",
        "proposal": proposal,
    }


def extract_file_trash_target(user_text: str) -> str | None:
    """Extract one explicit quoted absolute Windows file target."""

    text = str(user_text or "").strip()
    if not _FILE_DELETE_VERB.search(text):
        return None
    matches = [match.group("path").strip() for match in _QUOTED_WINDOWS_PATH.finditer(text)]
    if len(matches) != 1:
        return None
    return matches[0]


def is_temp_cleanup_request(user_text: str) -> bool:
    """Return true only for an explicit request to clean Windows temp data."""

    return bool(_TEMP_CLEANUP_INTENT.search(str(user_text or "").strip()))


def build_temp_cleanup_proposal(
    *,
    user_text: str,
    proposal_id: str,
    roots: list[Mapping[str, Any]],
    authority_mode: str,
) -> dict[str, Any]:
    """Build a host-owned, exact-root Windows temp-cleanup proposal."""

    checked_authority = (
        "full_access" if authority_mode == "full_access" else "ask_every_time"
    )
    public_roots = [
        {
            "name": str(root.get("name") or "Windows temporary data"),
            "path": str(root.get("path") or ""),
            "exists": bool(root.get("exists")),
        }
        for root in roots
        if str(root.get("path") or "").strip()
    ]
    proposal = {
        "schema": HOST_ACTION_SCHEMA,
        "id": proposal_id,
        "kind": TEMP_CLEANUP_ACTION,
        "title": "Clean Windows temporary files",
        "summary": (
            "Clean the contents of User Temp, Windows Temp, and Prefetch. "
            "Files that Windows is using, inaccessible entries, and reparse points are skipped."
        ),
        "arguments": {"roots": public_roots},
        "state": "pending_review",
        "risk": "destructive_permanent_bounded_roots",
        "requires_confirmation": checked_authority != "full_access",
        "execution_allowed": False,
        "authority_mode": checked_authority,
        "recovery": "not_available_for_temporary_data_cleanup",
        "request_sha256": hashlib.sha256(
            str(user_text).encode("utf-8")
        ).hexdigest(),
        "planner_used": False,
    }
    return {
        "content": (
            "Full access is enabled. Cleaning the bounded Windows temporary folders now."
            if checked_authority == "full_access"
            else "The Windows temporary-folder cleanup is ready for your review."
        ),
        "proposal": proposal,
    }


def build_file_trash_proposal(
    *,
    user_text: str,
    proposal_id: str,
    snapshot: Mapping[str, Any] | None,
    error: str | None = None,
    authority_mode: str = "ask_every_time",
) -> dict[str, Any]:
    """Build a host-owned file proposal without asking the model to plan it."""

    target = extract_file_trash_target(user_text)
    if not target:
        raise ValueError("One quoted absolute Windows file path is required")
    evidence = {
        "request_sha256": hashlib.sha256(
            str(user_text).encode("utf-8")
        ).hexdigest(),
        "planner_used": False,
    }
    checked_authority = (
        "full_access" if authority_mode == "full_access" else "ask_every_time"
    )
    if snapshot is None:
        reason = str(error or "The exact file could not be verified.").strip()
        return {
            "content": reason,
            "proposal": {
                "schema": HOST_ACTION_SCHEMA,
                "id": proposal_id,
                "kind": FILE_TRASH_ACTION,
                "title": "Move a file to Recycle Bin",
                "summary": target,
                "arguments": {"path": target},
                "state": "blocked_target_unavailable",
                "risk": "destructive_recoverable",
                "requires_confirmation": True,
                "execution_allowed": False,
                "authority_mode": checked_authority,
                "runtime_reason": reason,
                **evidence,
            },
        }
    canonical = str(snapshot["path"])
    proposal = {
        "schema": HOST_ACTION_SCHEMA,
        "id": proposal_id,
        "kind": FILE_TRASH_ACTION,
        "title": "Move a file to Recycle Bin",
        "summary": canonical,
        "arguments": {
            "path": canonical,
            "expected_size_bytes": int(snapshot["size_bytes"]),
            "expected_modified_ns": int(snapshot["modified_ns"]),
        },
        "state": "pending_review",
        "risk": "destructive_recoverable",
        "requires_confirmation": checked_authority != "full_access",
        "execution_allowed": False,
        "authority_mode": checked_authority,
        "recovery": "windows_recycle_bin",
        **evidence,
    }
    return {
        "content": (
            "Full access is enabled. Moving the exact file to the Windows Recycle Bin now."
            if checked_authority == "full_access"
            else "I found the exact file. Confirm below to move it to the Windows Recycle Bin."
        ),
        "proposal": proposal,
    }


def _whole_json_object(raw: str) -> dict[str, Any] | None:
    if not raw.startswith("{") or not raw.endswith("}"):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _action_name(value: Mapping[str, Any] | None) -> str:
    if not value:
        return ""
    return str(value.get("action") or "").strip().casefold().replace("_", ".")


def _image_prompt(value: Mapping[str, Any] | None) -> str:
    if _action_name(value) not in {IMAGE_ACTION, "generate.image"}:
        return ""
    arguments = value.get("arguments") if value else None
    prompt = arguments.get("prompt") if isinstance(arguments, Mapping) else value.get("prompt")
    text = str(prompt or "").strip()
    return text if 0 < len(text) <= 4_000 else ""


def _clarification_question(value: Mapping[str, Any] | None) -> str:
    if not value or value.get("requires_clarification") is not True:
        return ""
    question = str(value.get("question") or "").strip()
    return question[:1_000] if question else ""


def _terminal_arguments(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if _action_name(value) not in {TERMINAL_ACTION, "terminal.command"}:
        return None
    arguments = value.get("arguments") if value else None
    if not isinstance(arguments, Mapping):
        return None
    argv = arguments.get("argv")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 128
        or any(not isinstance(item, str) or not item or len(item) > 32_768 for item in argv)
    ):
        return None
    cwd = str(arguments.get("working_directory") or "").strip()
    try:
        timeout = float(arguments.get("timeout_seconds", 10))
    except (TypeError, ValueError):
        return None
    if not 0.05 <= timeout <= 30:
        return None
    result: dict[str, Any] = {"argv": list(argv), "timeout_seconds": timeout}
    if cwd:
        result["working_directory"] = cwd[:32_768]
    return result


def _unix_command_reason(argv: list[str]) -> str:
    executable = str(argv[0]).strip().casefold().rsplit("\\", 1)[-1]
    if executable in _UNIX_EXECUTABLES:
        return f"{argv[0]} is not an approved Windows-native executable."
    if any(str(item).startswith("/tmp") for item in argv):
        return "/tmp is a Unix path and is not a valid Windows target."
    return ""


def _looks_destructive(argv: list[str]) -> bool:
    joined = " ".join(str(item) for item in argv).casefold()
    return any(token in joined for token in ("remove-item", "del ", "erase ", "rmdir", "clear-content"))


def _argv_summary(argv: list[str]) -> str:
    text = " ".join(argv)
    return text if len(text) <= 240 else f"{text[:237]}..."


def _image_runtime_reason(image_runtime: Mapping[str, Any] | None) -> str:
    if image_runtime:
        reason = str(
            image_runtime.get("runtime_reason")
            or image_runtime.get("unavailable_reason")
            or image_runtime.get("technical_details", {}).get("runtime_reason")
            or ""
        ).strip()
        if reason:
            return reason
    return (
        "Steak gen 1 ScaledFP8 is registered, but its compatible local text encoder, "
        "tokenizer, VAE, scheduler/configuration, and image worker have not passed validation."
    )
