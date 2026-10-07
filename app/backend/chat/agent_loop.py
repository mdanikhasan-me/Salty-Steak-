"""Re-entrant tool loop for local Windows automation.

Ordinary chat turns are single-shot: the model answers once and the turn ends.
An automation task needs the opposite shape, because the model cannot know what
a click did until it looks again.  This module runs the observe/act cycle,
feeding every tool result back into the conversation until the model reports
that the task is finished.

The model still has no execution authority.  Each step is validated here and
performed by ``AutomationBroker``, which independently enforces its own grants
and writes its own audit record.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from ..automation.capability_registry import (
    APPLICATION_LAUNCH_CAPABILITY,
    BROWSER_CAPABILITY,
    DISCORD_INSPECT_CAPABILITY,
    FILES_CAPABILITY,
    INPUT_CONTROL_CAPABILITY,
    SCREEN_CAPTURE_CAPABILITY,
    TERMINAL_CAPABILITY,
    UI_AUTOMATION_CAPABILITY,
    WINDOW_CONTROL_CAPABILITY,
    get_capability_descriptor,
)
from ..automation.routing import (
    execution_route_record,
    ordered_capabilities,
    resolve_execution,
)
from ..automation.credentials import redact
from ..automation.policy import (
    DENY,
    REQUIRE_APPROVAL,
    PolicyEngine,
    await_approval,
)
from ..system.files import atomic_write_json
from .actions import _looks_destructive
from .orchestrator import strip_reasoning
from .task_runtime import (
    EXECUTING,
    OBSERVING,
    PLANNING,
    WAITING,
    TaskCancelled,
    TaskContext,
)


AGENT_SCHEMA = "salty-steak-agent-task-v1"
RESPOND_ACTION = "respond"



MAX_ITERATIONS = 8_192
DEFAULT_MISSION_DURATION_SECONDS = 8 * 60 * 60
MAX_MISSION_DURATION_SECONDS = 8 * 60 * 60
MAX_PARSE_FAILURES = 3
MAX_OBSERVATION_CHARACTERS = 2_000
MAX_INSTRUCTION_CHARACTERS = 4_000
STALE_ANALYSIS_CHARACTERS = 160



MAX_IDENTICAL_ATTEMPTS = 3
DISCORD_EXHAUSTIVE_BATCH_SIZE = 10
DISCORD_EXHAUSTIVE_MESSAGE_LIMIT = 500
DISCORD_EXHAUSTIVE_SCROLLS = 30
DISCORD_EXHAUSTIVE_INVENTORY_SCROLLS = 100
DISCORD_EXHAUSTIVE_MAX_CHANNELS = 5_000









MAX_IDLE_REPEATS = 2
MAX_RETAINED_STEPS = 200
MAX_TRANSCRIPT_MESSAGES = 48
MAX_TRANSCRIPT_CHARACTERS = 96_000
RECENT_TRANSCRIPT_MESSAGES = 18
RECENT_CHECKPOINT_STEPS = 20
MISSION_CHECKPOINT_SCHEMA = "salty-steak-agent-mission-checkpoint-v1"
VISION_PROMPT = (
    "Describe this screen for an automation agent. List the visible windows, "
    "buttons, menus, text fields, and any readable text, and say roughly where "
    "each one sits on screen. Be factual and specific."
)
VISION_OUTPUT_TOKENS = 256
DISCORD_REPORT_SYSTEM = (
    "Write the final visible report for a read-only Discord inspection. "
    "Use only the supplied observed evidence. Write natural, concise prose, "
    "not JSON, code, private reasoning, or an action request. State exact "
    "coverage, lifecycle counts, requirement uncertainty, and gaps. Never "
    "claim that every candidate was checked when coverage is incomplete, and "
    "never claim that a message, reaction, join, entry, or account change was "
    "performed. Use digits for every measured count. A lifecycle total counts "
    "giveaway items, not channels: never label the sum of active, ended, and "
    "unknown items as a channel count. Use at most 180 words and finish the "
    "final sentence."
)
DISCORD_ATOMIC_FALLBACKS = frozenset(
    {
        APPLICATION_LAUNCH_CAPABILITY,
        BROWSER_CAPABILITY,
        INPUT_CONTROL_CAPABILITY,
        UI_AUTOMATION_CAPABILITY,
        WINDOW_CONTROL_CAPABILITY,
    }
)


def is_native_discord_task(instruction: str) -> bool:
    text = " ".join(str(instruction or "").casefold().split())
    if "discord" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "open discord",
            "discord app",
            "installed discord",
            "discord server",
            "discord channel",
            "signed-in discord",
            "signed in discord",
            "discord account",
            "giveaway",
        )
    )


def requires_discord_inventory(instruction: str) -> bool:
    text = " ".join(str(instruction or "").casefold().split())
    return is_native_discord_task(text) and any(
        marker in text
        for marker in (
            "giveaway",
            "all server",
            "every server",
            "which server",
            "whichever server",
            "server and channel",
            "inventory",
            "scan discord",
        )
    )


def requires_discord_content_scan(instruction: str) -> bool:
    text = " ".join(str(instruction or "").casefold().split())
    return requires_discord_inventory(text) and any(
        marker in text
        for marker in (
            "giveaway",
            "message",
            "running",
            "active",
            "requirement",
            "criteria",
        )
    )


def requires_discord_exhaustive_content_scan(instruction: str) -> bool:
    """Whether the requested scope cannot be proved by a candidate sample."""

    text = " ".join(str(instruction or "").casefold().split())
    if not requires_discord_content_scan(text):
        return False
    return any(
        marker in text
        for marker in (
            "all server",
            "every server",
            "whichever server",
            "all joined server",
            "every joined server",
            "all channel",
            "every channel",
            "whichever channel",
            "complete inventory",
            "full inventory",
            "scan everything",
            "scan them all",
        )
    )


_DISCORD_DISCOVERY_ALIASES = (
    (r"\bgiveaways?\b", "giveaway"),
    (r"\bgivers?\b", "giveaway"),
    (r"\braffles?\b", "raffle"),
    (r"\bprizes?\b", "prize"),
    (r"\brewards?\b", "reward"),
    (r"\bcontests?\b", "contest"),
    (r"\bevents?\b", "event"),
    (r"\bgifts?\b", "gift"),
)


def discord_discovery_queries(instruction: str) -> tuple[str, ...]:
    """Derive bounded live-search concepts from this request, never IDs.

    These are discovery hints only. A match still has to be opened and its
    visible messages read before it can be reported as active or ended.
    """

    text = " ".join(str(instruction or "").casefold().split())
    queries: list[str] = []
    for pattern, canonical in _DISCORD_DISCOVERY_ALIASES:
        if re.search(pattern, text) and canonical not in queries:
            queries.append(canonical)



    return tuple(queries[:3] or [""])

AGENT_RULES_HEAD = (
    "Rules:\n"
    "- The tools above are listed best route first. Always take the highest one "
    "that can do the job. Reading structured information beats looking at "
    "pixels, and looking at pixels beats moving the mouse.\n"
    "- Prefer the most direct capability that accomplishes the task. Do not add "
    "steps the task does not need.\n"
    "- Capabilities are primitives, not a prewritten mission. You decide which "
    "observation or action is needed next from the task and the latest evidence. "
    "A service capability may execute one requested primitive, but it must not "
    "silently expand that request into an inventory, scan, or external action.\n"




    "- A question the computer can answer directly — free space, a path, a "
    "version, whether something is installed, what is running — is one "
    "terminal.execute call. Do not photograph the screen or walk an interface "
    "tree to find something a command prints.\n"

    "- Every step must change something or learn something new. If you have "
    "just read a page or taken a screenshot, the next step acts on what you "
    "saw; reading it again tells you nothing you do not already have.\n"
)

AGENT_RULES_TAIL = (
    "- Use respond when the task is fully complete, you have confirmed it is "
    "impossible, or a destructive target remains ambiguous after read-only "
    "discovery and you need the user to identify it.\n"
    "- Resolve routine choices autonomously from the user's request and fresh "
    "observations. Never invent a destructive target or a Windows user path. "
    "For removal, first establish the exact requested path or selection; if "
    "the target remains ambiguous after read-only discovery, ask which target "
    "the user means and do not delete anything. Full access does not identify "
    "an unspecified file. Do not ask again for permission already granted.\n"
    "- If an action fails, try a different approach before giving up.\n"
    "- Describe each action briefly in the reason field so the user can follow "
    "your progress.\n\n"
    "Output format — reply with exactly this JSON object and nothing else:\n"
    '{"action": "<tool name>", "reason": "<one sentence: why this action now>", '
    '"arguments": { }, "answer": "<only when action is respond>"}'
)

SERVICE_WORKFLOW_RULES = {
    BROWSER_CAPABILITY: (
        "- Gmail: use browser.control with https://mail.google.com in the "
        "Salty-owned profile. Query real page elements. If the account needs "
        "sign-in, call show_window and stop for the user to authenticate; never "
        "request or store a password. Discord web is https://discord.com/app.\n"
        "- An opaque browser element handle carries no meaning by itself. When "
        "acting on one, repeat its observed accessible name in the name argument.\n"
        "- Never request, extract, paste, log, or store a raw Discord user token. "
        "Programmatic Discord access must use an official scoped OAuth2 grant or "
        "a dedicated bot account; normal-user login challenges stay visible for "
        "the user to complete.\n"
    ),
    APPLICATION_LAUNCH_CAPABILITY: (
        "- For Discord, prefer application.launch for the installed Discord app. "
        "If it is unavailable, use another granted structured route.\n"
        "- Switching Discord accounts uses Discord's visible Account Switcher. "
        "Choose only an already listed account automatically; adding or signing "
        "into an account waits for the user at password, passkey, CAPTCHA, or 2FA.\n"
    ),
    UI_AUTOMATION_CAPABILITY: (
        "- After Discord is open, use ui.automation to find channels, message "
        "fields, and buttons as controls rather than pixels. Repeat the observed "
        "accessible name beside an opaque element handle.\n"
        "- Focus Discord's top-level window with window.control focus, or with "
        "ui.automation focus scoped by the exact observed process_id or "
        "window_handle. Do not pass a window title where an element handle is "
        "required by an inside-window action.\n"
        "- A Discord server inventory comes from fresh observed server controls. "
        "Scroll and deduplicate what is actually visible; never invent hidden "
        "servers or scrape member/message data that was not requested.\n"
        "- Discord may initially expose only its native Electron shell. Focus the "
        "exact Discord window with window.control immediately before input, open "
        "its official Quick Switcher with Ctrl+K using "
        "input.control bound to that observed window, then read a fresh UI tree. "
        "Once renderer controls appear, do not walk the whole Discord tree: use "
        "find_control for the exact Quick Switcher ComboBox and Window, prefer "
        "ui.automation set_value over raw typing, then search for ListItem controls "
        "scoped to that Window. Prefix * lists/searches servers, # searches text "
        "channels, and ! searches voice channels. Preserve the observed result "
        "names, which include the server/channel pair, before selecting anything.\n"
        "- If target-bound Ctrl+K is refused because focus moved, run a fresh "
        "window.control focus and retry Ctrl+K. Do not treat an earlier focus as "
        "still true. Until the exact Quick Switcher ComboBox is observed, do not "
        "use a generic pattern=Value result, do not focus the Friends document, "
        "and do not use input.control type_text; those routes can type into the "
        "wrong Discord field.\n"
        "- To inspect giveaway activity, search observed text-channel candidates, "
        "navigate to one observed result, read the fresh channel header and visible "
        "messages, find the exact 'Messages in ...' List, and scope get_tree to "
        "that list. Scroll that list until a fresh read reaches the newest visible "
        "evidence; deduplicate messages and distinguish active from explicitly Ended. "
        "Record whether current evidence shows an active giveaway. "
        "Navigation is not entry: never click an entry/reaction button or send a "
        "message unless the user separately requests that exact external action.\n"
        "- Before joining Discord voice, resolve one exact account, server, and "
        "voice channel from fresh observations and read the current voice state. "
        "If that exact channel is already joined, do not click again. Otherwise "
        "invoke the observed channel once, then verify the joined state before "
        "responding. Never loop or retry a successful/pending join.\n"
    ),
}

MESSAGE_WORKFLOW_RULES = (
    "- A request to draft a Gmail or Discord message stops after the fields are "
    "filled and verified. It never presses Send. For any later send, repeat the "
    "observed control name in the action arguments (for example name=Send) so "
    "the policy can require explicit approval. Never claim a message was sent "
    "until a fresh page or control readback proves it.\n"
)


class AgentTaskError(RuntimeError):
    """Raised when an automation task cannot be run at all."""


def build_system_prompt(capabilities: Sequence[str]) -> str:
    """Describe only the tools whose grants are currently enabled.

    Advertising a capability the broker will refuse makes the model plan around
    a door it cannot open, so an ungranted tool is simply absent.  The ones that
    remain are listed in ladder order, because the order the model reads them in
    is itself a recommendation.
    """

    ordered = ordered_capabilities(capabilities)
    lines = [
        "You are a local Windows automation agent running inside Salty Steak on "
        "this computer.",
        "",
        "You have these tools, best route first:",
    ]
    for capability in ordered:
        try:
            description = get_capability_descriptor(capability).model_instructions
        except KeyError:
            continue
        lines.append(description)
    lines.append(
        "respond — Give your final answer when the task is complete or "
        "confirmed impossible.\n"
        '  Arguments: {"answer": "your message to the user"}'
    )
    rules = AGENT_RULES_HEAD
    for capability in ordered:
        try:
            rules += get_capability_descriptor(capability).agent_rules
        except KeyError:
            continue







        if not (
            capability == UI_AUTOMATION_CAPABILITY
            and DISCORD_INSPECT_CAPABILITY in ordered
        ):
            rules += SERVICE_WORKFLOW_RULES.get(capability, "")
    if DISCORD_INSPECT_CAPABILITY in ordered:





        rules += (
            "- When discord.inspect is available, it owns Discord focus, Quick "
            "Switcher access, inventory, channel navigation, and visible message "
            "reads. For a Discord inspection task, make discord.inspect the first "
            "action; it launches Discord itself when needed. "
            "Do not prepare it with window.control, ui.automation, input.control, "
            "or screen.capture. Use a lower-level fallback only after "
            "discord.inspect itself returns a failed observation that requires "
            "that fallback.\n"
        )
    if BROWSER_CAPABILITY in ordered or UI_AUTOMATION_CAPABILITY in ordered:
        rules += MESSAGE_WORKFLOW_RULES
    rules += AGENT_RULES_TAIL
    lines.extend(["", rules])
    return "\n".join(lines)


def parse_agent_action(text: str, allowed: Sequence[str]) -> dict[str, Any]:
    """Validate one whole-JSON action from the model.

    Only a complete JSON object is accepted.  Scraping an action out of
    surrounding prose would let stray model text act as an instruction.
    """

    raw = str(text or "").strip()
    if raw.startswith("```"):

        newline = raw.find("\n")
        raw = raw[newline + 1 :] if newline >= 0 else ""
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3].strip()
    if not raw.startswith("{") or not raw.endswith("}"):
        raise ValueError("Reply with exactly one JSON object and no other text.")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"That was not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("The JSON value must be an object.")

    action = str(parsed.get("action") or "").strip().casefold().replace("_", ".")
    if action == "respond":
        answer = str(parsed.get("answer") or "").strip()
        if not answer:
            arguments = parsed.get("arguments")
            if isinstance(arguments, Mapping):
                answer = str(arguments.get("answer") or "").strip()
        if not answer:
            raise ValueError("A respond action needs a non-empty answer.")
        return {
            "action": RESPOND_ACTION,
            "reason": str(parsed.get("reason") or "").strip()[:500],
            "arguments": {},
            "answer": answer,
        }
    if action not in set(allowed):
        raise ValueError(
            "Unknown action. Choose one of: " + ", ".join([*allowed, RESPOND_ACTION])
        )
    arguments = parsed.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("The arguments field must be a JSON object.")
    return {
        "action": action,
        "reason": str(parsed.get("reason") or "").strip()[:500],
        "arguments": dict(arguments),
        "answer": None,
    }


def effect_key(capability: str, arguments: Mapping[str, Any]) -> str | None:
    """What a call would *achieve*, independent of which rung achieves it.

    Exact-fingerprint stagnation only catches a call repeated verbatim. The
    live failure was subtler and worse: `application.launch` to youtube.com,
    then `browser.control open_url` to youtube.com, then `application.launch`
    again — three different calls, one effect, already true after the first.

    Normalising the effect is deliberately narrow. It maps a destination to the
    place it reaches, not a request to an intent; the model still decides what
    it wants and why. A call whose effect cannot be stated plainly returns None
    and is never suppressed.
    """

    def host_of(value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        lowered = text.casefold()
        if lowered.startswith(("http://", "https://")):
            from urllib.parse import urlsplit, urlunsplit

            parsed = urlsplit(text)
            # Only the scheme and host are case insensitive. Query strings,
            # fragments and path case can identify entirely different pages.
            authority = parsed.netloc.lower()
            path = "" if parsed.path == "/" else parsed.path
            destination = urlunsplit((parsed.scheme.lower(), authority, path,
                                      parsed.query, parsed.fragment))
            return f"visit:{destination}" if parsed.hostname else None
        return None

    if capability == "application.launch":
        target = arguments.get("target")
        return host_of(target) or (
            f"launch:{str(target).strip().casefold()}" if target else None
        )
    if capability == "browser.control":
        command = str(arguments.get("command") or "").casefold()
        if command in {"open_url", "navigate"}:
            return host_of(arguments.get("url"))
        if command == "show_window":
            return "browser:visible"



    # Clicking the same control can advance a wizard, increment a value or
    # reverse a toggle. Repeated observations, not control identity, detect
    # stagnation for these operations.
    return None


def step_fingerprint(
    capability: str,
    arguments: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> str:
    """Identify a step by what it did and what came back.

    Volatile fields are excluded so that "the same thing happened again" is not
    hidden by a changing path or timestamp.
    """

    stable = {
        key: value
        for key, value in observation.items()
        if key not in {"screenshot_path", "duration_ms", "audit_record_id"}
    }
    return json.dumps(
        {"capability": capability, "arguments": dict(arguments), "observation": stable},
        sort_keys=True,
        default=str,
    )


def is_destructive(action: str, arguments: Mapping[str, Any]) -> bool:
    """Report whether a step needs review before it can run unattended."""

    if action != TERMINAL_CAPABILITY:
        return False
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not argv:
        return False
    return _looks_destructive([str(item) for item in argv])


def explicit_constraint_violation(
    instruction: str,
    capability: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Enforce a user's explicit negative instruction before any side effect."""

    request = " ".join(str(instruction or "").casefold().split())
    command = str(arguments.get("command") or arguments.get("action") or "").casefold()
    name = str(arguments.get("name") or "").casefold()
    if capability == INPUT_CONTROL_CAPABILITY and command == "type_text" and any(
        phrase in request for phrase in ("do not type", "don't type", "never type")
    ):
        return "The task explicitly says not to type text. Use a structured read/navigation route."
    if capability in {UI_AUTOMATION_CAPABILITY, BROWSER_CAPABILITY} and command in {
        "invoke",
        "select",
        "click",
        "submit",
    }:
        constraints = (
            (("do not send", "don't send", "never send"), ("send", "reply")),
            (("do not post", "don't post", "never post"), ("post", "publish")),
            (("do not react", "don't react", "never react"), ("react", "reaction")),
            (("do not join", "don't join", "never join"), ("join",)),
            (("do not enter", "don't enter", "never enter"), ("enter", "entry")),
        )
        for phrases, controls in constraints:
            if any(phrase in request for phrase in phrases) and any(
                control in name for control in controls
            ):
                return (
                    f"The task explicitly forbids this external action: "
                    f"{arguments.get('name') or command}."
                )
    return None





