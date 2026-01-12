"""Salty Steak Native Desktop AI Platform — execution routing and capability tiers.

The model is the orchestrator.  It reads the request, decides *what* should
happen, and emits a structured action.  This module decides *how* that action
is carried out:

    model action
      -> capability registry
      -> argument resolution
      -> native / api / terminal / window
      -> vision, only when sight is required
      -> raw synthetic input, last

Each rung down is slower, less reliable, and harder for a person to audit than
the one above it.  Clicking a coordinate read off a screenshot is the least
trustworthy way to accomplish anything, so it is what the system reaches for
last rather than first.

Nothing here decides what the user wanted.  Routing that replaced the model's
understanding would make the product a command launcher with a chat window
attached: it would answer "open youtube" and be helpless at "open the video I
was watching yesterday".  The model keeps the intent; the runtime keeps the
execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .broker import (
    APPLICATION_LAUNCH_CAPABILITY,
    INPUT_CONTROL_CAPABILITY,
    SCREEN_CAPTURE_CAPABILITY,
    TERMINAL_CAPABILITY,
    WINDOW_CONTROL_CAPABILITY,
    UI_AUTOMATION_CAPABILITY,
    BROWSER_CAPABILITY,
)


ROUTING_SCHEMA = "salty-steak-automation-routing-v1"



TIER_NATIVE = 10
TIER_API = 20
TIER_TERMINAL = 30
TIER_WINDOW = 40
TIER_UI_AUTOMATION = 45
TIER_VISION = 50
TIER_RAW_INPUT = 60

TIER_NAMES = {
    TIER_NATIVE: "native",
    TIER_API: "api",
    TIER_TERMINAL: "terminal",
    TIER_WINDOW: "window",
    TIER_UI_AUTOMATION: "ui_automation",
    TIER_VISION: "vision",
    TIER_RAW_INPUT: "raw_input",
}


@dataclass(frozen=True)
class CapabilityTier:
    """One rung of the ladder."""

    capability: str
    tier: int
    summary: str
    requires_sight: bool = False

    @property
    def tier_name(self) -> str:
        return TIER_NAMES[self.tier]


CAPABILITY_TIERS = (
    CapabilityTier(
        APPLICATION_LAUNCH_CAPABILITY,
        TIER_NATIVE,
        "Open an application, link, or file through its registered handler.",
    ),
    CapabilityTier(
        BROWSER_CAPABILITY,
        TIER_API,
        "Read and operate web pages structurally in a Salty-owned session.",
    ),
    CapabilityTier(
        TERMINAL_CAPABILITY,
        TIER_TERMINAL,
        "Run a command and read its exact output.",
    ),
    CapabilityTier(
        WINDOW_CONTROL_CAPABILITY,
        TIER_WINDOW,
        "Find, focus, and close windows by their real titles.",
    ),
    CapabilityTier(
        UI_AUTOMATION_CAPABILITY,
        TIER_UI_AUTOMATION,
        "Read and operate the controls inside an application semantically.",
    ),
    CapabilityTier(
        SCREEN_CAPTURE_CAPABILITY,
        TIER_VISION,
        "Look at the screen when no structured route exists.",
        requires_sight=True,
    ),
    CapabilityTier(
        INPUT_CONTROL_CAPABILITY,
        TIER_RAW_INPUT,
        "Move the pointer and send keystrokes when nothing better applies.",
        requires_sight=True,
    ),
)

CAPABILITY_TIER_BY_ID = {item.capability: item for item in CAPABILITY_TIERS}


def ordered_capabilities(granted: Sequence[str]) -> list[str]:
    """Return the granted capabilities in ladder order, best route first."""

    known = [item for item in CAPABILITY_TIERS if item.capability in set(granted)]
    return [item.capability for item in sorted(known, key=lambda item: item.tier)]


def requires_sight(capabilities: Sequence[str]) -> bool:
    """Report whether any granted capability actually needs the screen.

    Vision is expensive: a description costs a full model pass over an image.
    It is only worth preparing when the task can reach a rung that needs it.
    """

    return any(
        CAPABILITY_TIER_BY_ID[capability].requires_sight
        for capability in capabilities
        if capability in CAPABILITY_TIER_BY_ID
    )


@dataclass(frozen=True)
class ExecutionRoute:
    """How an action the model asked for will actually be carried out."""

    capability: str
    arguments: dict[str, Any]
    tier: int
    resolution: str
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tier_name(self) -> str:
        return TIER_NAMES[self.tier]





KNOWN_SITES = {
    "youtube": "https://www.youtube.com",
    "you tube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://www.wikipedia.org",
    "twitter": "https://twitter.com",
    "x.com": "https://x.com",
    "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "chatgpt": "https://chat.openai.com",
    "outlook": "https://outlook.live.com",
    "drive": "https://drive.google.com",
    "maps": "https://maps.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "spotify": "https://open.spotify.com",
}



KNOWN_APPLICATIONS = {
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "paint": "mspaint",
    "file explorer": "explorer",
    "explorer": "explorer",
    "task manager": "taskmgr",
    "control panel": "control",
    "command prompt": "cmd",
    "registry editor": "regedit",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "chrome": "chrome",
    "firefox": "firefox",
    "discord": "discord",
    "spotify app": "spotify",
    "vs code": "code",
    "visual studio code": "code",
    "vscode": "code",
    "code": "code",
    "terminal": "wt",
    "windows terminal": "wt",
}

_URL_PREFIXES = ("http://", "https://")


def _normalise(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def normalise_launch_target(value: str) -> tuple[str, str]:
    """Turn a loosely named target into an exact one.

    The model is good at knowing the user meant YouTube and poor at reliably
    recalling an exact URL, so the name it emits is resolved here against known
    destinations rather than trusted verbatim or guessed by the model.
    """

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("A launch target is required")
    lowered = raw.casefold()
    if lowered.startswith(_URL_PREFIXES):
        return raw, "explicit_url"
    if lowered.startswith("www."):
        return f"https://{raw}", "explicit_url"
    key = _normalise(raw)
    if key in KNOWN_SITES:
        return KNOWN_SITES[key], "known_site"
    if key in KNOWN_APPLICATIONS:
        return KNOWN_APPLICATIONS[key], "known_application"


    return raw, "verbatim"


def resolve_execution(
    capability: str,
    arguments: Mapping[str, Any],
    granted: Sequence[str],
) -> ExecutionRoute:
    """Decide how to carry out an action the model has already chosen.

    This never changes which capability the model asked for.  It resolves
    arguments and reports the tier so the runtime, the audit trail, and the
    activity panel all describe the same execution.
    """

    resolved = dict(arguments)
    resolution = "verbatim"
    notes: list[str] = []

    if capability == APPLICATION_LAUNCH_CAPABILITY:
        target = resolved.get("target")
        if isinstance(target, str) and target.strip():
            exact, resolution = normalise_launch_target(target)
            if exact != target:
                notes.append(f"resolved {target!r} to {exact}")
            resolved["target"] = exact


            if resolution == "known_application" and "wait_ms" not in resolved:
                resolved["wait_ms"] = 600

    tier_entry = CAPABILITY_TIER_BY_ID.get(capability)
    if tier_entry is None:


        return ExecutionRoute(
            capability=capability,
            arguments=resolved,
            tier=TIER_RAW_INPUT,
            resolution=resolution,
            notes=tuple(notes),
        )
    if capability not in set(granted):
        notes.append("capability is not currently granted")
    return ExecutionRoute(
        capability=capability,
        arguments=resolved,
        tier=tier_entry.tier,
        resolution=resolution,
        notes=tuple(notes),
    )


def describe_routing(granted: Sequence[str]) -> dict[str, Any]:
    """Report the active ladder for diagnostics and UI surfaces."""

    ordered = ordered_capabilities(granted)
    return {
        "schema": ROUTING_SCHEMA,
        "ladder": [
            {
                "capability": capability,
                "tier": CAPABILITY_TIER_BY_ID[capability].tier_name,
                "rank": CAPABILITY_TIER_BY_ID[capability].tier,
                "summary": CAPABILITY_TIER_BY_ID[capability].summary,
                "requires_sight": CAPABILITY_TIER_BY_ID[capability].requires_sight,
            }
            for capability in ordered
        ],
        "sight_reachable": requires_sight(ordered),
        "deterministic_site_count": len(KNOWN_SITES),
        "deterministic_application_count": len(KNOWN_APPLICATIONS),
    }


def execution_route_record(route: ExecutionRoute) -> dict[str, Any]:
    """Public record for the audit trail and the activity panel."""

    return {
        "capability": route.capability,
        "tier": route.tier_name,
        "resolution": route.resolution,
        "notes": list(route.notes),
    }


def merge_capability_manifest(
    descriptions: Mapping[str, str],
    granted: Sequence[str],
) -> list[str]:
    """Order the model-facing tool manifest by the ladder, best route first."""

    return [
        descriptions[capability]
        for capability in ordered_capabilities(granted)
        if capability in descriptions
    ]
