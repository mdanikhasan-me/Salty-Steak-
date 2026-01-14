"""Salty Steak Native Desktop AI Platform — short-term structured world state.

What the runtime already knows about this computer, right now, for one task.

Without it every step re-discovers the same facts: which window is open, which
tab is loaded, where an executable lives. That is slow, and worse, it spends
reasoning turns on questions the runtime can already answer. This is execution
memory, not knowledge: it lives and dies with the task, and it is separate from
anything durable the user has approved remembering.

Entries carry an age and a volatility, because a fact about the desktop is only
useful while it is still true. An action that could have invalidated a slot
clears it rather than letting the model act on a stale belief.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

WORLD_STATE_SCHEMA = "salty-steak-world-state-v1"



STABLE = "stable"
SEMI_STABLE = "semi_stable"
VOLATILE = "volatile"

FRESHNESS_SECONDS = {
    STABLE: 3_600.0,
    SEMI_STABLE: 60.0,
    VOLATILE: 5.0,
}



INVALIDATED_BY = {
    "application.launch": ("windows", "active_window", "processes"),
    "window.control": ("windows", "active_window"),
    "ui.automation": ("active_window", "ui_elements"),
    "browser.control": ("browser_tabs", "current_url", "page_elements"),
    "input.control": ("active_window", "ui_elements", "page_elements"),
    "terminal.execute": ("processes", "files"),
}

SLOT_VOLATILITY = {
    "resolved_executables": STABLE,
    "installed_applications": STABLE,
    "default_browser": STABLE,
    "known_folders": STABLE,
    "working_directory": STABLE,
    "browser_profile": STABLE,
    "processes": SEMI_STABLE,
    "windows": SEMI_STABLE,
    "browser_tabs": SEMI_STABLE,
    "files": SEMI_STABLE,
    "records": SEMI_STABLE,
    "variables": SEMI_STABLE,
    "active_window": VOLATILE,
    "current_url": VOLATILE,
    "ui_elements": VOLATILE,
    "page_elements": VOLATILE,
    "last_action": VOLATILE,
    "last_verification": VOLATILE,
}


@dataclass
class WorldFact:
    """One thing the runtime believes, and when it learned it."""

    slot: str
    value: Any
    source: str
    recorded_at: float = field(default_factory=time.monotonic)

    @property
    def volatility(self) -> str:
        return SLOT_VOLATILITY.get(self.slot, SEMI_STABLE)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.recorded_at

    @property
    def fresh(self) -> bool:
        return self.age_seconds <= FRESHNESS_SECONDS[self.volatility]

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "value": self.value,
            "source": self.source,
            "age_seconds": round(self.age_seconds, 2),
            "volatility": self.volatility,
            "fresh": self.fresh,
        }


class WorldState:
    """Structured execution memory for one task."""

    def __init__(self) -> None:
        self._facts: dict[str, WorldFact] = {}

    def record(self, slot: str, value: Any, *, source: str = "observation") -> WorldFact:
        fact = WorldFact(slot=str(slot), value=value, source=str(source))
        self._facts[fact.slot] = fact
        return fact

    def get(self, slot: str, *, require_fresh: bool = True) -> Any | None:
        fact = self._facts.get(slot)
        if fact is None:
            return None
        if require_fresh and not fact.fresh:
            return None
        return fact.value

    def known(self, slot: str) -> bool:
        return self.get(slot) is not None

    def forget(self, slots: Iterable[str]) -> list[str]:
        removed = [slot for slot in slots if self._facts.pop(slot, None) is not None]
        return removed

    def invalidate_for(self, capability: str) -> list[str]:
        """Drop what an action could have made untrue.

        Acting on a belief the action itself invalidated is how automation
        clicks a button that has already moved.
        """

        return self.forget(INVALIDATED_BY.get(capability, ()))

    def absorb(self, capability: str, observation: Mapping[str, Any]) -> list[str]:
        """Learn from a capability result and forget what it superseded."""

        self.invalidate_for(capability)
        learned: list[str] = []

        if capability == "window.control":
            windows = observation.get("windows")
            if isinstance(windows, list):
                self.record(
                    "windows",
                    [
                        {
                            "title": item.get("title"),
                            "handle": item.get("handle"),
                            "process_id": item.get("process_id"),
                        }
                        for item in windows[:40]
                        if isinstance(item, Mapping)
                    ],
                    source=capability,
                )
                learned.append("windows")
            window = observation.get("window")
            if isinstance(window, Mapping):
                self.record("active_window", dict(window), source=capability)
                learned.append("active_window")

        elif capability == "application.launch":
            target = observation.get("target")
            if target:
                self.record("last_launched", target, source=capability)
                learned.append("last_launched")

        elif capability == "browser.control":
            for key, slot in (("url", "current_url"), ("title", "page_title")):
                value = observation.get(key)
                if value:
                    self.record(slot, value, source=capability)
                    learned.append(slot)
            matches = observation.get("matches")
            if isinstance(matches, list) and matches:
                self.record(
                    "page_elements",
                    [
                        {
                            "element": item.get("element"),
                            "role": item.get("role"),
                            "name": item.get("name"),
                        }
                        for item in matches[:25]
                        if isinstance(item, Mapping)
                    ],
                    source=capability,
                )
                learned.append("page_elements")

        elif capability == "ui.automation":
            window = observation.get("window")
            if isinstance(window, Mapping):
                self.record("active_window", dict(window), source=capability)
                learned.append("active_window")
            matches = observation.get("matches")
            if isinstance(matches, list) and matches:
                self.record(
                    "ui_elements",
                    [
                        {
                            "element": item.get("element"),
                            "control_type": item.get("control_type"),
                            "name": item.get("name"),
                        }
                        for item in matches[:25]
                        if isinstance(item, Mapping)
                    ],
                    source=capability,
                )
                learned.append("ui_elements")

        self.record("last_action", capability, source="runtime")
        return learned

    def briefing(self) -> dict[str, Any]:
        """A compact view of what is currently known and still true.

        Deliberately small: this is given to the model every step, so it must
        stay a summary rather than becoming a second transcript.
        """

        return {
            slot: fact.value
            for slot, fact in sorted(self._facts.items())
            if fact.fresh and slot != "last_action"
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": WORLD_STATE_SCHEMA,
            "facts": {slot: fact.to_dict() for slot, fact in sorted(self._facts.items())},
            "fresh_count": sum(1 for fact in self._facts.values() if fact.fresh),
            "total_count": len(self._facts),
        }