MAX_LISTED_PATHS = 40
MAX_OBSERVATION_ITEMS = 40
MAX_UIA_OBSERVATION_ITEMS = 120
MAX_OBSERVATION_FIELDS = 50
MAX_OBSERVATION_DEPTH = 6
MAX_DISCORD_CHANNEL_SAMPLES = 12
MAX_DISCORD_SCAN_SAMPLES = 6
MAX_DISCORD_ITEM_SAMPLES = 3
MAX_DISCORD_REQUIREMENT_SAMPLES = 4
MAX_DISCORD_MESSAGE_SAMPLES = 2
MAX_DISCORD_EVIDENCE_CHARACTERS = 360
MAX_DISCORD_REQUIREMENT_CHARACTERS = 240
MAX_DISCORD_MESSAGE_CHARACTERS = 400


def _bounded_paths(value: Any) -> list[str]:
    return [str(item) for item in (value or [])][:MAX_LISTED_PATHS]


def _bounded_observation_value(value: Any, *, depth: int = 0) -> Any:
    """Keep structured tool data useful without turning it into a context dump."""

    value = redact(value)
    if depth >= MAX_OBSERVATION_DEPTH:
        return "[nested value omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_observation_value(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_OBSERVATION_FIELDS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_observation_value(item, depth=depth + 1)
            for item in list(value)[:MAX_OBSERVATION_ITEMS]
        ]
    if isinstance(value, str):
        return value[:MAX_OBSERVATION_CHARACTERS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_OBSERVATION_CHARACTERS]


