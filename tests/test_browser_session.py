from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.automation.browser_session import (
    SignInRequest,
    SignInTimeout,
    await_user_sign_in,
    default_profile_directory,
    looks_like_sign_in,
)
from app.backend.chat.task_runtime import EXECUTING, WAITING, TaskContext

PASSWORD = "hunter2-not-in-any-log"


class _FakeBrowser:
    """A session that returns a scripted sequence of pages."""

    def __init__(self, pages: list[dict]) -> None:
        self._pages = list(pages)
        self.calls: list[str] = []

    def call(self, command, payload=None, **_kwargs):
        self.calls.append(command)
        if command == "get_page":
            # The last page repeats: a user who never signs in leaves the wall
            # on screen, they do not arrive somewhere else.
            return self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]
        return {"ok": True}


def _wall(url="https://portal.example/login"):
    return {
        "url": url,
        "title": "Sign in",
        "summary": "sign in with your password",
        "controls": [{"type": "password", "name": "Password"}],
    }


def _home():
    return {"url": "https://portal.example/home", "title": "Home", "summary": "welcome"}


# ------------------------------------------------------------------- profile


def test_the_session_profile_is_salty_owned_and_persistent(tmp_path: Path) -> None:
    profile = default_profile_directory(tmp_path)

    assert profile.is_dir()
    assert profile.parent.name == "runtime"
    # Asked twice, the same folder: a sign-in performed once must still be
    # there for the next task.
    assert default_profile_directory(tmp_path) == profile
    # Never the user's own Chrome or Edge profile.
    assert "Google" not in str(profile) and "Microsoft" not in str(profile)


# ------------------------------------------------------------------ detection


def test_a_password_box_is_recognised_as_a_login_wall() -> None:
    request = looks_like_sign_in(_wall())

    assert request is not None
    assert request.service == "portal.example"
    assert "password" in request.reason


def test_an_ordinary_page_is_not_mistaken_for_a_login_wall() -> None:
    assert looks_like_sign_in(_home()) is None
    # A page that merely mentions logging in is not a wall.
    assert (
        looks_like_sign_in(
            {"url": "https://example.com/help", "summary": "how to log in"}
        )
        is None
    )
    assert looks_like_sign_in({"summary": "password"}) is None


def test_an_authentication_address_with_matching_text_is_recognised() -> None:
    request = looks_like_sign_in(
        {
            "url": "https://login.microsoftonline.com/oauth2/authorize",
            "title": "Sign in",
            "summary": "sign in to continue",
            "controls": [],
        }
    )
    assert request is not None


def test_the_handoff_never_asks_the_model_for_a_credential() -> None:
    payload = SignInRequest(url="https://portal.example/login", reason="asked").to_dict()

    assert payload["credentials_requested_by_model"] is False
    assert "not recorded" in payload["action_required"]
    assert PASSWORD not in json.dumps(payload)


# -------------------------------------------------------------------- waiting


def test_the_user_signs_in_and_the_task_resumes() -> None:
    task = TaskContext(goal="check my results")
    task.transition(EXECUTING)
    browser = _FakeBrowser([_wall(), _wall(), _home()])

    outcome = await_user_sign_in(
        client=browser,
        request=looks_like_sign_in(_wall()),
        task=task,
        sleep=lambda _seconds: None,
    )

    assert outcome["status"] == "signed_in"
    assert outcome["url"] == "https://portal.example/home"
    # The window is shown for the sign-in and put back afterwards.
    assert browser.calls[0] == "show_window"
    assert browser.calls[-1] == "hide_window"
    assert task.state == EXECUTING
    kinds = [event.kind for event in task.events]
    assert "sign_in_required" in kinds and "sign_in_finished" in kinds


def test_the_task_reports_what_it_is_waiting_for() -> None:
    task = TaskContext(goal="check my results")
    seen: list[str] = []
    browser = _FakeBrowser([_wall()] * 3)

    def watch(_seconds: float) -> None:
        seen.append(task.state)

    with pytest.raises(SignInTimeout):
        await_user_sign_in(
            client=browser,
            request=looks_like_sign_in(_wall()),
            task=task,
            timeout_seconds=0.05,
            poll_seconds=0.02,
            sleep=watch,
        )

    # Not a stall: the interface can say the task is waiting on a person.
    assert WAITING in seen
    waiting = next(e for e in task.events if e.kind == "state" and e.detail.get("state") == WAITING)
    assert waiting.detail["waiting_for"] == "user_sign_in"


def test_stop_is_answered_while_waiting_on_a_person() -> None:
    task = TaskContext(goal="check my results")
    browser = _FakeBrowser([_wall()] * 5)
    task.request_stop("user_requested")

    outcome = await_user_sign_in(
        client=browser,
        request=looks_like_sign_in(_wall()),
        task=task,
        sleep=lambda _seconds: None,
    )

    assert outcome["status"] == "cancelled"
    # A user who changes their mind at a login wall must not have to wait out
    # the five-minute timeout.
    assert "get_page" not in browser.calls


def test_a_sign_in_leaves_no_credential_anywhere_in_the_task_record() -> None:
    task = TaskContext(goal="sign in")
    task.transition(EXECUTING)
    browser = _FakeBrowser([_wall(), _home()])

    await_user_sign_in(
        client=browser,
        request=looks_like_sign_in(_wall()),
        task=task,
        sleep=lambda _seconds: None,
    )

    assert PASSWORD not in json.dumps(task.snapshot(include_events=True))


def test_an_older_host_without_window_commands_still_works() -> None:
    class _Older(_FakeBrowser):
        def call(self, command, payload=None, **kwargs):
            if command in {"show_window", "hide_window"}:
                raise RuntimeError("Unknown browser command")
            return super().call(command, payload, **kwargs)

    outcome = await_user_sign_in(
        client=_Older([_home()]),
        request=looks_like_sign_in(_wall()),
        task=None,
        sleep=lambda _seconds: None,
    )
    assert outcome["status"] == "signed_in"
