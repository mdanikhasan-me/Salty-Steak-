from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.backend.chat.task_runtime import TaskContext
from app.backend.system.environment import (
    HostEnvironment,
    HostEnvironmentRegistry,
    resolve_application,
    seed_world_state,
)

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows host discovery")


def _environment(**overrides) -> HostEnvironment:
    base = {
        "platform": "nt",
        "applications": {"notepad": r"C:\Windows\notepad.exe"},
        "known_folders": {"documents": r"C:\Users\test\Documents"},
        "default_browser": "msedge",
        "screen": {
            "primary_width": 1920,
            "primary_height": 1080,
            "virtual_left": 0,
            "virtual_top": 0,
            "virtual_width": 1920,
            "virtual_height": 1080,
            "monitor_count": 1,
        },
        "python_executable": r"C:\python.exe",
        "working_directory": r"D:\Projects",
        "discovered_at": time.monotonic(),
    }
    base.update(overrides)
    return HostEnvironment(**base)


def test_a_discovered_application_is_reported_with_its_path() -> None:
    environment = _environment()

    assert environment.has("Notepad") is True
    assert environment.has("photoshop") is False
    assert environment.path_for("notepad").endswith("notepad.exe")


def test_discovery_is_cached_until_it_is_invalidated(tmp_path: Path) -> None:
    registry = HostEnvironmentRegistry(working_directory=tmp_path)
    calls = {"count": 0}
    discover = registry._discover

    def counted():
        calls["count"] += 1
        return discover()

    registry._discover = counted

    first = registry.get()
    assert registry.get() is first
    assert calls["count"] == 1

    registry.invalidate()
    assert registry.get() is not first
    assert calls["count"] == 2


def test_a_stale_cache_is_re_discovered(tmp_path: Path) -> None:
    registry = HostEnvironmentRegistry(working_directory=tmp_path, ttl_seconds=0.0)
    assert registry.get() is not registry.get()


def test_the_facts_given_to_a_task_stay_small_and_useful() -> None:
    facts = _environment().world_state_facts()

    assert facts["installed_applications"] == ["notepad"]
    assert facts["default_browser"] == "msedge"
    assert facts["screen"] == {"width": 1920, "height": 1080, "monitor_count": 1}
    # Executable paths are discoverable on demand; shipping all of them into
    # every prompt would cost more context than the discovery saves.
    assert "applications" not in facts
    assert len(json.dumps(facts)) < 1500


def test_a_missing_slot_is_absent_rather_than_an_error() -> None:
    facts = _environment(
        default_browser=None, screen=None, known_folders={}
    ).world_state_facts()

    assert "default_browser" not in facts
    assert "screen" not in facts
    assert "known_folders" not in facts
    assert facts["working_directory"] == r"D:\Projects"


def test_a_task_starts_already_knowing_the_machine() -> None:
    context = TaskContext(goal="open my documents")

    seed_world_state(context, _environment())

    assert context.world_state.get("default_browser") == "msedge"
    assert "notepad" in context.world_state.get("installed_applications")
    # Stable facts survive an action that only invalidates volatile ones.
    context.world_state.invalidate_for("application.launch")
    assert context.world_state.get("known_folders")["documents"].endswith("Documents")


@WINDOWS_ONLY
def test_a_real_windows_host_resolves_real_applications(tmp_path: Path) -> None:
    """Proof against this actual machine, not a fixture."""

    environment = HostEnvironmentRegistry(working_directory=tmp_path).get()

    assert environment.platform == "nt"
    # Present on every Windows installation.
    assert environment.has("notepad")
    assert Path(environment.path_for("notepad")).exists()
    assert environment.known_folders.get("home")
    assert environment.screen["primary_width"] > 0
    assert environment.screen["monitor_count"] >= 1


@WINDOWS_ONLY
def test_an_application_off_the_path_still_resolves_through_the_registry() -> None:
    # Edge is installed under Program Files and is not normally on PATH, so a
    # PATH-only lookup would wrongly report the machine cannot browse.
    assert resolve_application("msedge")
    assert resolve_application("no-such-application-xyz") is None
