"""Salty Steak Native Desktop AI Platform — browser session and sign-in handoff.

Two things a general operator needs from a browser that a one-shot page reader
does not: a session that survives the task, and an honest answer to what
happens at a login wall.

The session is a persistent profile folder that Salty Steak owns. It is never
the user's own browser profile — attaching to that would hand the runtime every
site the user is signed in to, silently. Signing in once inside Salty's own
session is a deliberate act with a visible boundary, and because the profile
persists, it is an act the user performs once rather than every task.

At a login wall the runtime stops and asks. It does not type a stored password
into a page it guessed was the right one, and it does not touch multi-factor
prompts. The user completes the sign-in themselves, in a window made visible
for exactly that purpose, and the task resumes. That is slower than automating
the credential, and it is the only version that is honest about who
authenticated.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BROWSER_SESSION_SCHEMA = "salty-steak-browser-session-v1"



SIGN_IN_TIMEOUT_SECONDS = 300.0
SIGN_IN_POLL_SECONDS = 1.5



SIGN_IN_URL_HINTS = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth",
    "/oauth",
    "/sso",
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
)

SIGN_IN_TEXT_HINTS = (
    "sign in",
    "log in",
    "login",
    "password",
    "verify your identity",
    "two-factor",
    "authenticator",
)


def default_profile_directory(workspace_root: str | Path) -> Path:
    """Where Salty Steak's own browser session lives.

    Deliberately inside the workspace and deliberately not the user's Chrome or
    Edge profile: a capability that could read the user's existing sessions is
    a different and much larger grant than the one they agreed to.
    """

    profile = Path(workspace_root).resolve() / "runtime" / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


@dataclass
class SignInRequest:
    """A login wall the runtime has stopped at, described for the user."""

    url: str
    reason: str
    service: str | None = None
    detected_at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BROWSER_SESSION_SCHEMA,
            "state": "sign_in_required",
            "url": self.url,
            "service": self.service,
            "reason": self.reason,


            "action_required": (
                "Sign in yourself in the Salty Steak browser window. Your "
                "password is typed into the site, not into Salty Steak, and it "
                "is not recorded."
            ),
            "credentials_requested_by_model": False,
        }


def looks_like_sign_in(page: Mapping[str, Any]) -> SignInRequest | None:
    """Decide whether a page is asking someone to authenticate.

    Conservative on purpose. A false positive stops a working task to ask a
    pointless question; a false negative merely means the model discovers the
    wall the ordinary way, by finding nothing it expected on the page.
    """

    url = str(page.get("url") or "")
    if not url:
        return None
    lowered = url.casefold()
    summary = str(page.get("summary") or "").casefold()
    title = str(page.get("title") or "")


    has_password_field = any(
        str(control.get("type") or "").casefold() == "password"
        for control in page.get("controls", [])
        if isinstance(control, Mapping)
    )
    url_hint = any(hint in lowered for hint in SIGN_IN_URL_HINTS)
    text_hint = any(hint in summary for hint in SIGN_IN_TEXT_HINTS)

    if not has_password_field and not (url_hint and text_hint):
        return None

    host = lowered.split("//", 1)[-1].split("/", 1)[0] or None
    return SignInRequest(
        url=url,
        service=host,
        reason=(
            "The page is asking for a password"
            if has_password_field
            else f"{title or host} is asking for sign-in"
        ),
    )


class SignInTimeout(RuntimeError):
    """Raised when a sign-in was offered to the user and never completed."""


def await_user_sign_in(
    *,
    client: Any,
    request: SignInRequest,
    task: Any = None,
    timeout_seconds: float = SIGN_IN_TIMEOUT_SECONDS,
    poll_seconds: float = SIGN_IN_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Show the window, wait for the user to sign in, and resume.

    The task enters WAITING so the interface can say what it is waiting for
    rather than appearing to have stalled. Stop stays responsive throughout:
    a user who changes their mind at a login wall should not have to wait out
    the timeout.
    """

    from ..chat.task_runtime import EXECUTING, WAITING

    if task is not None:
        task.transition(WAITING, waiting_for="user_sign_in", url=request.url)
        task.record_event("sign_in_required", **request.to_dict())



    try:
        client.call("show_window", {})
    except Exception:


        pass

    deadline = time.monotonic() + float(timeout_seconds)
    outcome: dict[str, Any]
    while True:
        if task is not None and task.stop_requested:
            outcome = {"status": "cancelled", "reason": "stop_requested"}
            break
        if time.monotonic() >= deadline:
            outcome = {
                "status": "timed_out",
                "reason": f"No sign-in completed within {int(timeout_seconds)} seconds",
            }
            break

        sleep(poll_seconds)
        try:
            page = client.call("get_page", {"limit": 40})
        except Exception:
            continue


        if looks_like_sign_in(page) is None:
            outcome = {
                "status": "signed_in",
                "url": page.get("url"),
                "title": page.get("title"),
            }
            break

    try:
        client.call("hide_window", {})
    except Exception:
        pass

    if task is not None:
        task.record_event("sign_in_finished", status=outcome["status"])
        if not task.finished and outcome["status"] != "cancelled":
            task.transition(EXECUTING, resumed_after="user_sign_in")

    if outcome["status"] == "timed_out":
        raise SignInTimeout(outcome["reason"])
    return outcome