def _discord_channel_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    fields = (
        "server",
        "channel",
        "channel_key",
        "channel_type",
        "name",
        "inventory_index",
        "discovery_priority",
        "readback_server",
        "readback_channel",
        "readback_matches_channel_key",
        "selection_match_basis",
        "window_title",
    )
    return {
        field: _bounded_observation_value(value[field])
        for field in fields
        if field in value
    }


def _discord_requirement_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    fields = (
        "type",
        "key",
        "operator",
        "expected",
        "status",
        "text",
        "confidence",
        "source_message_index",
    )
    record = {
        field: _bounded_observation_value(value[field])
        for field in fields
        if field in value
    }
    if "text" in record:
        record["text"] = str(record["text"])[
            :MAX_DISCORD_REQUIREMENT_CHARACTERS
        ]
    return record


def _discord_giveaway_summary(
    value: Any,
    *,
    include_requirements: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    fields = (
        "event_key",
        "state",
        "end_evidence",
        "criteria_status",
        "disposition",
        "is_newest_observed",
        "source_message_index",
    )
    record = {
        field: _bounded_observation_value(value[field])
        for field in fields
        if field in value
    }
    evidence = str(value.get("evidence") or "")
    if evidence:
        record["evidence"] = evidence[:MAX_DISCORD_EVIDENCE_CHARACTERS]
    if include_requirements:
        requirements = [
            _discord_requirement_summary(item)
            for item in list(value.get("requirements") or [])[
                :MAX_DISCORD_REQUIREMENT_SAMPLES
            ]
        ]
        requirements = [item for item in requirements if item]
        if requirements:
            record["requirements"] = requirements
    return record


def _discord_coverage_summary(value: Any, *, depth: int = 0) -> Any:
    """Keep proof of Discord coverage without copying every per-server page."""

    if depth >= 4:
        return "[coverage detail omitted]"
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "per_server" and isinstance(item, Mapping):
                rows = [row for row in item.values() if isinstance(row, Mapping)]
                compact["per_server_count"] = len(item)
                compact["per_server_complete_count"] = sum(
                    bool(row.get("scroll_boundary_reached"))
                    and not bool(row.get("truncated_by_limit"))
                    for row in rows
                )
                compact["per_server_unique_results"] = sum(
                    int(row.get("unique_results") or 0) for row in rows
                )
                compact["per_server_incomplete"] = [
                    str(name)[:200]
                    for name, row in item.items()
                    if isinstance(row, Mapping)
                    and (
                        not row.get("scroll_boundary_reached")
                        or row.get("truncated_by_limit")
                    )
                ][:12]
                continue
            compact[key_text] = _discord_coverage_summary(item, depth=depth + 1)
        return compact
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _discord_coverage_summary(item, depth=depth + 1)
            for item in list(value)[:12]
        ]
    if isinstance(value, str):
        return value[:MAX_DISCORD_EVIDENCE_CHARACTERS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_DISCORD_EVIDENCE_CHARACTERS]


def _discord_message_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, Any] = {}
    for key, item in list(value.items())[:20]:
        if isinstance(item, str):
            compact[str(key)] = item[:MAX_DISCORD_MESSAGE_CHARACTERS]
        elif item is None or isinstance(item, (bool, int, float)):
            compact[str(key)] = item
    return compact


def _discord_scan_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, Any] = {}
    selected = _discord_channel_summary(value.get("selected_channel"))
    if selected:
        compact["selected_channel"] = selected
    for field in (
        "channel_key",
        "candidate_classification",
        "criteria_status",
        "disposition",
        "explicit_state",
        "giveaway_state_counts",
        "discovery_signals",
        "message_list",
    ):
        if field in value:
            compact[field] = _bounded_observation_value(value[field])
    items = list(value.get("giveaway_items") or [])
    compact["giveaway_item_count"] = len(items)





    representative_items: list[Mapping[str, Any]] = []
    seen_items: set[str] = set()
    candidates: list[Any] = [value.get("newest_observed_giveaway")]
    for field in (
        "active_giveaway_items",
        "unknown_giveaway_items",
        "ended_giveaway_items",
    ):



        candidates.extend(list(value.get(field) or [])[:1])
    candidates.extend(items)
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        identity = str(item.get("event_key") or "").strip()
        if not identity:
            identity = json.dumps(
                {
                    "state": item.get("state"),
                    "source_message_index": item.get("source_message_index"),
                    "evidence": str(item.get("evidence") or "")[:120],
                },
                sort_keys=True,
                default=str,
            )
        if identity in seen_items:
            continue
        seen_items.add(identity)
        representative_items.append(item)
        if len(representative_items) >= MAX_DISCORD_ITEM_SAMPLES:
            break
    if representative_items:
        compact["representative_giveaway_items"] = [
            _discord_giveaway_summary(item, include_requirements=False)
            for item in representative_items
        ]
        compact["representative_giveaway_item_count"] = len(
            representative_items
        )




    requirement_candidates: list[Any] = list(value.get("requirements") or [])
    for item in representative_items:
        requirement_candidates.extend(list(item.get("requirements") or []))
    compact["requirement_record_count"] = len(requirement_candidates)
    requirements: list[dict[str, Any]] = []
    seen_requirements: set[str] = set()
    for item in requirement_candidates:
        summary = _discord_requirement_summary(item)
        if not summary:
            continue
        identity = json.dumps(summary, sort_keys=True, default=str)
        if identity in seen_requirements:
            continue
        seen_requirements.add(identity)
        if len(requirements) < MAX_DISCORD_REQUIREMENT_SAMPLES:
            requirements.append(summary)
    compact["unique_requirement_count"] = len(seen_requirements)
    if requirements:
        compact["requirements"] = requirements

    messages = list(value.get("messages") or [])
    compact["message_count"] = len(messages)



    if messages and not representative_items:
        compact["message_samples"] = [
            summary
            for summary in (
                _discord_message_summary(item)
                for item in messages[:MAX_DISCORD_MESSAGE_SAMPLES]
            )
            if summary
        ]
    unassociated = list(value.get("unassociated_rule_evidence") or [])
    if unassociated:
        compact["unassociated_rule_evidence"] = [
            str(_bounded_observation_value(item))[
                :MAX_DISCORD_REQUIREMENT_CHARACTERS
            ]
            for item in unassociated[:4]
        ]
    if "coverage" in value:
        compact["coverage"] = _discord_coverage_summary(value["coverage"])
    return compact


