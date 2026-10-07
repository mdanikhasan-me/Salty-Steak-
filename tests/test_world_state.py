from __future__ import annotations

import json

from app.backend.chat.task_runtime import TaskContext, merge_world_state
from app.backend.chat.world_state import STABLE, VOLATILE, WorldState


def test_a_fact_is_remembered_so_the_next_step_need_not_ask_again() -> None:
    state = WorldState()
    state.record("resolved_executables", {"notepad": r"C:\Windows\notepad.exe"})

    assert state.known("resolved_executables") is True
    assert state.get("resolved_executables")["notepad"].endswith("notepad.exe")
    assert state.get("browser_tabs") is None


def test_a_volatile_fact_expires_but_a_stable_one_does_not() -> None:
    state = WorldState()
    state.record("active_window", {"title": "Notepad"})
    state.record("working_directory", r"D:\Projects")

    # Age the recordings past the volatile horizon without sleeping.
    for fact in state._facts.values():
        fact.recorded_at -= 30.0

    assert state._facts["active_window"].volatility == VOLATILE
    assert state._facts["working_directory"].volatility == STABLE
    assert state.get("active_window") is None
    assert state.get("working_directory") == r"D:\Projects"
    # The value is still there for anyone willing to accept it as stale.
    assert state.get("active_window", require_fresh=False) == {"title": "Notepad"}


def test_an_action_forgets_what_it_could_have_made_untrue() -> None:
    state = WorldState()
    state.record("active_window", {"title": "Notepad"})
    state.record("windows", [{"title": "Notepad"}])
    state.record("working_directory", r"D:\Projects")

    dropped = state.invalidate_for("application.launch")

    # Launching an application changes what windows exist; it does not move
    # the working directory.
    assert set(dropped) == {"active_window", "windows"}
    assert state.get("working_directory") == r"D:\Projects"


def test_a_browser_result_teaches_the_page_it_landed_on() -> None:
    state = WorldState()
    learned = state.absorb(
        "browser.control",
        {
            "url": "https://www.youtube.com",
            "title": "YouTube",
            "matches": [{"element": "web-el-3", "role": "button", "name": "Search"}],
        },
    )

    assert set(learned) == {"current_url", "page_title", "page_elements"}
    assert state.get("current_url") == "https://www.youtube.com"
    assert state.get("page_elements")[0]["element"] == "web-el-3"


def test_a_ui_result_supersedes_the_previous_element_list() -> None:
    state = WorldState()
    state.absorb("ui.automation", {"matches": [{"element": "uia-1", "name": "Old"}]})
    state.absorb("ui.automation", {"matches": [{"element": "uia-2", "name": "New"}]})

    elements = state.get("ui_elements")
    assert len(elements) == 1
    assert elements[0]["element"] == "uia-2"


def test_the_briefing_stays_small_and_carries_only_live_facts() -> None:
    state = WorldState()
    state.record("working_directory", r"D:\Projects")
    state.record("active_window", {"title": "Notepad"})
    state.record("last_action", "window.control")
    state._facts["active_window"].recorded_at -= 30.0

    briefing = state.briefing()

    assert briefing == {"working_directory": r"D:\Projects"}
    # It is handed to the model every step, so it must serialise compactly.
    assert len(json.dumps(briefing)) < 500


def test_a_task_carries_world_state_and_reports_it() -> None:
    context = TaskContext(goal="open youtube")
    merge_world_state(context, {"default_browser": "msedge"})

    assert context.world_state.get("default_browser") == "msedge"
    snapshot = context.snapshot()
    assert snapshot["world_state"]["facts"]["default_browser"]["value"] == "msedge"
    assert snapshot["world_state"]["fresh_count"] == 1