def _discord_model_observation(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project a full Discord audit result into a small model-facing record.

    The broker audit retains the complete result. This projection contains the
    exact channel readback, lifecycle, requirements, counts, cursors and gaps
    needed for the next decision without putting a 1,130-channel dump into one
    prompt message.
    """

    compact: dict[str, Any] = {
        "operation": str(result.get("operation") or ""),
        "observation_compacted": True,
    }
    account = result.get("account")
    if isinstance(account, Mapping):
        compact["account"] = {
            str(key): _bounded_observation_value(value)
            for key, value in account.items()
            if key in {"observed", "identity_bound", "label", "handle"}
        }
    servers = list(result.get("servers") or [])
    channels = list(result.get("channels") or [])
    if servers:
        compact["server_count"] = len(servers)
        compact["servers"] = [
            _bounded_observation_value(item) for item in servers[:12]
        ]
        compact["servers_sampled"] = len(compact["servers"])
    if channels:
        compact["channel_count"] = len(channels)
        compact["channels"] = [
            summary
            for summary in (
                _discord_channel_summary(item)
                for item in channels[:MAX_DISCORD_CHANNEL_SAMPLES]
            )
            if summary
        ]
        compact["channels_sampled"] = len(compact["channels"])
    for field in (
        "inventory_gaps",
        "scan_gaps",
        "discovery_candidate_count",
        "candidate_index_size",
        "candidate_server_count",
        "cursor",
        "next_cursor",
        "done",
        "total_channels",
        "index_scope",
        "giveaway_state_counts",
        "discovery_signals",
        "candidate_classification",
        "criteria_status",
        "disposition",
        "explicit_state",
        "error",
    ):
        if field in result:
            compact[field] = _bounded_observation_value(result[field])
    selected = _discord_channel_summary(result.get("selected_channel"))
    if selected:
        compact["selected_channel"] = selected
    scans = list(result.get("scans") or [])
    if scans:
        compact["scan_count"] = len(scans)
        aggregate_lifecycle = {"active": 0, "ended": 0, "unknown": 0}
        aggregate_confirmed = 0
        aggregate_manual_review = 0
        aggregate_requirements = 0
        aggregate_incomplete_history = 0
        for scan in scans:
            if not isinstance(scan, Mapping):
                continue
            counts = dict(scan.get("giveaway_state_counts") or {})
            for state in aggregate_lifecycle:
                try:
                    aggregate_lifecycle[state] += max(
                        0, int(counts.get(state) or 0)
                    )
                except (TypeError, ValueError):
                    pass
            aggregate_confirmed += (
                scan.get("candidate_classification") == "confirmed_giveaway"
            )
            aggregate_manual_review += scan.get("criteria_status") in {
                "needs_manual_review",
                "requirements_unverified",
            }
            message_coverage = dict(
                ((scan.get("coverage") or {}).get("messages") or {})
            )
            aggregate_incomplete_history += not bool(
                message_coverage.get("history_boundary_reached")
            )
            requirement_candidates = list(scan.get("requirements") or [])
            for giveaway_item in list(scan.get("giveaway_items") or []):
                if isinstance(giveaway_item, Mapping):
                    requirement_candidates.extend(
                        list(giveaway_item.get("requirements") or [])
                    )
            requirement_identities = {
                json.dumps(
                    _discord_requirement_summary(requirement),
                    sort_keys=True,
                    default=str,
                )
                for requirement in requirement_candidates
                if _discord_requirement_summary(requirement)
            }
            aggregate_requirements += len(requirement_identities)
        compact["scan_aggregate"] = {
            "lifecycle": aggregate_lifecycle,
            "confirmed_channel_count": aggregate_confirmed,
            "manual_review_channel_count": aggregate_manual_review,
            "requirement_record_count": aggregate_requirements,
            "incomplete_history_channel_count": aggregate_incomplete_history,
        }
        compact["scans"] = [
            summary
            for summary in (
                _discord_scan_summary(item)
                for item in scans[:MAX_DISCORD_SCAN_SAMPLES]
            )
            if summary
        ]
        compact["scans_sampled"] = len(compact["scans"])
    if not scans and any(
        result.get(field)
        for field in (
            "messages",
            "giveaway_items",
            "active_giveaway_items",
            "ended_giveaway_items",
            "unknown_giveaway_items",
            "requirements",
            "newest_observed_giveaway",
        )
    ):
        compact.update(_discord_scan_summary(result))
    if "coverage" in result:
        compact["coverage"] = _discord_coverage_summary(result["coverage"])
    compact["substep_count"] = len(list(result.get("substeps") or []))
    return compact


def summarise_observation(
    action: str,
    result: Mapping[str, Any],
    *,
    describe_screenshot: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Reduce a broker result to what the model needs for its next decision."""

    status = str(result.get("status") or "unknown")
    observation: dict[str, Any] = {"action": action, "status": status}
    if result.get("audit_record_id"):
        observation["audit_record_id"] = str(result["audit_record_id"])
    if action == SCREEN_CAPTURE_CAPABILITY or (
        action == BROWSER_CAPABILITY and result.get("command") == "capture_preview"
    ):
        artifact = dict(result.get("artifact") or {})
        observation.update(
            {
                "screenshot_path": artifact.get("path"),
                "image_width": artifact.get("width"),
                "image_height": artifact.get("height"),
                "screen_width": artifact.get("source_width"),
                "screen_height": artifact.get("source_height"),
                "scale_divisor": artifact.get("scale_divisor"),
            }
        )



        path = str(artifact.get("path") or "")
        if describe_screenshot is not None and path:
            try:
                description = describe_screenshot(path)
            except Exception as error:
                observation["visual_analysis_error"] = (
                    f"{type(error).__name__}: {error}"[:MAX_OBSERVATION_CHARACTERS]
                )
            else:
                if description:
                    observation["visual_analysis"] = description[
                        :MAX_OBSERVATION_CHARACTERS
                    ]
    elif action == TERMINAL_CAPABILITY:
        observation.update(
            {
                "exit_code": result.get("exit_code"),
                "stdout": str((result.get("stdout") or {}).get("text") or "")[
                    :MAX_OBSERVATION_CHARACTERS
                ],
                "stderr": str((result.get("stderr") or {}).get("text") or "")[
                    :MAX_OBSERVATION_CHARACTERS
                ],
            }
        )
    elif action == FILES_CAPABILITY:







        observation.update(
            {
                "operation": result.get("operation"),
                "mutating": bool(result.get("mutating")),
                "matched_paths": _bounded_paths(result.get("matched_paths")),
                "affected_paths": _bounded_paths(result.get("affected_paths")),
                "preserved_paths": _bounded_paths(result.get("preserved_paths")),
                "failed_paths": list(result.get("failed_paths") or [])[:MAX_LISTED_PATHS],
            }
        )
        after = result.get("after_state")
        if isinstance(after, Mapping):
            observation["after_state"] = {
                "still_present": _bounded_paths(after.get("still_present")),
                "preserved_present": _bounded_paths(after.get("preserved_present")),
            }
    elif action == APPLICATION_LAUNCH_CAPABILITY:
        observation["target"] = result.get("target")
    elif action == INPUT_CONTROL_CAPABILITY:
        observation["performed"] = result.get("action")
    elif action == DISCORD_INSPECT_CAPABILITY:
        observation.update(_discord_model_observation(result))
        return observation






    try:
        descriptor = get_capability_descriptor(action)
    except KeyError:
        descriptor = None
    if descriptor is not None:
        for field_name in descriptor.observation_fields:



            if action == SCREEN_CAPTURE_CAPABILITY and field_name == "artifact":
                continue
            if field_name in result and field_name not in observation:
                if action == BROWSER_CAPABILITY and field_name == "summary":
                    # Browser text is explicitly paginated. Do not secretly clip
                    # it again, leaving next_text_offset beyond unread evidence.
                    observation[field_name] = str(redact(result[field_name]) or "")[:8000]
                elif action == UI_AUTOMATION_CAPABILITY and field_name == "nodes":
                    observation[field_name] = [
                        _bounded_observation_value(item)
                        for item in list(result[field_name] or [])[
                            :MAX_UIA_OBSERVATION_ITEMS
                        ]
                    ]
                else:
                    observation[field_name] = _bounded_observation_value(
                        result[field_name]
                    )
    if action == FILES_CAPABILITY and isinstance(observation.get('change'), dict):
        original = (result.get('change') or {}).get('diff')
        if original != observation['change'].get('diff'):
            observation['change']['truncated'] = True
    return observation


class AgentLoop:
    """Drive observe/act iterations until the model reports completion."""

    def __init__(
        self,
        *,
        broker: Any,
        generate: Callable[[list[dict[str, str]]], str],
        capabilities: Sequence[str],
        authority_mode: str = "ask_every_time",
        max_iterations: int = MAX_ITERATIONS,
        on_step: Callable[[dict[str, Any]], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        describe_screenshot: Callable[[str], str] | None = None,
        generate_with_preview: Callable[
            [list[dict[str, str]], Callable[[Mapping[str, Any]], None]], str
        ]
        | None = None,
        generate_final_with_preview: Callable[
            [list[dict[str, str]], Callable[[Mapping[str, Any]], None]], str
        ]
        | None = None,
        task: TaskContext | None = None,
        memory: Any = None,
        mission_memory: Any = None,
        approve: Callable[[Mapping[str, Any]], bool] | None = None,
        policy: PolicyEngine | None = None,
        established: str = "",
        max_duration_seconds: float = DEFAULT_MISSION_DURATION_SECONDS,
        checkpoint_path: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.broker = broker
        self.generate = generate
        self.generate_with_preview = generate_with_preview
        self.generate_final_with_preview = (
            generate_final_with_preview or generate_with_preview
        )
        self.describe_screenshot = describe_screenshot
        self.memory = memory
        self.mission_memory = mission_memory





        self.established = str(established or "")


        self.task = task or TaskContext()
        self.capabilities = list(capabilities)
        self.authority_mode = (
            "full_access" if authority_mode == "full_access" else "ask_every_time"
        )


        self.approve = approve
        self.policy = policy or PolicyEngine(authority_mode=self.authority_mode)
        self.max_iterations = max(1, min(int(max_iterations), MAX_ITERATIONS))
        self.on_step = on_step
        self.on_progress = on_progress
        self.max_duration_seconds = max(
            0.001,
            min(float(max_duration_seconds), MAX_MISSION_DURATION_SECONDS),
        )
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self._clock = clock
        self._mission_started = self._clock()
        self.task.configure_mission_budget(
            step_limit=self.max_iterations,
            duration_limit_seconds=self.max_duration_seconds,
        )


        self.should_stop = should_stop
        self.steps: list[dict[str, Any]] = []
        self.total_step_count = 0


        self.model_turns = 0

        self._attempts: dict[str, int] = {}

        self._effects: set[str] = set()

        self._idle = 0
        self._completion_retries = 0
        self._step_started = self._clock()
        self._discord_inspector_failed = False
        self._service_route_repairs = 0
        self._discord_inventory_complete = False
        self._discord_candidate_index_ready = False
        self._discord_scan_started = False
        self._discord_scan_cursor = 0
    def _recall(self, task: str) -> str:
        """Remembered context for this task, or nothing at all.

        Memory is an aid, never a dependency: a store that is missing or
        failing must not stop a task the user asked for.
        """

        briefings: list[str] = []
        for store in (self.memory, self.mission_memory):
            if store is None:
                continue
            try:
                value = str(store.briefing(task) or "").strip()
            except Exception:
                value = ""
            if value:
                briefings.append(value)
        return "\n\n".join(briefings)
    def run(self, instruction: str, *, opening: str = "") -> dict[str, Any]:
        task = str(instruction or "").strip()
        if not task or len(task) > MAX_INSTRUCTION_CHARACTERS:
            raise ValueError(
                f"An automation task must contain 1 to {MAX_INSTRUCTION_CHARACTERS} characters"
            )
        if not self.capabilities:
            raise AgentTaskError(
                "No computer-control capability has been granted, so there is "
                "nothing this task can do."
            )
        begin_task = getattr(self.broker, "begin_agent_task", None)
        if callable(begin_task):
            begin_task(self.task.task_id, should_stop=self._stopped)
        system_prompt = build_system_prompt(self.capabilities)



        remembered = self._recall(task)
        if remembered:
            system_prompt = f"{system_prompt}\n\n{remembered}"



        if self.established:
            system_prompt = f"{system_prompt}\n{self.established}"
        # The native runtime may evict older user/assistant turns to fit a
        # growing tool transcript. Keep the exact task in retained context;
        # observations must not silently outlive the goal and its constraints.
        system_prompt += (
            "\n\nCurrent user task (retain its constraints throughout this mission):\n"
            + task
        )
        transcript: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        if opening:


            transcript.append({"role": "user", "content": opening})
        parse_failures = 0
        self._write_checkpoint("running", instruction=task)

        for iteration in range(self.total_step_count + 1, self.max_iterations + 1):
            self.task.current_step = iteration
            self._compact_transcript(transcript, system_prompt=system_prompt, task=task)

            if self._stopped():
                return self._cancelled()
            if self._duration_exhausted():
                return self._exhausted("time")
            self.task.transition(PLANNING, step=iteration)
            self.task.begin_generation_preview(
                "Selecting the next audited action from observed state."
            )
            self._publish_progress()
            started = self._clock()
            if self.generate_with_preview is not None:
                reply = self.generate_with_preview(
                    list(transcript), self._receive_generation_preview
                )
            else:
                reply = self.generate(list(transcript))
            self.task.note_model_call(self._clock() - started)
            self.task.metrics.planning_model_calls += 1
            self.task.finish_generation_preview()
            self.model_turns += 1
            self._publish_progress()

            if self._stopped():
                return self._cancelled()
            if self._duration_exhausted():
                return self._exhausted("time")
            transcript.append({"role": "assistant", "content": reply})
            try:
                action = parse_agent_action(reply, self.capabilities)
            except ValueError as error:
                parse_failures += 1
                self.task.metrics.parse_failures += 1
                if parse_failures >= MAX_PARSE_FAILURES:
                    return self._finish(
                        "failed",
                        "The model did not produce a usable action after "
                        f"{parse_failures} attempts.",
                    )


                transcript.append(
                    {"role": "user", "content": json.dumps({"error": str(error)})}
                )
                self.task.record_event(
                    "planning_repair",
                    step=iteration,
                    error=str(error)[:500],
                )
                self._publish_progress()
                continue
            parse_failures = 0

            if action["action"] == RESPOND_ACTION:
                probe = getattr(self.task, "completion_probe", None)
                if probe is not None:
                    if self._stopped():
                        return self._cancelled()
                    try:
                        verified, evidence = probe({
                            "kind": "action", "status": "completed",
                            "answer": str(action["answer"]), "steps": list(self.steps),
                            "capability": self.steps[-1]["action"] if self.steps else "",
                        })
                    except TaskCancelled:
                        return self._cancelled()
                    except Exception as error:
                        verified, evidence = None, {"error": str(error)[:500]}
                    if self._stopped():
                        return self._cancelled()
                    if verified is not True:
                        self._completion_retries += 1
                        self.task.record_event("goal_not_verified", evidence=evidence)
                        if self._completion_retries >= 3:
                            return self._finish(
                                "failed",
                                "The task is incomplete: the requested result could not be verified. "
                                + self._what_was_achieved(),
                            )
                        transcript.append({"role": "assistant", "content": json.dumps(action, default=str)})
                        transcript.append({
                            "role": "user",
                            "content": "The requested result is not yet verified. Continue with the remaining actions "
                            "or a fresh observation. Do not claim completion. Verification: "
                            + json.dumps(evidence, default=str)[:4000],
                        })
                        self._publish_progress()
                        continue
                self._record(iteration, action, {"status": "completed"})
                return self._finish("completed", str(action["answer"]))

            violation = explicit_constraint_violation(
                task, str(action["action"]), action.get("arguments") or {}
            )
            if violation:
                self._record(
                    iteration,
                    action,
                    {"status": "blocked", "reason": violation},
                )
                transcript.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "status": "blocked_by_explicit_constraint",
                                "error": violation,
                                "note": (
                                    "Choose a different read/navigation capability "
                                    "that obeys the user's stated constraint."
                                ),
                            }
                        ),
                    }
                )
                continue




            achieved = effect_key(action["action"], action["arguments"])
            if achieved is not None and achieved in self._effects:
                self.task.metrics.stagnation_breaks += 1
                self.task.record_event("already_satisfied", effect=achieved)
                self._record(
                    iteration,
                    action,
                    {"status": "already_satisfied", "effect": achieved},
                )
                transcript.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "status": "already_satisfied",
                                "effect": achieved,
                                "note": (
                                    "This is already true from an earlier step. "
                                    "Do something that moves the task forward, "
                                    "or respond if the goal is met."
                                ),
                            }
                        ),
                    }
                )
                self._idle += 1
                if self._idle >= MAX_IDLE_REPEATS:




                    return self._finish(
                        "failed",
                        "The task is incomplete: repeated actions made no further "
                        "progress. " + self._what_was_achieved(),
                    )
                continue





            action["arguments"] = self._semantic_action_arguments(
                action["action"], action["arguments"]
            )
            self.task.current_capability = str(action["action"])
            self.task.record_event(
                "action_selected",
                step=iteration,
                action=action["action"],
                reason=action.get("reason") or "",
            )
            self._publish_progress(
                action=str(action["action"]),
                reason=str(action.get("reason") or ""),
            )
            decision = self.policy.evaluate(action["action"], action["arguments"])
            if decision.outcome == DENY:
                self._record(
                    iteration, action, {"status": "blocked", "reason": decision.reason}
                )
                return self._finish("needs_review", decision.reason)
            if decision.outcome == REQUIRE_APPROVAL:
                approved = (
                    await_approval(decision, ask=self.approve, task=self.task)
                    if self.approve is not None
                    else False
                )
                if not approved:
                    self._record(
                        iteration,
                        action,
                        {"status": "blocked", "reason": decision.reason},
                    )
                    return self._finish(
                        "needs_review",
                        f"{decision.reason} It needs your review before it can "
                        "continue: " + json.dumps(redact(action["arguments"])),
                    )


            if self._stopped():
                return self._cancelled()
            if self._duration_exhausted():
                return self._exhausted("time")



            route = resolve_execution(
                action["action"], action["arguments"], self.capabilities
            )
            self.task.current_capability = route.capability
            self._step_started = self._clock()
            self.task.transition(EXECUTING, capability=route.capability)
            self._publish_progress(
                action=route.capability,
                reason=str(action.get("reason") or ""),
            )
            self.task.note_tool_call(route.capability, route.tier_name)
            if route.capability == SCREEN_CAPTURE_CAPABILITY:
                self.task.note_screenshot()
            try:




                from ..automation.invocation import invoke_capability

                progress_setter = getattr(
                    self.broker, "set_invocation_progress_callback", None
                )
                if callable(progress_setter):
                    progress_setter(self._receive_tool_progress)
                result = invoke_capability(
                    self.broker,
                    route.capability,
                    route.arguments,
                    authority_mode=self.authority_mode,
                    granted=self.capabilities,
                    repair=self._repair_arguments,
                    task=self.task,
                )
                observation = summarise_observation(
                    route.capability,
                    result,
                    describe_screenshot=self.describe_screenshot,
                )
            except Exception as error:




                kind = getattr(error, "kind", None) or type(error).__name__
                observation = {
                    "action": route.capability,
                    "status": "failed",
                    "error": f"{kind}: {error}"[:MAX_OBSERVATION_CHARACTERS],
                }
                result = {"status": "failed"}
            finally:
                progress_setter = getattr(
                    self.broker, "set_invocation_progress_callback", None
                )
                if callable(progress_setter):
                    progress_setter(None)
            if route.capability == DISCORD_INSPECT_CAPABILITY:
                self._note_discord_observation(route.arguments, observation)

            if self._stopped():
                return self._cancelled()
            self.task.transition(OBSERVING, capability=route.capability)
            self._publish_progress(
                action=route.capability,
                reason="Checking the observed result before deciding what comes next.",
            )
            self._record(iteration, action, observation, route=route)


            self.task.world_state.absorb(route.capability, result)
            mission_memory_update: dict[str, Any] = {}
            if self.mission_memory is not None:
                try:
                    mission_memory_update = dict(
                        self.mission_memory.observe_step(
                            task_id=self.task.task_id,
                            capability=route.capability,
                            arguments=route.arguments,
                            result=result,
                            world_state=self.task.world_state.briefing(),
                        )
                        or {}
                    )
                except Exception as error:
                    self.task.record_event(
                        "mission_memory_failed",
                        error=f"{type(error).__name__}: {error}"[:500],
                    )





            if str(observation.get("status") or "") == "succeeded":
                landed = effect_key(route.capability, route.arguments)
                if landed is not None:
                    self._effects.add(landed)






                self._idle = 0

            fingerprint = step_fingerprint(
                route.capability, route.arguments, observation
            )
            self._attempts[fingerprint] = self._attempts.get(fingerprint, 0) + 1
            if self._attempts[fingerprint] >= MAX_IDENTICAL_ATTEMPTS:
                self.task.metrics.stagnation_breaks += 1
                self.task.record_event(
                    "stagnation",
                    capability=route.capability,
                    attempts=self._attempts[fingerprint],
                )
                remaining = [
                    name
                    for name in ordered_capabilities(self.capabilities)
                    if name != route.capability
                ]
                if not remaining:
                    return self._finish(
                        "failed",
                        f"{route.capability} produced the same result "
                        f"{self._attempts[fingerprint]} times and no other "
                        "capability is available.",
                    )


                self.task.metrics.escalations += 1
                transcript.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "error": (
                                    f"{route.capability} has returned the same "
                                    f"result {self._attempts[fingerprint]} times. "
                                    "It is not making progress. Use a different "
                                    "capability or respond explaining what is "
                                    "blocking the task."
                                ),
                                "available": remaining,
                            }
                        ),
                    }
                )
                continue

            self._compact_screenshots(transcript)
            message = dict(observation)


            known = self.task.world_state.briefing()
            if known:
                message["known_state"] = known
            if mission_memory_update.get("locations_observed") or mission_memory_update.get(
                "items_observed"
            ):
                message["durable_mission_memory_update"] = mission_memory_update
                try:
                    memory_briefing = self.mission_memory.briefing(task, limit=8)
                except Exception:
                    memory_briefing = ""
                if memory_briefing:
                    message["durable_mission_memory"] = memory_briefing
            transcript.append(
                {"role": "user", "content": json.dumps(message, sort_keys=True)}
            )
            self._compact_transcript(transcript, system_prompt=system_prompt, task=task)






        return self._exhausted("steps")

    def _exhausted(self, reason: str) -> dict[str, Any]:
        """End a bounded mission truthfully, preserving only observed success."""

        done = [
            str(step.get("reason") or step.get("action") or "")
            for step in self.steps
            if step.get("status") == "succeeded" and step.get("action") != "respond"
        ]
        boundary = (
            f"the {self.max_duration_seconds / 3600:g}-hour mission time budget"
            if reason == "time"
            else f"the {self.max_iterations:,}-decision runaway guard"
        )
        if done:
            summary = (
                f"I reached {boundary} before finishing this, so treat it as "
                "incomplete. What did run: " + "; ".join(done)
            )
        else:
            summary = (
                f"I reached {boundary} before finishing this, and nothing I "
                "tried succeeded."
            )
        return self._finish("exhausted", summary)

    def _what_was_achieved(self) -> str:
        """Report completed actions without claiming the entire task succeeded."""

        done = [
            str(step.get("reason") or step.get("action") or "")
            for step in self.steps
            if step.get("status") == "succeeded" and step.get("action") != "respond"
        ]
        if not done:
            return "No successful actions were recorded."
        return "Actions performed: " + "; ".join(done)

    @staticmethod
    def _compact_screenshots(transcript: list[dict[str, str]]) -> None:
        """Reduce older captures so only the newest view stays in full.

        Every capture would otherwise accumulate, and a twenty-step task can
        exhaust the window before it finishes.  Earlier descriptions are kept
        as a one-line trace rather than dropped outright, because they are the
        record of what the agent has already seen and tried.
        """

        for message in transcript:
            if message["role"] != "user":
                continue
            if '"screenshot_path"' not in message["content"]:
                continue
            try:
                payload = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or "screenshot_path" not in payload:
                continue
            payload["screenshot_path"] = "(superseded by a newer screenshot)"
            previous = str(payload.get("visual_analysis") or "")
            if len(previous) > STALE_ANALYSIS_CHARACTERS:
                payload["visual_analysis"] = (
                    previous[:STALE_ANALYSIS_CHARACTERS].rstrip() + "… (earlier view)"
                )
            message["content"] = json.dumps(payload, sort_keys=True)

    def _compact_transcript(
        self,
        transcript: list[dict[str, str]],
        *,
        system_prompt: str,
        task: str,
    ) -> None:
        """Roll a long mission forward from facts, recent steps, and live state.

        A multi-hour mission cannot carry every historical tool observation into
        every later model call. The complete invocation trail remains in the
        broker audit; this model context keeps the exact goal, current world
        state, recent bounded steps, and the newest conversational turns.
        """

        characters = sum(len(str(message.get("content") or "")) for message in transcript)
        if (
            len(transcript) <= MAX_TRANSCRIPT_MESSAGES
            and characters <= MAX_TRANSCRIPT_CHARACTERS
        ):
            return
        recent = [
            dict(message)
            for message in transcript[max(2, len(transcript) - RECENT_TRANSCRIPT_MESSAGES) :]
        ]
        checkpoint = redact(
            {
                "mission_checkpoint": {
                    "completed_decisions": self.total_step_count,
                    "world_state": self.task.world_state.briefing(),
                    "recent_steps": _bounded_observation_value(self.steps[-RECENT_CHECKPOINT_STEPS:]),
                    "note": (
                        "Earlier calls remain in the durable automation audit. "
                        "Continue from these observed facts without repeating work."
                    ),
                }
            }
        )
        retained = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
            {
                "role": "user",
                "content": json.dumps(checkpoint, sort_keys=True, default=str),
            },
        ]
        # Enforce the advertised bound rather than retaining an arbitrarily
        # large recent tail. Keep newest evidence first, in original order,
        # and explicitly mark a partial message rather than hiding the loss.
        remaining = MAX_TRANSCRIPT_CHARACTERS - sum(len(m["content"]) for m in retained)
        if remaining < MAX_TRANSCRIPT_CHARACTERS // 2:
            retained[2]["content"] = json.dumps({"mission_checkpoint": {
                "completed_decisions": self.total_step_count,
                "note": "Detailed earlier calls remain in the durable automation audit.",
            }})
            remaining = MAX_TRANSCRIPT_CHARACTERS - sum(len(m["content"]) for m in retained)
        tail = []
        marker = "\n[Earlier context omitted; reread specific evidence if needed.]"
        for message in reversed(recent):
            content = str(message.get("content") or "")
            if len(content) > remaining:
                if not tail and remaining > len(marker):
                    tail.append({**message, "content": content[:remaining-len(marker)] + marker})
                break
            tail.append(message)
            remaining -= len(content)
        transcript[:] = [*retained, *reversed(tail)]
        self.task.metrics.context_compactions += 1
        self.task.record_event(
            "context_compacted",
            retained_messages=len(transcript),
            completed_decisions=self.total_step_count,
        )
        self._write_checkpoint("running", instruction=task)

    def _discord_report_evidence(self) -> dict[str, Any]:
        """Aggregate only measured Discord facts for the visible report."""

        inventory: dict[str, Any] = {}
        discoveries: list[dict[str, Any]] = []
        scan_batches: list[dict[str, Any]] = []
        for step in self.steps:
            if str(step.get("action") or "") != DISCORD_INSPECT_CAPABILITY:
                continue
            observation = dict(step.get("observation") or {})
            operation = str(observation.get("operation") or "")
            if operation == "inventory" and observation.get("status") == "succeeded":
                inventory = observation
            elif operation == "find_channels" and observation.get("status") == "succeeded":
                discoveries.append(observation)
            elif operation == "scan_batch" and observation.get("status") == "succeeded":
                scan_batches.append(observation)

        scans = [
            dict(scan)
            for batch in scan_batches
            for scan in list(batch.get("scans") or [])
            if isinstance(scan, Mapping)
        ]
        aggregate_batches = [
            dict(batch.get("scan_aggregate") or {})
            for batch in scan_batches
            if isinstance(batch.get("scan_aggregate"), Mapping)
        ]
        use_batch_aggregates = bool(aggregate_batches) and len(
            aggregate_batches
        ) == len(scan_batches)
        lifecycle = {"active": 0, "ended": 0, "unknown": 0}
        verified_channels: list[dict[str, Any]] = []
        requirement_records = 0
        confirmed_scans = 0
        manual_review_scans = 0
        incomplete_history_scans = 0
        if use_batch_aggregates:
            for aggregate in aggregate_batches:
                counts = dict(aggregate.get("lifecycle") or {})
                for state in lifecycle:
                    try:
                        lifecycle[state] += max(0, int(counts.get(state) or 0))
                    except (TypeError, ValueError):
                        pass
                confirmed_scans += max(
                    0, int(aggregate.get("confirmed_channel_count") or 0)
                )
                manual_review_scans += max(
                    0, int(aggregate.get("manual_review_channel_count") or 0)
                )
                requirement_records += max(
                    0, int(aggregate.get("requirement_record_count") or 0)
                )
                incomplete_history_scans += max(
                    0, int(aggregate.get("incomplete_history_channel_count") or 0)
                )
        for scan in scans:
            if not use_batch_aggregates:
                counts = dict(scan.get("giveaway_state_counts") or {})
                for state in lifecycle:
                    try:
                        lifecycle[state] += max(0, int(counts.get(state) or 0))
                    except (TypeError, ValueError):
                        pass
                if scan.get("candidate_classification") == "confirmed_giveaway":
                    confirmed_scans += 1
                if scan.get("criteria_status") in {
                    "needs_manual_review",
                    "requirements_unverified",
                }:
                    manual_review_scans += 1
                try:
                    requirement_records += max(
                        0, int(scan.get("unique_requirement_count") or 0)
                    )
                except (TypeError, ValueError):
                    pass
                message_coverage = dict(
                    ((scan.get("coverage") or {}).get("messages") or {})
                )
                incomplete_history_scans += not bool(
                    message_coverage.get("history_boundary_reached")
                )
            selected = dict(scan.get("selected_channel") or {})
            if selected:
                verified_channels.append(
                    {
                        key: selected.get(key)
                        for key in (
                            "server",
                            "channel",
                            "channel_type",
                            "readback_server",
                            "readback_channel",
                            "readback_matches_channel_key",
                        )
                        if selected.get(key) is not None
                    }
                )

        last_batch = scan_batches[-1] if scan_batches else {}
        account = dict(inventory.get("account") or {})
        if not account and discoveries:
            account = dict(discoveries[-1].get("account") or {})
        discovered_channels: dict[str, dict[str, Any]] = {}
        for discovery in discoveries:
            for channel in list(discovery.get("channels") or []):
                if not isinstance(channel, Mapping):
                    continue
                key = str(
                    channel.get("channel_key")
                    or f"{channel.get('server')}::{channel.get('channel')}"
                )
                discovered_channels[key] = dict(channel)
        discovered_servers = {
            str(channel.get("server") or "").strip()
            for channel in discovered_channels.values()
            if str(channel.get("server") or "").strip()
        }
        discovered_channel_count = max(
            [
                int(discovery.get("candidate_index_size") or discovery.get("channel_count") or 0)
                for discovery in discoveries
            ]
            or [len(discovered_channels)]
        )
        discovered_server_count = max(
            [int(discovery.get("candidate_server_count") or 0) for discovery in discoveries]
            or [len(discovered_servers)]
        )
        complete_inventory = bool(self._discord_inventory_complete)
        verified_channel_count = sum(
            max(0, int(batch.get("scan_count") or len(batch.get("scans") or [])))
            for batch in scan_batches
        )
        scan_gap_count = sum(
            len(list(batch.get("scan_gaps") or [])) for batch in scan_batches
        )
        cursor_done = bool(last_batch.get("done"))
        return {
            "account": {
                "observed": bool(account.get("observed")),
                "label": str(account.get("label") or account.get("handle") or "")[:160],
            },
            "inventory": {
                "servers": (
                    int(inventory.get("server_count") or len(inventory.get("servers") or []))
                    if inventory
                    else max(discovered_server_count, len(discovered_servers))
                ),
                "channels": (
                    int(inventory.get("channel_count") or len(inventory.get("channels") or []))
                    if inventory
                    else max(discovered_channel_count, len(discovered_channels))
                ),
                "discovery_candidates": int(
                    inventory.get("discovery_candidate_count")
                    or max(discovered_channel_count, len(discovered_channels))
                ),
                "complete": complete_inventory,
                "scope": (
                    "complete_account_inventory"
                    if complete_inventory
                    else "task_targeted_live_candidates"
                ),
                "gap_count": len(list(inventory.get("inventory_gaps") or [])),
            },
            "content_scan": {
                "verified_channels": verified_channels[:6],
                "verified_channel_count": verified_channel_count,
                "confirmed_channel_count": confirmed_scans,
                "manual_review_channel_count": manual_review_scans,
                "lifecycle": lifecycle,
                "requirement_record_count": requirement_records,
                "cursor": int(last_batch.get("cursor") or 0),
                "next_cursor": int(last_batch.get("next_cursor") or 0),
                "cursor_done": cursor_done,
                "done": bool(
                    cursor_done
                    and scan_gap_count == 0
                    and incomplete_history_scans == 0
                ),
                "gap_count": scan_gap_count,
                "incomplete_history_channel_count": incomplete_history_scans,
            },
            "mutations": {
                "messages_sent": 0,
                "reactions_added": 0,
                "giveaways_joined": 0,
                "accounts_changed": 0,
            },
        }

    @staticmethod
    def _discord_report_fallback(evidence: Mapping[str, Any]) -> str:
        account = dict(evidence.get("account") or {})
        inventory = dict(evidence.get("inventory") or {})
        scan = dict(evidence.get("content_scan") or {})
        lifecycle = dict(scan.get("lifecycle") or {})
        server_count = int(inventory.get("servers") or 0)
        channel_count = int(inventory.get("channels") or 0)
        verified_count = int(scan.get("verified_channel_count") or 0)
        account_text = (
            f" for the signed-in account {account.get('label')}"
            if account.get("label")
            else " for the currently signed-in account"
        )
        complete_inventory = bool(inventory.get("complete"))
        discovery_sentence = (
            (
                f"The complete dynamic inventory contains {server_count:,} "
                f"server{'' if server_count == 1 else 's'} and {channel_count:,} "
                f"channel{'' if channel_count == 1 else 's'}; "
                f"{int(inventory.get('discovery_candidates') or 0):,} were prioritized "
                "as content-discovery candidates."
            )
            if complete_inventory
            else (
                f"The fast task-targeted search found {channel_count:,} candidate "
                f"channel{'' if channel_count == 1 else 's'} across {server_count:,} "
                f"observed server{'' if server_count == 1 else 's'}. This is not a "
                "complete inventory of every joined server or channel."
            )
        )
        report = [
            f"I completed a fresh read-only Discord inspection{account_text}.",
            discovery_sentence,
            (
                f"I opened and read back {verified_count:,} "
                f"channel{'' if verified_count == 1 else 's'} in this pass. "
                "Their observed giveaway items included "
                f"{int(lifecycle.get('active') or 0):,} active, "
                f"{int(lifecycle.get('ended') or 0):,} ended, and "
                f"{int(lifecycle.get('unknown') or 0):,} with an unverified state."
            ),
            (
                f"I recorded {int(scan.get('requirement_record_count') or 0):,} "
                "requirement signals; unknown or unparsed requirements remain manual review."
            ),
        ]
        if not scan.get("done"):
            report.append(
                f"Coverage is incomplete: the resumable scan cursor advanced to "
                f"{int(scan.get('next_cursor') or 0):,}, and more candidate channels remain."
            )
        if int(scan.get("gap_count") or 0):
            report.append(
                f"There were {int(scan.get('gap_count') or 0):,} bounded UI read gaps; "
                "they were not treated as negative results."
            )
        if int(scan.get("incomplete_history_channel_count") or 0):
            report.append(
                f"Message-history coverage remained partial in "
                f"{int(scan.get('incomplete_history_channel_count') or 0):,} "
                "channel(s); older long-running events outside those observed "
                "boundaries were not treated as absent."
            )
        report.append(
            "I did not send messages, add reactions, join giveaways, or change accounts."
        )
        return "\n\n".join(report)

    @staticmethod
    def _discord_report_is_usable(
        answer: str,
        evidence: Mapping[str, Any],
    ) -> bool:
        text = strip_reasoning(str(answer or "")).strip()
        if not text or text.startswith(("{", "[", "```")):
            return False
        if not text.endswith((".", "!", "?")):
            return False
        lowered = text.casefold()
        if "<think" in lowered:
            return False
        inventory = dict(evidence.get("inventory") or {})
        for count in (
            int(inventory.get("servers") or 0),
            int(inventory.get("channels") or 0),
        ):
            if count and str(count) not in text.replace(",", ""):
                return False
        scan = dict(evidence.get("content_scan") or {})
        verified_channels = int(scan.get("verified_channel_count") or 0)







        allowed_channel_counts = {
            value
            for value in (
                int(inventory.get("channels") or 0),
                verified_channels,
            )
            if value >= 0
        }
        explicit_channel_counts = {
            int(match.group(1).replace(",", ""))
            for match in re.finditer(
                r"\b(\d[\d,]*)\b(?:\s+[a-z-]+){0,3}\s+channels?\b",
                lowered,
            )
        }
        if explicit_channel_counts - allowed_channel_counts:
            return False
        if not scan.get("done") and not any(
            marker in lowered
            for marker in ("incomplete", "remain", "partial", "not finished")
        ):
            return False
        if any(
            claim in lowered
            for claim in (
                "i joined the giveaway",
                "i entered the giveaway",
                "i sent a message",
                "i changed account",
            )
        ):
            return False
        return any(
            marker in lowered
            for marker in ("active", "ended", "manual review", "coverage")
        )

    def _complete_primed_discord_report(self, task: str) -> dict[str, Any]:
        """Turn successful Discord evidence into a visible, non-JSON answer."""

        evidence = self._discord_report_evidence()
        fallback = self._discord_report_fallback(evidence)
        iteration = self.total_step_count + 1
        self.task.current_step = iteration
        self.task.current_capability = DISCORD_INSPECT_CAPABILITY
        self.task.transition(PLANNING, step=iteration)
        self.task.begin_generation_preview(
            "Writing the verified Discord report from observed evidence."
        )
        self._step_started = self._clock()
        answer = ""
        generation_error = ""
        if self.generate_final_with_preview is not None:
            messages = [
                {"role": "system", "content": DISCORD_REPORT_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"request": task, "observed_evidence": evidence},
                        sort_keys=True,
                        default=str,
                    ),
                },
            ]
            started = self._clock()
            try:
                answer = self.generate_final_with_preview(
                    messages, self._receive_discord_report_preview
                )
            except Exception as error:
                generation_error = f"{type(error).__name__}: {error}"[:500]
            else:
                self.task.note_model_call(self._clock() - started)
                self.task.metrics.planning_model_calls += 1
                self.model_turns += 1
        answer = strip_reasoning(str(answer or "")).strip()
        model_report_used = self._discord_report_is_usable(answer, evidence)
        if not model_report_used:
            answer = fallback
        self.task.finish_generation_preview()
        self.task.record_event(
            "discord_report_composed",
            step=iteration,
            model_report_used=model_report_used,
            fallback_used=not model_report_used,
            generation_error=generation_error or None,
            verified_channel_count=(evidence.get("content_scan") or {}).get(
                "verified_channel_count"
            ),
            scan_complete=(evidence.get("content_scan") or {}).get("done"),
        )
        action = {
            "action": RESPOND_ACTION,
            "reason": "Reporting only the Discord evidence observed in this pass.",
            "answer": answer,
        }
        self._record(iteration, action, {"status": "completed"})
        return self._finish("completed", answer)

    def _duration_exhausted(self) -> bool:
        return self._clock() - self._mission_started >= self.max_duration_seconds

    def _prime_discord_content(
        self,
        task: str,
        transcript: list[dict[str, str]],
    ) -> None:
        """Acquire task-scoped Discord evidence before model replanning."""

        exhaustive = requires_discord_exhaustive_content_scan(task)
        if exhaustive:
            scripted = [
                (
                    {
                        "operation": "inventory",
                        "limit": DISCORD_EXHAUSTIVE_MAX_CHANNELS,
                        "max_scrolls": DISCORD_EXHAUSTIVE_INVENTORY_SCROLLS,
                    },
                    (
                        "Building the complete current-account Discord server "
                        "and channel inventory before reading content."
                    ),
                )
            ]
            scripted.extend(
                (
                    {
                        "operation": "scan_batch",
                        "cursor": cursor,
                        "batch_limit": DISCORD_EXHAUSTIVE_BATCH_SIZE,
                        "limit": DISCORD_EXHAUSTIVE_MESSAGE_LIMIT,
                        "max_scrolls": DISCORD_EXHAUSTIVE_SCROLLS,
                    },
                    (
                        f"Reading indexed Discord channels {cursor + 1:,} through "
                        f"{cursor + DISCORD_EXHAUSTIVE_BATCH_SIZE:,} for lifecycle "
                        "and requirement evidence."
                    ),
                )
                for cursor in range(
                    0,
                    DISCORD_EXHAUSTIVE_MAX_CHANNELS,
                    DISCORD_EXHAUSTIVE_BATCH_SIZE,
                )
            )
        else:
            queries = discord_discovery_queries(task)
            scripted = [
                (
                    {
                        "operation": "find_channels",
                        "query": query,
                        "limit": 500,
                        "max_scrolls": 20,
                    },
                    (
                        f"Discovering live Discord channel candidates for {query!r}."
                        if query
                        else "Discovering the first live Discord channel candidates."
                    ),
                )
                for query in queries
            ]
            scripted.append(
                (
                    {
                        "operation": "scan_batch",
                        "cursor": 0,
                        "batch_limit": 3,
                        "limit": 60,
                        "max_scrolls": 4,
                    },
                    "Opening and reading the first account-bound candidate channels.",
                )
            )
        for arguments, reason in scripted:
            if self._stopped() or self._duration_exhausted():
                return
            if (
                arguments["operation"] == "scan_batch"
                and not self._discord_inventory_complete
                and not self._discord_candidate_index_ready
            ):
                return
            iteration = self.total_step_count + 1
            action = {
                "action": DISCORD_INSPECT_CAPABILITY,
                "reason": reason,
                "arguments": arguments,
                "answer": None,
            }
            decision = self.policy.evaluate(action["action"], action["arguments"])
            if decision.outcome == DENY:
                self._record(
                    iteration,
                    action,
                    {"status": "blocked", "reason": decision.reason},
                )
                return
            if decision.outcome == REQUIRE_APPROVAL:
                approved = (
                    await_approval(decision, ask=self.approve, task=self.task)
                    if self.approve is not None
                    else False
                )
                if not approved:
                    self._record(
                        iteration,
                        action,
                        {"status": "blocked", "reason": decision.reason},
                    )
                    return

            route = resolve_execution(
                action["action"], action["arguments"], self.capabilities
            )
            self.task.current_step = iteration
            self.task.current_capability = route.capability
            self.task.transition(PLANNING, step=iteration)
            self._step_started = self._clock()
            self.task.transition(EXECUTING, capability=route.capability)
            self._publish_progress(action=route.capability, reason=reason)
            self.task.note_tool_call(route.capability, route.tier_name)
            try:
                from ..automation.invocation import invoke_capability

                progress_setter = getattr(
                    self.broker, "set_invocation_progress_callback", None
                )
                if callable(progress_setter):
                    progress_setter(self._receive_tool_progress)
                result = invoke_capability(
                    self.broker,
                    route.capability,
                    route.arguments,
                    authority_mode=self.authority_mode,
                    granted=self.capabilities,
                    repair=None,
                    task=self.task,
                )
                observation = summarise_observation(route.capability, result)
            except Exception as error:
                kind = getattr(error, "kind", None) or type(error).__name__
                result = {"status": "failed"}
                observation = {
                    "action": route.capability,
                    "status": "failed",
                    "error": f"{kind}: {error}"[:MAX_OBSERVATION_CHARACTERS],
                }
            finally:
                progress_setter = getattr(
                    self.broker, "set_invocation_progress_callback", None
                )
                if callable(progress_setter):
                    progress_setter(None)

            self._note_discord_observation(route.arguments, observation)
            self.task.transition(OBSERVING, capability=route.capability)
            self._record(iteration, action, observation, route=route)
            self.task.world_state.absorb(route.capability, result)
            mission_update: dict[str, Any] = {}
            if self.mission_memory is not None:
                try:
                    mission_update = dict(
                        self.mission_memory.observe_step(
                            task_id=self.task.task_id,
                            capability=route.capability,
                            arguments=route.arguments,
                            result=result,
                            world_state=self.task.world_state.briefing(),
                        )
                        or {}
                    )
                except Exception as error:
                    self.task.record_event(
                        "mission_memory_failed",
                        error=f"{type(error).__name__}: {error}"[:500],
                    )
            message = dict(observation)
            if mission_update.get("locations_observed") or mission_update.get(
                "items_observed"
            ):
                message["durable_mission_memory_update"] = mission_update
            transcript.append(
                {"role": "user", "content": json.dumps(message, sort_keys=True)}
            )
            if str(observation.get("status") or "").casefold() != "succeeded":
                return
            if (
                exhaustive
                and arguments["operation"] == "scan_batch"
                and bool(observation.get("done"))
            ):
                break
        transcript.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "status": "fast_discord_evidence_ready",
                        "note": (
                            "The complete current-account inventory and exhaustive "
                            "content scan reached the final cursor. Report any UI "
                            "read gaps explicitly."
                            if exhaustive
                            and self._discord_inventory_complete
                            and bool(observation.get("done"))
                            else "The task-targeted live candidate search and first "
                            "content batch are ready. Report the measured scope "
                            "as partial unless a complete inventory was separately "
                            "proved; request scan_batch at next_cursor only when "
                            "more candidate coverage is necessary."
                        ),
                    }
                ),
            }
        )

    def _note_discord_observation(
        self,
        arguments: Mapping[str, Any],
        observation: Mapping[str, Any],
    ) -> None:
        discord_status = str(observation.get("status") or "").casefold()
        discord_operation = str(arguments.get("operation") or "").casefold()
        if discord_status == "succeeded" and discord_operation == "inventory":
            coverage = dict(observation.get("coverage") or {})
            server_coverage = dict(coverage.get("servers") or {})
            channel_coverage = dict(coverage.get("channels") or {})
            account = dict(observation.get("account") or {})
            self._discord_inventory_complete = bool(
                account.get("observed")
                and server_coverage.get("scroll_boundary_reached")
                and channel_coverage.get("scroll_boundary_reached")
                and not channel_coverage.get("truncated_by_limit")
                and not (observation.get("inventory_gaps") or [])
            )
        if discord_status == "succeeded" and discord_operation == "find_channels":
            self._discord_candidate_index_ready = bool(observation.get("channels"))
        if discord_status == "succeeded" and discord_operation == "scan_batch":
            self._discord_scan_started = True
            try:
                self._discord_scan_cursor = max(
                    self._discord_scan_cursor,
                    int(observation.get("next_cursor") or 0),
                )
            except (TypeError, ValueError):
                pass
        self._discord_inspector_failed = discord_status != "succeeded"

    def _receive_generation_preview(self, value: Mapping[str, Any]) -> None:
        preview = {
            "token_count": value.get("token_count"),
            "character_count": value.get("character_count"),
            "summary": "Selecting the next audited action from observed state.",
        }
        self.task.note_generation_preview(preview)
        self._publish_progress()

    def _receive_discord_report_preview(self, value: Mapping[str, Any]) -> None:
        preview = {
            "token_count": value.get("token_count"),
            "character_count": value.get("character_count"),
            "summary": "Writing the verified Discord report from observed evidence.",
        }
        self.task.note_generation_preview(preview)
        self._publish_progress(
            action=DISCORD_INSPECT_CAPABILITY,
            reason="Writing the verified Discord report from observed evidence.",
        )

    def _receive_tool_progress(self, value: Mapping[str, Any]) -> None:
        summary = str(value.get("summary") or "Working in the application")[:240]
        progress_state = str(value.get("state") or "")
        if progress_state == "waiting_for_user_idle":
            self.task.transition(
                WAITING,
                waiting_for="user_idle",
                summary=summary,
                resume_after_idle_seconds=value.get("resume_after_idle_seconds"),
            )
        elif progress_state == "resumed_after_user_idle" and self.task.state == WAITING:
            self.task.transition(
                EXECUTING,
                capability=self.task.current_capability,
                resumed_from="user_idle",
            )
        self.task.note_tool_progress(value)
        self.task.record_event(
            "compound_capability_progress",
            capability=self.task.current_capability,
            summary=summary,
            completed=value.get("completed"),
            total=value.get("total"),
        )
        self._publish_progress(
            action=self.task.current_capability or "",
            reason=summary,
        )

    def _publish_progress(self, *, action: str = "", reason: str = "") -> None:
        if self.on_progress is None:
            return
        snapshot = self.task.snapshot()
        snapshot.update(
            {
                "step": self.task.current_step,
                "action": action or self.task.current_capability or "",
                "reason": reason,
                "steps": list(self.steps),
                "step_count": self.total_step_count,
                "steps_truncated": self.total_step_count > len(self.steps),
            }
        )
        try:
            self.on_progress(snapshot)
        except BaseException:

            return

    def _write_checkpoint(self, state: str, *, instruction: str = "") -> None:
        if self.checkpoint_path is None:
            return
        payload = redact(
            {
                "schema": MISSION_CHECKPOINT_SCHEMA,
                "task_id": self.task.task_id,
                "state": state,
                "instruction": str(instruction or self.task.goal),
                "authority_mode": self.authority_mode,
                "capabilities": ordered_capabilities(self.capabilities),
                "total_step_count": self.total_step_count,
                "steps_truncated": self.total_step_count > len(self.steps),
                "retained_steps": list(self.steps),
                "task": self.task.snapshot(include_events=True),
            }
        )
        try:
            atomic_write_json(self.checkpoint_path, payload)
        except BaseException as error:
            self.task.record_event(
                "checkpoint_failed",
                error=f"{type(error).__name__}: {error}"[:500],
            )

    def _stopped(self) -> bool:
        """One cancellation check for every checkpoint in the loop.

        Reads the in-memory token first so a stop already accepted costs
        nothing, and folds in a caller-supplied predicate exactly once.
        """

        if self.task.stop_requested:
            return True
        if self.should_stop is not None and self.should_stop():
            self.task.request_stop("user_requested")
            return True
        return False

    def _repair_arguments(self, brief: str) -> Mapping[str, Any] | None:
        """One correction, from the model, given the capability's contract."""

        reply = self.generate(
            [
                {
                    "role": "system",
                    "content": (
                        "That tool call was rejected. Reply with one JSON "
                        "object holding only the corrected arguments - no "
                        "prose, no capability name, no explanation."
                    ),
                },
                {"role": "user", "content": brief},
            ]
        )
        from .actions import _whole_json_object
        from .orchestrator import strip_reasoning

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        if not isinstance(parsed, Mapping):
            return None
        inner = parsed.get("arguments")
        return inner if isinstance(inner, Mapping) else parsed

    def _semantic_action_arguments(
        self,
        capability: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Carry an observed control name beside an otherwise opaque handle.

        The approval policy cannot know that ``web-el-7`` is the Gmail Send
        button. The structured read that produced the handle did know, and the
        task world state retained that fact. Reattaching the observed name
        closes the gap without guessing from coordinates or trusting a model to
        classify the consequence of its own action.
        """

        checked = dict(arguments)
        world_state = getattr(self.task, "world_state", None)
        if capability == INPUT_CONTROL_CAPABILITY and world_state is not None:
            try:
                active = world_state.get("active_window") or {}
            except Exception:
                active = {}
            if isinstance(active, Mapping):
                handle = active.get("window_handle") or active.get("handle")
                process_id = active.get("process_id")
                if handle and not checked.get("expected_window_handle"):
                    checked["expected_window_handle"] = int(handle)
                if process_id and not checked.get("expected_process_id"):
                    checked["expected_process_id"] = int(process_id)
            return checked
        if checked.get("name") or not checked.get("element"):
            return checked
        state_name = (
            "page_elements"
            if capability == BROWSER_CAPABILITY
            else "ui_elements"
            if capability == UI_AUTOMATION_CAPABILITY
            else None
        )
        if state_name is None:
            return checked
        if world_state is None:
            return checked
        try:
            observed = world_state.get(state_name) or []
        except Exception:
            return checked
        handle = str(checked["element"])
        for item in observed:
            if not isinstance(item, Mapping) or str(item.get("element") or "") != handle:
                continue
            name = str(item.get("name") or "").strip()
            if name:
                checked["name"] = name
            break
        return checked

    def _cancelled(self) -> dict[str, Any]:
        self.task.finish_stopped()
        return self._finish("cancelled", "The task was stopped before it finished.")

    def _record(
        self,
        iteration: int,
        action: Mapping[str, Any],
        observation: Mapping[str, Any],
        *,
        route: Any | None = None,
    ) -> None:
        step = {
            "step": iteration,
            "action": action["action"],
            "reason": action.get("reason") or "",
            "arguments": dict(
                route.arguments if route is not None else action.get("arguments") or {}
            ),
            "status": observation.get("status"),
            "observation": dict(observation),

            "route": execution_route_record(route) if route is not None else None,


            "duration_ms": round((self._clock() - self._step_started) * 1000, 1),
            "started_at": self._step_started,
        }
        self.total_step_count += 1
        self.steps.append(step)
        if len(self.steps) > MAX_RETAINED_STEPS:
            del self.steps[: len(self.steps) - MAX_RETAINED_STEPS]
        self.task.record_event(
            "agent_step",
            step=iteration,
            action=step["action"],
            status=step["status"],
            reason=step["reason"],
            audit_record_id=(step["observation"] or {}).get("audit_record_id"),
        )
        self._write_checkpoint("running")
        if self.on_step is not None:
            self.on_step(dict(step))
        self._publish_progress(action=str(step["action"]), reason=str(step["reason"]))

    def _finish(self, state: str, answer: str) -> dict[str, Any]:

        if not self.task.finished:
            self.task.finish(
                {
                    "completed": "completed",
                    "cancelled": "stopped",
                    "failed": "failed",
                    "needs_review": "waiting",
                    "exhausted": "failed",
                }.get(state, "completed"),
                failure=answer if state in {"failed", "exhausted"} else None,
            )
        result = {
            "schema": AGENT_SCHEMA,
            "state": state,
            "answer": answer,
            "steps": list(self.steps),
            "step_count": self.total_step_count,
            "steps_truncated": self.total_step_count > len(self.steps),
            "authority_mode": self.authority_mode,
            "capabilities": ordered_capabilities(self.capabilities),


            "model_turns": self.model_turns,
            "tiers_used": sorted(
                {
                    str((step.get("route") or {}).get("tier"))
                    for step in self.steps
                    if step.get("route")
                }
            ),
            "task": self.task.snapshot(),
            "metrics": self.task.metrics.to_dict(),
        }
        self._write_checkpoint(state)
        self._publish_progress()
        end_task = getattr(self.broker, "end_agent_task", None)
        if callable(end_task):
            end_task(self.task.task_id)
        return result
